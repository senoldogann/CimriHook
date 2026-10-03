"""Bağlam codec'i: araç sonuçlarını ajanın bağlamında zaten bulunan bilgiye göre yeniden kodlar.

Temel ilke: ajan aynı bilgi için iki kez token ödememeli. Araç her zaman gerçekten çalışır;
yalnızca sonucun modele nasıl iletileceği değişir (video codec'lerindeki anahtar kare + fark
mantığı). Modül saf fonksiyonlardan oluşur; canlı hook ve geçmiş transcript denetimi (audit)
aynı kararları bu fonksiyonlarla üretir.

Kodlamalar:
- REF: istenen içerik bağlamdaki en güncel bilgiyle birebir aynı → kısa referans (kayıpsız).
- DELTA: içerik son anahtar kareden (ham iletilmiş tam sürüm) farklı → birleşik fark (kayıpsız).
  Fark her zaman anahtar kareye göre alınır; ajan en fazla bir farkı zihnen uygular.
- OUTLINE: büyük ve hiç görülmemiş bir dosyanın tamamı istendi → yapısal iskelet (geri alınabilir).
"""

import difflib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from cimrihook.model import Decision, Encoding, Observation, Tool, View
from cimrihook.outline import OutlineEntry, extract_outline, limit_outline

CHARS_PER_TOKEN: Final = 4
MAX_REF_RATIO: Final = 1.0
MAX_OUTLINE_RATIO: Final = 0.35
MAX_OUTLINE_ENTRIES: Final = 250
MIN_OUTLINE_ENTRIES: Final = 3
MAX_DIFF_LINES: Final = 6000  # difflib'in karesel en kötü durumuna karşı üst sınır
DIFF_CONTEXT_LINES: Final = 2
HUNK_HEADER: Final = re.compile(r"^@@ -(\d+)(,\d+)? \+(\d+)(,\d+)? @@")


@dataclass(frozen=True, slots=True)
class CodecConfig:
    """Codec eşikleri; tez deneylerinde (ablation) tek tek değiştirilebilir."""

    enabled: frozenset[Encoding]
    outline_min_tokens: int
    delta_max_ratio: float
    min_saving_tokens: int


def estimate_tokens(text: str) -> int:
    """Kaba token tahmini (~4 karakter/token); ölçümde gerçek API kullanımı esas alınır."""
    return -(-len(text) // CHARS_PER_TOKEN)


def raw_tokens(obs: Observation) -> int:
    """Ham sonucun modele maliyetini tahmin eder (Read için "<n>\\t" satır önekleri dahil)."""
    chars = sum(len(line) + 1 for line in obs.lines)
    if obs.tool is Tool.READ:
        chars += sum(len(str(obs.start_line + index)) + 1 for index in range(len(obs.lines)))
    return -(-chars // CHARS_PER_TOKEN)


def last_line(start_line: int, lines: Sequence[str]) -> int:
    """Bir satır aralığının son satır numarası."""
    return start_line + len(lines) - 1


def agent_wants_raw(previous: View | None, mutated_since_previous: bool) -> bool:
    """Kaçış kuralı: ısrar eden ajan ham çıktıyı alır; hiçbir bilgi kalıcı olarak gizlenmez.

    - Aynı isteğe bir bağlam kuşağında yalnızca bir kez iskelet (OUTLINE) verilir.
    - REF/DELTA ile yanıtlanmış bir istek, arada sonucu değiştirebilecek bir araç çağrısı
      olmadan tekrarlanırsa ajan ham çıktıyı istiyordur.
    """
    if previous is None:
        return False
    if previous.encoding is Encoding.OUTLINE:
        return True
    return previous.encoding is not Encoding.RAW and not mutated_since_previous


def decide(
    obs: Observation,
    stream_views: Sequence[View],
    previous_request: View | None,
    mutated_since_previous: bool,
    config: CodecConfig,
) -> Decision:
    """Gözlemi, ajanın bağlamındaki bilgiye göre en ucuz güvenli biçimde kodlar."""
    raw = raw_tokens(obs)
    if agent_wants_raw(previous_request, mutated_since_previous):
        return Decision(Encoding.RAW, "", raw, raw)
    if Encoding.REF in config.enabled:
        ref = encode_ref(obs, stream_views)
        if ref is not None and worth_sending(ref, raw, MAX_REF_RATIO, config.min_saving_tokens):
            return Decision(Encoding.REF, ref, raw, estimate_tokens(ref))
    if Encoding.DELTA in config.enabled:
        delta = encode_delta(obs, stream_views)
        if delta is not None and worth_sending(
            delta, raw, config.delta_max_ratio, config.min_saving_tokens
        ):
            return Decision(Encoding.DELTA, delta, raw, estimate_tokens(delta))
    if Encoding.OUTLINE in config.enabled:
        outline = encode_outline(obs, stream_views, raw, config.outline_min_tokens)
        if outline is not None and worth_sending(
            outline, raw, MAX_OUTLINE_RATIO, config.min_saving_tokens
        ):
            return Decision(Encoding.OUTLINE, outline, raw, estimate_tokens(outline))
    return Decision(Encoding.RAW, "", raw, raw)


def worth_sending(message: str, raw: int, max_ratio: float, min_saving: int) -> bool:
    """Yeniden kodlama yalnızca belirgin tasarruf sağlıyorsa yapılır."""
    sent = estimate_tokens(message)
    return sent <= raw * max_ratio and raw - sent >= min_saving


def covers(view: View, obs: Observation) -> bool:
    """Görünüm, gözlemin istediği satırların tamamını içeriyor mu?"""
    if obs.whole:
        return view.whole and view.total_lines == obs.total_lines
    return view.start_line <= obs.start_line and last_line(
        view.start_line, view.lines
    ) >= last_line(obs.start_line, obs.lines)


def window(view: View, obs: Observation) -> tuple[str, ...]:
    """Görünümden, gözlemin satır aralığına denk gelen dilimi döndürür."""
    if obs.whole:
        return view.lines
    begin = obs.start_line - view.start_line
    return view.lines[begin : begin + len(obs.lines)]


def latest_knowledge(obs: Observation, views: Sequence[View]) -> View | None:
    """Gözlemin aralığını kapsayan, içerik taşıyan en güncel görünüm."""
    for view in reversed(views):
        if view.encoding is not Encoding.OUTLINE and covers(view, obs):
            return view
    return None


def latest_keyframe(obs: Observation, views: Sequence[View]) -> View | None:
    """Delta tabanı: gözlemin aralığını içeren, ham iletilmiş en güncel sürüm."""
    for view in reversed(views):
        if view.encoding is not Encoding.RAW:
            continue
        if obs.whole and view.whole:
            return view
        if (
            not obs.whole
            and view.start_line <= obs.start_line
            and (
                view.whole
                or last_line(view.start_line, view.lines) >= last_line(obs.start_line, obs.lines)
            )
        ):
            return view
    return None


def encode_ref(obs: Observation, views: Sequence[View]) -> str | None:
    """İçerik ajanın bildiğiyle birebir aynıysa referans mesajı üretir."""
    known = latest_knowledge(obs, views)
    if known is None or window(known, obs) != obs.lines:
        return None
    return ref_text(obs)


def encode_delta(obs: Observation, views: Sequence[View]) -> str | None:
    """Son anahtar kareye göre birleşik fark mesajı üretir."""
    base = latest_keyframe(obs, views)
    if base is None:
        return None
    old = window(base, obs)
    if len(old) + len(obs.lines) > MAX_DIFF_LINES:
        return None
    diff = unified_diff(old, obs.lines, 1 if obs.whole else obs.start_line)
    if not diff:
        return None
    return delta_text(obs, diff)


def encode_outline(
    obs: Observation, views: Sequence[View], raw: int, min_tokens: int
) -> str | None:
    """Büyük ve ajanın hiç görmediği bir dosyanın tamamı istendiyse iskelet mesajı üretir."""
    if obs.tool is not Tool.READ or not obs.full_request or raw < min_tokens:
        return None
    if any(view.encoding is not Encoding.OUTLINE for view in views):
        return None  # ajan bu dosyadan içerik gördü; iskelet yerine delta ya da ham daha doğru
    entries = extract_outline(obs.label, obs.lines, obs.start_line)
    if entries is None or len(entries) < MIN_OUTLINE_ENTRIES:
        return None
    kept, omitted = limit_outline(entries, MAX_OUTLINE_ENTRIES)
    return outline_text(obs, kept, omitted, raw)


def unified_diff(old: Sequence[str], new: Sequence[str], first_line: int) -> tuple[str, ...]:
    """Başlıksız birleşik fark; hunk numaraları dosyadaki gerçek satırlara kaydırılır."""
    lines = list(difflib.unified_diff(list(old), list(new), lineterm="", n=DIFF_CONTEXT_LINES))
    return tuple(shift_hunk(line, first_line - 1) for line in lines[2:])


def shift_hunk(line: str, shift: int) -> str:
    """Pencereye göreli hunk başlığını dosya satır numaralarına çevirir."""
    match = HUNK_HEADER.match(line)
    if match is None or shift == 0:
        return line
    old_start = int(match.group(1)) + shift
    new_start = int(match.group(3)) + shift
    old_len = match.group(2) or ""
    new_len = match.group(4) or ""
    return f"@@ -{old_start}{old_len} +{new_start}{new_len} @@{line[match.end() :]}"


def subject(obs: Observation) -> str:
    """Mesajlarda sonucun neyi kapsadığını anlatan ifade."""
    if obs.tool is Tool.BASH:
        return f"the output of {obs.label} ({len(obs.lines)} lines)"
    if obs.whole:
        return f"{obs.label} (all {obs.total_lines} lines)"
    return f"lines {obs.start_line}-{last_line(obs.start_line, obs.lines)} of {obs.label}"


def retry_hint(obs: Observation) -> str:
    """Kaçış kuralını ajana anlatan ifade."""
    if obs.tool is Tool.BASH:
        return "run the exact same command again"
    return "repeat this exact Read call"


def ref_text(obs: Observation) -> str:
    """REF mesajı."""
    return (
        f"[CimriHook] REF: {subject(obs)} is identical to what you already received earlier in "
        "this conversation, so it was not sent again; that earlier result is still current. "
        f"If you can no longer see it, {retry_hint(obs)} to get the full raw result."
    )


def delta_text(obs: Observation, diff: Sequence[str]) -> str:
    """DELTA mesajı: açıklama satırı + birleşik fark."""
    header = (
        f"[CimriHook] DELTA: {subject(obs)} changed compared with the most recent full version "
        "you received earlier in this conversation. Only the differences are shown below as a "
        "unified diff (earlier -> now; '-' removed, '+' added); everything else is unchanged. "
        f"For the full raw result instead, {retry_hint(obs)}."
    )
    return "\n".join((header, *diff))


def outline_text(obs: Observation, entries: Sequence[OutlineEntry], omitted: int, raw: int) -> str:
    """OUTLINE mesajı: boyut bilgisi + bildirimler + nasıl devam edileceği."""
    scope = (
        f"{obs.total_lines} lines"
        if obs.whole
        else f"{obs.total_lines} lines; outline of lines "
        f"{obs.start_line}-{last_line(obs.start_line, obs.lines)}"
    )
    lines = [
        f"[CimriHook] OUTLINE (not the file content): {obs.label} is large ({scope}, "
        f"~{raw} tokens), so its full text was withheld to save context. "
        "Declarations (L<line>: code):",
        *(f"L{entry.line}: {entry.text}" for entry in entries),
    ]
    if omitted > 0:
        lines.append(f"... {omitted} nested declarations omitted; use Grep to locate them.")
    lines.append(
        "Read only the parts you need with offset/limit (e.g. offset=<line>, limit=80) or search "
        f"the file. For the full text anyway, {retry_hint(obs)}."
    )
    return "\n".join(lines)
