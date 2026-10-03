"""Geçmiş Claude Code transcript'lerinin denetimi: CimriHook açık olsaydı ne kadar tasarruf olurdu?

Canlı hook'un kullandığı saf codec, kayıtlı araç sonuçları üzerinde yeniden oynatılır. REF ve
DELTA kayıpsız olduğundan tahminleri gerçekçidir; OUTLINE ise ajanın sonra hangi aralıkları
okuyacağı bilinemediği için üst sınırdır. Tasarruf iki biçimde raporlanır:
- doğrudan: modele gitmeyecek araç sonucu tokenları,
- bağlamda kalma ağırlıklı: her token, sıkıştırmaya kadar sonraki her API isteğinde önbellekten
  yeniden okunur; tasarruf, gerçek önbellek fiyat çarpanlarıyla girdi maliyetine oranlanır.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from cimrihook.claude import MUTATING_TOOLS, JsonObject, is_unchanged_read, observe
from cimrihook.codec import CodecConfig, decide
from cimrihook.errors import HookPayloadError
from cimrihook.model import Decision, Encoding, SavingsRow, View
from cimrihook.report import render_table, total_line

SECONDS_PER_DAY: Final = 86_400
WORKFLOW_JOURNAL: Final = "journal.jsonl"
BOUNDARY_SUBTYPES: Final = frozenset({"compact_boundary", "microcompact_boundary"})
# Taban girdi fiyatına göre çarpanlar (Anthropic prompt caching fiyatlandırması).
WRITE_5M_WEIGHT: Final = 1.25
WRITE_1H_WEIGHT: Final = 2.0
READ_WEIGHT: Final = 0.1


@dataclass(frozen=True, slots=True)
class Usage:
    """Bir API isteğinin girdi tarafı token kullanımı."""

    uncached: int
    write_5m: int
    write_1h: int
    read: int
    output: int


@dataclass(frozen=True, slots=True)
class ToolUse:
    """Asistan mesajındaki araç çağrısı."""

    id: str
    name: str
    input: JsonObject


@dataclass(frozen=True, slots=True)
class Replayed:
    """Yeniden oynatılan tek karar ve bağlamda kalma süresi."""

    tool: str
    decision: Decision
    later_requests: int  # sonuç bağlama girdikten sonra kuşak bitene kadar yapılan istek sayısı


@dataclass(frozen=True, slots=True)
class TranscriptReplay:
    """Tek transcript (tek bağlam penceresi) üzerindeki yeniden oynatma sonucu."""

    decisions: tuple[Replayed, ...]
    usages: dict[str, Usage]  # message.id -> son satırdaki kullanım
    native_unchanged: int  # Claude Code'un kendi 'file_unchanged' yanıtları
    skipped: int  # ayrıştırılamayan satırlar ve şemaya uymayan sonuçlar


@dataclass(frozen=True, slots=True)
class AuditResult:
    """Tüm transcript'lerin özeti."""

    transcripts: int
    subagent_transcripts: int
    days: int
    requests: int
    usage: Usage
    savings: tuple[SavingsRow, ...]
    native_unchanged: int
    skipped: int
    weighted_saved: float  # taban girdi fiyatı cinsinden tasarruf
    weighted_input: float  # taban girdi fiyatı cinsinden toplam girdi maliyeti


def audit_transcripts(
    projects_dir: Path, days: int, config: CodecConfig, now: float
) -> AuditResult:
    """Son `days` gün içinde değişen transcript'leri yeniden oynatır ve özetler."""
    files = transcript_files(projects_dir, now - days * SECONDS_PER_DAY)
    replays = [replay_transcript(path, config) for path in files]
    usages: dict[str, Usage] = {}
    for replay in replays:
        usages.update(replay.usages)  # aynı mesaj birden çok dosyada olabilir: kimliğe göre tekille
    usage = Usage(
        uncached=sum(item.uncached for item in usages.values()),
        write_5m=sum(item.write_5m for item in usages.values()),
        write_1h=sum(item.write_1h for item in usages.values()),
        read=sum(item.read for item in usages.values()),
        output=sum(item.output for item in usages.values()),
    )
    decisions = [item for replay in replays for item in replay.decisions]
    write_weight = average_write_weight(usage)
    return AuditResult(
        transcripts=len(files),
        subagent_transcripts=sum(1 for path in files if "subagents" in path.parts),
        days=days,
        requests=len(usages),
        usage=usage,
        savings=summarize(decisions),
        native_unchanged=sum(replay.native_unchanged for replay in replays),
        skipped=sum(replay.skipped for replay in replays),
        weighted_saved=sum(weighted_saving(item, write_weight) for item in decisions),
        weighted_input=usage.uncached
        + usage.write_5m * WRITE_5M_WEIGHT
        + usage.write_1h * WRITE_1H_WEIGHT
        + usage.read * READ_WEIGHT,
    )


def transcript_files(projects_dir: Path, min_mtime: float) -> tuple[Path, ...]:
    """Ana ve alt ajan transcript'leri (iş akışı günlükleri hariç)."""
    return tuple(
        sorted(
            path
            for path in projects_dir.rglob("*.jsonl")
            if path.name != WORKFLOW_JOURNAL and path.stat().st_mtime >= min_mtime
        )
    )


def replay_transcript(path: Path, config: CodecConfig) -> TranscriptReplay:
    """Bir transcript'teki araç sonuçlarını canlı hook'la aynı codec'ten geçirir.

    Durum yalnızca bu fonksiyonun içinde tutulur; dışarıya saf bir sonuç döner.
    """
    tool_uses: dict[str, ToolUse] = {}
    usages: dict[str, Usage] = {}
    request_ids: set[str] = set()
    views: dict[str, list[View]] = {}
    latest: dict[str, View] = {}
    mutations: list[tuple[int, str]] = []
    pending: list[tuple[str, Decision, int]] = []  # (araç, karar, karar anındaki istek sayısı)
    decisions: list[Replayed] = []
    native_unchanged = 0
    skipped = 0
    step = 0
    with path.open("rb") as handle:
        for raw_line in handle:
            entry = parse_line(raw_line)
            if entry is None:
                skipped += 1
                continue
            if entry.get("type") == "system" and entry.get("subtype") in BOUNDARY_SUBTYPES:
                decisions.extend(close_generation(pending, len(request_ids)))
                pending, views, latest, mutations = [], {}, {}, []
                continue
            message = entry.get("message")
            if entry.get("type") == "assistant" and isinstance(message, dict):
                tool_uses.update((use.id, use) for use in assistant_tool_uses(message))
                message_id = message.get("id")
                if isinstance(message_id, str):
                    request_ids.add(message_id)
                    usage = message_usage(message)
                    if usage is not None:
                        usages[message_id] = usage  # aynı kimliğin son satırı geçerlidir
                continue
            result = user_tool_result(entry)
            if result is None:
                continue
            tool_use_id, tool_response, cwd = result
            use = tool_uses.get(tool_use_id)
            if use is None:
                continue
            step += 1
            if is_unchanged_read(use.name, tool_response):
                native_unchanged += 1
                continue
            try:
                obs = observe(use.name, use.input, tool_response, cwd)
            except HookPayloadError:
                skipped += 1  # eski sürümlerin farklı şemaları sayılır, sessizce yutulmaz
                continue
            if use.name in MUTATING_TOOLS:
                mutations.append((step, tool_use_id if obs is None else obs.request_key))
            if obs is None:
                continue
            previous = latest.get(obs.request_key)
            mutated = previous is not None and any(
                index > previous.step and key != obs.request_key for index, key in mutations
            )
            stream_views = views.setdefault(obs.stream, [])
            decision = decide(obs, stream_views, previous, mutated, config)
            view = View(
                step=step,
                stream=obs.stream,
                request_key=obs.request_key,
                encoding=decision.encoding,
                start_line=obs.start_line,
                lines=() if decision.encoding is Encoding.OUTLINE else obs.lines,
                total_lines=obs.total_lines,
                whole=obs.whole,
            )
            stream_views.append(view)
            latest[obs.request_key] = view
            pending.append((use.name, decision, len(request_ids)))
    decisions.extend(close_generation(pending, len(request_ids)))
    return TranscriptReplay(tuple(decisions), usages, native_unchanged, skipped)


def close_generation(
    pending: Sequence[tuple[str, Decision, int]], requests_at_end: int
) -> list[Replayed]:
    """Kuşak bittiğinde her kararın bağlamda kaç istek boyunca kaldığını hesaplar."""
    return [
        Replayed(tool, decision, requests_at_end - requests_at_decision)
        for tool, decision, requests_at_decision in pending
    ]


def parse_line(raw_line: bytes) -> JsonObject | None:
    """Transcript satırını çözer; yarım yazılmış ya da bozuk satırlar için None."""
    try:
        decoded: object = json.loads(raw_line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(decoded, dict):
        return None
    return {str(key): value for key, value in decoded.items()}


def assistant_tool_uses(message: dict[object, object]) -> tuple[ToolUse, ...]:
    """Asistan mesajındaki tool_use blokları."""
    content = message.get("content")
    if not isinstance(content, list):
        return ()
    return tuple(
        ToolUse(
            id=block["id"],
            name=block["name"],
            input={str(key): value for key, value in block["input"].items()},
        )
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "tool_use"
        and isinstance(block.get("id"), str)
        and isinstance(block.get("name"), str)
        and isinstance(block.get("input"), dict)
    )


def message_usage(message: dict[object, object]) -> Usage | None:
    """Mesajın girdi tarafı kullanımı; token alanları yoksa None."""
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None
    uncached = usage.get("input_tokens")
    written = usage.get("cache_creation_input_tokens")
    read = usage.get("cache_read_input_tokens")
    output = usage.get("output_tokens")
    if not (
        isinstance(uncached, int)
        and isinstance(written, int)
        and isinstance(read, int)
        and isinstance(output, int)
    ):
        return None
    split = usage.get("cache_creation")
    write_1h = split.get("ephemeral_1h_input_tokens") if isinstance(split, dict) else None
    hour = write_1h if isinstance(write_1h, int) else 0  # ayrıntı yoksa yazımlar 5 dakikalıktır
    return Usage(
        uncached=uncached, write_5m=written - hour, write_1h=hour, read=read, output=output
    )


def user_tool_result(entry: JsonObject) -> tuple[str, object, str] | None:
    """Tek bir tool_result taşıyan kullanıcı satırından (tool_use_id, sonuç, cwd) çıkarır."""
    message = entry.get("message")
    tool_response = entry.get("toolUseResult")
    cwd = entry.get("cwd")
    if entry.get("type") != "user" or tool_response is None or not isinstance(cwd, str):
        return None
    if not isinstance(message, dict) or not isinstance(message.get("content"), list):
        return None
    ids = [
        block.get("tool_use_id")
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]
    if len(ids) != 1 or not isinstance(ids[0], str):
        return None
    return ids[0], tool_response, cwd


def average_write_weight(usage: Usage) -> float:
    """Gözlenen 5 dk / 1 saat önbellek yazım karışımının ortalama fiyat çarpanı."""
    written = usage.write_5m + usage.write_1h
    if written == 0:
        return WRITE_5M_WEIGHT
    return (usage.write_5m * WRITE_5M_WEIGHT + usage.write_1h * WRITE_1H_WEIGHT) / written


def weighted_saving(item: Replayed, write_weight: float) -> float:
    """Kaydedilen token bir kez önbelleğe yazılır, sonraki her istekte yeniden okunurdu."""
    saved = item.decision.tokens_raw - item.decision.tokens_sent
    if item.later_requests <= 0:
        return 0.0
    return saved * (write_weight + READ_WEIGHT * (item.later_requests - 1))


def summarize(decisions: Sequence[Replayed]) -> tuple[SavingsRow, ...]:
    """Kararları kodlama + araç çiftine göre toplar."""
    totals: dict[tuple[Encoding, str], tuple[int, int, int]] = {}
    for item in decisions:
        key = (item.decision.encoding, item.tool)
        count, raw, sent = totals.get(key, (0, 0, 0))
        totals[key] = (count + 1, raw + item.decision.tokens_raw, sent + item.decision.tokens_sent)
    return tuple(
        SavingsRow(encoding, tool, count, raw, sent)
        for (encoding, tool), (count, raw, sent) in sorted(totals.items())
    )


def render_audit(result: AuditResult) -> str:
    """Denetim raporunun metni."""
    usage = result.usage
    share = 100 * result.weighted_saved / result.weighted_input if result.weighted_input else 0.0
    return "\n".join(
        [
            f"CimriHook audit: {result.transcripts} transcripts "
            f"({result.subagent_transcripts} subagent) changed in the last {result.days} days",
            f"API requests: {result.requests:,} | input-side tokens: uncached {usage.uncached:,}, "
            f"cache write {usage.write_5m + usage.write_1h:,} (1h TTL {usage.write_1h:,}), "
            f"cache read {usage.read:,}",
            f"Claude Code native 'file_unchanged' hits: {result.native_unchanged:,} | "
            f"skipped lines/results: {result.skipped:,}",
            "What CimriHook would have sent instead (OUTLINE is an upper bound):",
            *render_table(result.savings),
            total_line(result.savings),
            f"Context-residency weighted: {result.weighted_saved:,.0f} of "
            f"{result.weighted_input:,.0f} base-price input token equivalents ({share:.1f}%); "
            "each saved token would have been re-read from cache on every later request "
            "until compaction.",
        ]
    )
