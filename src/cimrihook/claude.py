"""Claude Code bağlayıcısı.

Hook yükünü tipli olaylara ayrıştırır, Read/Bash sonuçlarını codec gözlemlerine çevirir ve
`updatedToolOutput` yanıtlarını aracın kendi çıktı şemasına uygun üretir; Claude Code şemaya
uymayan çıktıyı reddedip orijinali kullanır. Şekiller Claude Code 2.1.288 ile doğrulanmıştır:
Read `offset` 1 tabanlıdır, başarısız Bash çağrıları (sıfır olmayan çıkış kodu) yapılandırılmış
sonuç yerine düz metin hata döndürür ve sıkıştırma transcript'e `compact_boundary` olarak yazılır.
"""

import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from cimrihook.errors import HookPayloadError
from cimrihook.model import Observation, Tool

type JsonObject = dict[str, object]

READ_DEFAULT_LIMIT: Final = 2000
REHYDRATE_MAX_CHARS: Final = 100_000  # Claude Code'un Read token tavanına yakın sınır
MAX_LABEL_CHARS: Final = 100
MUTATING_TOOLS: Final = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit", "Bash"})
# Komut dosya değiştirdiyse, git işlemi yaptıysa ya da arka planda sürüyorsa Claude Code sonuca
# ek alanlar koyar; bu sonuçlar yeniden kodlanmaz.
BASH_SIDE_EFFECT_KEYS: Final = frozenset({"bashEditDiff", "gitOperation", "backgroundTaskId"})
BASH_PERSISTED_KEYS: Final = frozenset({"persistedOutputPath", "persistedOutputSize"})
BOUNDARY_MARKERS: Final = (b'"subtype":"compact_boundary"', b'"subtype":"microcompact_boundary"')
MARKER_SLACK: Final = max(len(marker) for marker in BOUNDARY_MARKERS)
RESET_EVENTS: Final = frozenset({"SessionStart", "PreCompact"})


@dataclass(frozen=True, slots=True)
class Session:
    """Hook çağrısının ait olduğu oturum ve bağlam penceresi."""

    session_id: str
    agent_id: str | None  # yalnızca alt ajan içindeki çağrılarda dolu
    transcript_path: str
    cwd: str


@dataclass(frozen=True, slots=True)
class ToolResult:
    """PostToolUse olayı."""

    session: Session
    tool_name: str
    tool_use_id: str
    tool_input: JsonObject
    tool_response: object


@dataclass(frozen=True, slots=True)
class ContextReset:
    """SessionStart ya da PreCompact: bağlamdaki eski içerik artık güvenilmez."""

    session_id: str
    transcript_path: str
    event: str


@dataclass(frozen=True, slots=True)
class ReadRequest:
    """Read çağrısının normalize edilmiş girdisi."""

    path: str  # ajanın verdiği yol
    resolved: str  # sembolik bağlantıları çözülmüş mutlak yol
    start_line: int
    limit: int | None
    key: str
    full: bool  # offset/limit verilmeden (tüm dosya) istendi mi


type HookEvent = ToolResult | ContextReset


def parse_event(raw: str) -> HookEvent:
    """stdin'deki hook yükünü tipli olaya çevirir."""
    try:
        decoded: object = json.loads(raw)
    except json.JSONDecodeError as error:
        raise HookPayloadError(
            f"hook payload is not valid JSON ({error.msg}); first bytes: {raw[:120]!r}"
        ) from error
    payload = as_object(decoded, "payload")
    event = require_str(payload, "hook_event_name", "payload")
    if event == "PostToolUse":
        return parse_tool_result(payload)
    if event in RESET_EVENTS:
        return ContextReset(
            session_id=require_str(payload, "session_id", event),
            transcript_path=require_str(payload, "transcript_path", event),
            event=event,
        )
    raise HookPayloadError(
        f"unsupported hook_event_name {event!r}; register CimriHook only for "
        "PostToolUse, SessionStart and PreCompact"
    )


def parse_tool_result(payload: JsonObject) -> ToolResult:
    """PostToolUse yükünü ayrıştırır."""
    where = "PostToolUse"
    return ToolResult(
        session=Session(
            session_id=require_str(payload, "session_id", where),
            agent_id=optional_str(payload, "agent_id", where),
            transcript_path=require_str(payload, "transcript_path", where),
            cwd=require_str(payload, "cwd", where),
        ),
        tool_name=require_str(payload, "tool_name", where),
        tool_use_id=require_str(payload, "tool_use_id", where),
        tool_input=as_object(payload.get("tool_input"), f"{where}.tool_input"),
        tool_response=payload.get("tool_response"),
    )


def context_key(session: Session) -> str:
    """Ana konuşma ve her alt ajan ayrı bir bağlam penceresidir."""
    agent = "main" if session.agent_id is None else session.agent_id
    return f"{session.session_id}:{agent}"


def read_request(tool_input: Mapping[str, object], cwd: str) -> ReadRequest:
    """Read girdisini normalize eder (offset 1 tabanlıdır; offset yoksa 1)."""
    where = "Read.tool_input"
    path = require_str(tool_input, "file_path", where)
    offset = optional_int(tool_input, "offset", where)
    limit = optional_int(tool_input, "limit", where)
    resolved = os.path.realpath(os.path.join(cwd, path))
    start_line = 1 if offset is None or offset < 1 else offset
    return ReadRequest(
        path=path,
        resolved=resolved,
        start_line=start_line,
        limit=limit,
        key=digest("read", resolved, str(start_line), str(limit)),
        full=start_line == 1 and limit is None,
    )


def observe(
    tool_name: str, tool_input: JsonObject, tool_response: object, cwd: str
) -> Observation | None:
    """Desteklenen araç sonucunu gözleme çevirir; yeniden kodlanamayan sonuçlar için None."""
    if tool_name == Tool.READ.value:
        return observe_read(tool_input, tool_response, cwd)
    if tool_name == Tool.BASH.value:
        return observe_bash(tool_input, tool_response, cwd)
    return None


def observe_read(tool_input: JsonObject, tool_response: object, cwd: str) -> Observation | None:
    """Metin Read sonucunu gözleme çevirir; görsel, PDF ve 'değişmedi' sonuçları için None."""
    if isinstance(tool_response, str):
        return None  # başarısız çağrılar düz metin hata olarak döner
    response = as_object(tool_response, "Read.tool_response")
    if require_str(response, "type", "Read.tool_response") != "text":
        return None
    where = "Read.tool_response.file"
    file = as_object(response.get("file"), where)
    content = require_str(file, "content", where)
    num_lines = require_int(file, "numLines", where)
    lines = tuple(content.split("\n")[:num_lines]) if num_lines > 0 else ()
    return read_observation(
        read_request(tool_input, cwd),
        require_int(file, "startLine", where),
        lines,
        require_int(file, "totalLines", where),
        optional_bool(file, "truncatedByTokenCap", where) is True,
    )


def read_observation(
    request: ReadRequest,
    start_line: int,
    lines: tuple[str, ...],
    total_lines: int,
    truncated: bool,
) -> Observation:
    """Read isteği ve döndürülen satırlardan gözlem kurar."""
    return Observation(
        tool=Tool.READ,
        stream=f"read:{request.resolved}",
        request_key=request.key,
        label=request.path,
        start_line=start_line,
        lines=lines,
        total_lines=total_lines,
        whole=start_line == 1 and len(lines) >= total_lines and not truncated,
        full_request=request.full,
    )


def observe_read_from_disk(request: ReadRequest) -> Observation:
    """Read'in döndüreceği pencereyi diskten üretir (yerel 'değişmedi' yanıtını doldurmak için)."""
    text = Path(request.resolved).read_text(encoding="utf-8")
    all_lines = text.removesuffix("\n").split("\n") if text else []
    count = READ_DEFAULT_LIMIT if request.limit is None else request.limit
    begin = request.start_line - 1
    lines = fit_chars(all_lines[begin : begin + count], REHYDRATE_MAX_CHARS)
    return read_observation(request, request.start_line, lines, len(all_lines), False)


def fit_chars(lines: Sequence[str], max_chars: int) -> tuple[str, ...]:
    """Toplam karakter sınırına sığan baştaki satırlar."""
    kept: list[str] = []
    used = 0
    for line in lines:
        used += len(line) + 1
        if used > max_chars:
            break
        kept.append(line)
    return tuple(kept)


def observe_bash(tool_input: JsonObject, tool_response: object, cwd: str) -> Observation | None:
    """Başarılı, ön planda çalışmış ve yan etkisi raporlanmamış Bash sonucunu gözleme çevirir."""
    if isinstance(tool_response, str):
        return None  # sıfır olmayan çıkış kodu: Claude Code düz metin hata döndürür
    where = "Bash.tool_response"
    response = as_object(tool_response, where)
    if BASH_SIDE_EFFECT_KEYS & response.keys():
        return None
    if optional_bool(response, "isImage", where) is True or require_bool(
        response, "interrupted", where
    ):
        return None
    command = require_str(tool_input, "command", "Bash.tool_input")
    lines = output_lines(
        require_str(response, "stdout", where), require_str(response, "stderr", where)
    )
    stream = f"bash:{cwd}\0{command}"
    return Observation(
        tool=Tool.BASH,
        stream=stream,
        request_key=digest(stream),
        label=f"`{shorten(command)}`",
        start_line=1,
        lines=lines,
        total_lines=len(lines),
        whole=True,
        full_request=False,
    )


def output_lines(stdout: str, stderr: str) -> tuple[str, ...]:
    """stdout ve stderr'i ajanın gördüğü sırayla tek satır dizisine çevirir."""
    out = stdout.split("\n") if stdout else []
    err = ["[stderr]", *stderr.split("\n")] if stderr else []
    return (*out, *err)


def is_unchanged_read(tool_name: str, tool_response: object) -> bool:
    """Claude Code'un yerel Read tekilleştirmesi ('file_unchanged') mi?"""
    return (
        tool_name == Tool.READ.value
        and isinstance(tool_response, dict)
        and tool_response.get("type") == "file_unchanged"
    )


def message_output(obs: Observation, tool_response: object, message: str) -> JsonObject:
    """Codec mesajını, aracın çıktı şemasına uyan bir updatedToolOutput nesnesine sarar."""
    if obs.tool is Tool.READ:
        lines = tuple(message.split("\n"))
        return read_output(obs.label, lines, 1, len(lines))
    original = as_object(tool_response, "Bash.tool_response")
    kept = {key: value for key, value in original.items() if key not in BASH_PERSISTED_KEYS}
    return kept | {"stdout": message, "stderr": ""}


def read_output(
    file_path: str, lines: Sequence[str], start_line: int, total_lines: int
) -> JsonObject:
    """Read aracının 'text' çıktı şeması."""
    file: JsonObject = {
        "filePath": file_path,
        "content": "\n".join(lines),
        "numLines": len(lines),
        "startLine": start_line,
        "totalLines": total_lines,
    }
    return {"type": "text", "file": file}


def post_tool_use_output(updated: JsonObject) -> JsonObject:
    """PostToolUse hook yanıtı: aracın modele gidecek sonucunu değiştirir."""
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "updatedToolOutput": updated}}


def transcript_size(path: str) -> int:
    """Transcript henüz yazılmamış olabilir (Claude Code onu eşzamansız yazar); o durumda 0."""
    try:
        return os.path.getsize(path)
    except FileNotFoundError:
        return 0


def transcript_has_new_boundary(path: str, offset: int, size: int) -> bool:
    """Son kontrolden beri transcript'e (mikro)sıkıştırma sınırı yazıldı mı?"""
    if size < offset:
        return True  # dosya küçüldü: bağlamın içeriği bilinmiyor, güvenli taraf sıfırlamaktır
    if size == offset:
        return False
    start = max(0, offset - MARKER_SLACK)
    with open(path, "rb") as handle:
        handle.seek(start)
        chunk = handle.read(size - start)
    return any(marker in chunk for marker in BOUNDARY_MARKERS)


def as_object(value: object, where: str) -> JsonObject:
    """JSON nesnesi bekler."""
    if not isinstance(value, dict):
        raise HookPayloadError(f"{where}: expected a JSON object, got {type(value).__name__}")
    return {str(key): item for key, item in value.items()}


def require_str(obj: Mapping[str, object], key: str, where: str) -> str:
    """Zorunlu metin alanı."""
    value = obj.get(key)
    if not isinstance(value, str):
        raise HookPayloadError(f"{where}.{key}: expected a string, got {value!r:.80}")
    return value


def optional_str(obj: Mapping[str, object], key: str, where: str) -> str | None:
    """İsteğe bağlı metin alanı."""
    value = obj.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise HookPayloadError(f"{where}.{key}: expected a string, got {value!r:.80}")
    return value


def require_int(obj: Mapping[str, object], key: str, where: str) -> int:
    """Zorunlu tam sayı alanı."""
    value = optional_int(obj, key, where)
    if value is None:
        raise HookPayloadError(f"{where}.{key}: required integer is missing")
    return value


def optional_int(obj: Mapping[str, object], key: str, where: str) -> int | None:
    """İsteğe bağlı tam sayı alanı."""
    value = obj.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise HookPayloadError(f"{where}.{key}: expected an integer, got {value!r:.80}")
    return value


def require_bool(obj: Mapping[str, object], key: str, where: str) -> bool:
    """Zorunlu mantıksal alan."""
    value = optional_bool(obj, key, where)
    if value is None:
        raise HookPayloadError(f"{where}.{key}: required boolean is missing")
    return value


def optional_bool(obj: Mapping[str, object], key: str, where: str) -> bool | None:
    """İsteğe bağlı mantıksal alan."""
    value = obj.get(key)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise HookPayloadError(f"{where}.{key}: expected a boolean, got {value!r:.80}")
    return value


def digest(*parts: str) -> str:
    """Parçaların kararlı kimliği."""
    joined = "\0".join(parts).encode("utf-8", "surrogatepass")
    return hashlib.sha256(joined).hexdigest()[:32]


def shorten(text: str) -> str:
    """Komutu tek satıra indirip kısaltır."""
    one_line = " ".join(text.split())
    if len(one_line) <= MAX_LABEL_CHARS:
        return one_line
    return one_line[: MAX_LABEL_CHARS - 3] + "..."
