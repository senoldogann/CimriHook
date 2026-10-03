"""Maliyet anatomisi: kullanıcının kendi Claude Code kayıtlarında paranın nereye gittiği.

Her API isteği o ana kadarki konuşmayı yeniden okuduğundan bir oturumun maliyeti uzunluğuyla
karesel büyür. Rapor kayıtlardaki her gerçek isteği kendi kullanım verisiyle API liste fiyatından
fiyatlar ve maliyeti bu büyümenin göründüğü eksenlere ayırır: isteğin bağlam boyutu, token türü,
büyük önbellek yeniden yazımları ve olası nedenleri, statik önek, ana oturum ile alt ajanlar,
sıkıştırmalar. Önerilen sıkıştırma penceresinin etkisi simülatörden gelir ve A/B ile
doğrulanmamış bir tahmin olarak etiketlenir.
"""

import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from cimrihook.audit import (
    SECONDS_PER_DAY,
    Usage,
    average_write_weight,
    message_usage,
    parse_line,
    transcript_files,
)
from cimrihook.claude import JsonObject
from cimrihook.simulate import (
    SYNTHETIC_MODEL,
    CostOverrides,
    SimulationResult,
    claude_hint,
    claude_prices,
    compact_metadata_tokens,
    context_of,
    load_claude_trace,
    simulate_traces,
    total_usage,
    written_of,
)

BANDS: Final = ((100_000, "up to 100k"), (200_000, "100k-200k"), (400_000, "200k-400k"))
TOP_BAND: Final = "over 400k"
REWRITE_TOKENS: Final = 100_000  # tek istekte bundan fazla girdi yazan istek büyük yeniden yazımdır
FIVE_MINUTES: Final = 300.0
ONE_HOUR: Final = 3_600.0
# API liste fiyatları: taban girdi, USD / milyon token (2026-10, platform.claude.com fiyatları).
# Önbellek ve çıktı çarpanları simulate.claude_prices'tan gelir; listede olmayan modeller
# fiyatlandırılmaz ve raporda ayrıca sayılır.
USD_PER_MTOK: Final = (
    ("claude-opus-5", 4.0),
    ("claude-opus-4", 5.0),
    ("claude-sonnet-5", 2.0),
    ("claude-sonnet-4", 3.0),
    ("claude-haiku-4-5", 1.0),
)
# Eklentisiz, MCP'siz ve kullanıcı ayarsız Claude Code 2.1.288'in ilk istek bağlamı (A/B bench).
BARE_PREFIX_TOKENS: Final = 16_600
SESSION_START: Final = "session start"
AFTER_COMPACTION: Final = "after compaction"
IDLE_HOUR: Final = "idle over 1 hour (cache expired)"
IDLE_FIVE_MINUTES: Final = "idle 5-60 min on a 5-minute cache"
MODEL_SWITCH: Final = "model switch"
OTHER_BREAK: Final = "other cache break"
REWRITE_CAUSES: Final = (
    IDLE_HOUR,
    IDLE_FIVE_MINUTES,
    SESSION_START,
    AFTER_COMPACTION,
    MODEL_SWITCH,
    OTHER_BREAK,
)


@dataclass(frozen=True, slots=True)
class Request:
    """Tek gerçek API isteği ve oturum içindeki yeri."""

    timestamp: float | None  # epoch saniye; satırda zaman yoksa None
    model: str
    usage: Usage
    subagent: bool
    first: bool  # transcript'teki ilk istek
    after_compaction: bool  # sıkıştırmadan sonraki ilk istek
    model_switch: bool  # bir önceki istekten farklı model
    gap_seconds: float | None  # bir önceki istekten bu yana geçen süre


@dataclass(frozen=True, slots=True)
class TranscriptScan:
    """Bir transcript'teki istekler ve sıkıştırmaları tetikleyen bağlam boyutları."""

    requests: tuple[Request, ...]
    compaction_triggers: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class Share:
    """Bir kalemin adı, sayısı (istek ya da token) ve USD maliyeti."""

    label: str
    count: int
    usd: float


@dataclass(frozen=True, slots=True)
class Anatomy:
    """Raporun verisi."""

    transcripts: int
    days: int
    requests: int
    unpriced_requests: int
    unpriced_models: tuple[str, ...]
    total_usd: float
    main_usd: float
    subagent_usd: float
    bands: tuple[Share, ...]  # isteğin bağlam boyutuna göre
    token_types: tuple[Share, ...]  # sayı: token
    rewrites: tuple[Share, ...]  # büyük yeniden yazımlar, nedene göre; maliyet: yazılan kısım
    prefix_main: int | None  # oturumların ilk isteğindeki bağlamın medyanı
    prefix_subagent: int | None
    compactions: int
    compaction_trigger: int | None  # medyan
    after_compaction_context: int | None  # sıkıştırmadan sonraki ilk isteğin bağlamı, medyan


def scan_transcript(path: Path, subagent: bool) -> TranscriptScan:
    """Transcript'teki gerçek API istekleri (yerel sentetik mesajlar hariç) ve sıkıştırmalar.

    Aynı mesaj kimliği birden çok satıra yazılır; zaman ilk satırdan, kullanım son satırdan alınır.
    """
    order: list[str] = []
    times: dict[str, float | None] = {}
    models: dict[str, str] = {}
    usages: dict[str, Usage] = {}
    after: set[str] = set()
    triggers: list[int] = []
    awaiting = False
    with path.open("rb") as handle:
        for raw_line in handle:
            entry = parse_line(raw_line)
            if entry is None:
                continue
            if entry.get("type") == "system" and entry.get("subtype") == "compact_boundary":
                trigger = compact_metadata_tokens(entry, "preTokens")
                if trigger is not None:
                    triggers.append(trigger)
                awaiting = True
                continue
            message = entry.get("message")
            if entry.get("type") != "assistant" or not isinstance(message, dict):
                continue
            message_id = message.get("id")
            model = message.get("model")
            usage = message_usage(message)
            if (
                not isinstance(message_id, str)
                or not isinstance(model, str)
                or model == SYNTHETIC_MODEL
                or usage is None
            ):
                continue
            if message_id not in usages:
                order.append(message_id)
                times[message_id] = entry_time(entry)
                models[message_id] = model
                if awaiting:
                    after.add(message_id)
                    awaiting = False
            usages[message_id] = usage
    return TranscriptScan(
        requests=tuple(
            Request(
                timestamp=times[message_id],
                model=models[message_id],
                usage=usages[message_id],
                subagent=subagent,
                first=index == 0,
                after_compaction=message_id in after,
                model_switch=index > 0 and models[order[index - 1]] != models[message_id],
                gap_seconds=gap(times[order[index - 1]], times[message_id]) if index > 0 else None,
            )
            for index, message_id in enumerate(order)
        ),
        compaction_triggers=tuple(triggers),
    )


def entry_time(entry: JsonObject) -> float | None:
    """Transcript satırının zamanı (epoch saniye); yoksa None."""
    stamp = entry.get("timestamp")
    if not isinstance(stamp, str):
        return None
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()


def gap(previous: float | None, current: float | None) -> float | None:
    """İki istek arasındaki süre; zamanlardan biri yoksa None."""
    return None if previous is None or current is None else current - previous


def usd_per_token(model: str) -> float | None:
    """Modelin taban girdi liste fiyatı (USD/token); listede olmayan model için None."""
    for marker, usd in USD_PER_MTOK:
        if marker in model.lower():
            return usd / 1e6
    return None


def band(context: int) -> str:
    """İsteğin bağlam boyutunun bandı."""
    return next((label for limit, label in BANDS if context <= limit), TOP_BAND)


def rewrite_cause(request: Request) -> str | None:
    """Büyük bir önbellek yeniden yazımının olası nedeni; büyük yeniden yazım değilse None."""
    if written_of(request.usage) <= REWRITE_TOKENS:
        return None
    if request.first:
        return SESSION_START
    if request.after_compaction:
        return AFTER_COMPACTION
    if request.gap_seconds is not None and request.gap_seconds > ONE_HOUR:
        return IDLE_HOUR
    if (
        request.gap_seconds is not None
        and request.gap_seconds > FIVE_MINUTES
        and request.usage.write_1h == 0
    ):
        return IDLE_FIVE_MINUTES
    if request.model_switch:
        return MODEL_SWITCH
    return OTHER_BREAK


def token_costs(request: Request, base: float) -> tuple[float, float, float, float]:
    """İsteğin USD maliyeti token türüne göre: (önbellek okuma, yazma, önbelleksiz, çıktı)."""
    prices = claude_prices(request.model)
    usage = request.usage
    return (
        usage.read * prices.read * base,
        (usage.write_5m * prices.write_5m + usage.write_1h * prices.write_1h) * base,
        usage.uncached * prices.uncached * base,
        usage.output * prices.output * base,
    )


def build_anatomy(scans: Sequence[TranscriptScan], days: int) -> Anatomy:
    """Taranan transcript'lerden maliyet anatomisi."""
    requests = [request for scan in scans for request in scan.requests]
    priced = [
        (request, token_costs(request, base))
        for request in requests
        if (base := usd_per_token(request.model)) is not None
    ]
    total = sum(sum(costs) for _, costs in priced)
    labels = (*(label for _, label in BANDS), TOP_BAND)
    causes = [(rewrite_cause(request), costs) for request, costs in priced]
    firsts = [scan.requests[0] for scan in scans if scan.requests]
    after = [context_of(request.usage) for request in requests if request.after_compaction]
    triggers = [tokens for scan in scans for tokens in scan.compaction_triggers]
    usages = [request.usage for request, _ in priced]
    return Anatomy(
        transcripts=len(scans),
        days=days,
        requests=len(requests),
        unpriced_requests=len(requests) - len(priced),
        unpriced_models=tuple(
            sorted({request.model for request in requests if usd_per_token(request.model) is None})
        ),
        total_usd=total,
        main_usd=sum(sum(costs) for request, costs in priced if not request.subagent),
        subagent_usd=sum(sum(costs) for request, costs in priced if request.subagent),
        bands=tuple(
            Share(
                label,
                sum(1 for request, _ in priced if band(context_of(request.usage)) == label),
                sum(
                    sum(costs)
                    for request, costs in priced
                    if band(context_of(request.usage)) == label
                ),
            )
            for label in labels
        ),
        token_types=(
            Share(
                "cache read",
                sum(usage.read for usage in usages),
                sum(costs[0] for _, costs in priced),
            ),
            Share(
                "cache write",
                sum(usage.write_5m + usage.write_1h for usage in usages),
                sum(costs[1] for _, costs in priced),
            ),
            Share(
                "uncached input",
                sum(usage.uncached for usage in usages),
                sum(costs[2] for _, costs in priced),
            ),
            Share("output", sum(usage.output for usage in usages), sum(c[3] for _, c in priced)),
        ),
        rewrites=tuple(
            Share(
                cause,
                sum(1 for found, _ in causes if found == cause),
                sum(costs[1] + costs[2] for found, costs in causes if found == cause),
            )
            for cause in REWRITE_CAUSES
        ),
        prefix_main=median_or_none([context_of(r.usage) for r in firsts if not r.subagent]),
        prefix_subagent=median_or_none([context_of(r.usage) for r in firsts if r.subagent]),
        compactions=len(triggers),
        compaction_trigger=median_or_none(triggers),
        after_compaction_context=median_or_none(after),
    )


def median_or_none(values: Sequence[int]) -> int | None:
    """Değerlerin medyanı; değer yoksa None."""
    return int(statistics.median(values)) if values else None


def diagnose_claude(
    projects_dir: Path, days: int, now: float
) -> tuple[Anatomy, SimulationResult | None]:
    """Son `days` gündeki transcript'lerin anatomisi ve pencere politikalarının simülasyonu.

    Kayıtlarda hiç gerçek sıkıştırma yoksa sıkıştırmanın bedeli ölçülemez; simülasyon yapılmaz
    ve rapor bunu söyler.
    """
    files = transcript_files(projects_dir, now - days * SECONDS_PER_DAY)
    anatomy = build_anatomy(
        [scan_transcript(path, "subagents" in path.parts) for path in files], days
    )
    if anatomy.compactions == 0:
        return anatomy, None
    traces = [load_claude_trace(path) for path in files]
    requests = [usage for trace in traces for usage in trace.requests]
    simulation = simulate_traces(
        "Claude Code",
        traces,
        days,
        average_write_weight(total_usage(requests)),
        CostOverrides(None, None, None, 0, 0, None),
    )
    return anatomy, simulation


def percent(part: float, whole: float) -> str:
    """Yüzde metni."""
    return f"{100 * part / whole:.0f}%" if whole > 0 else "-"


def tokens_text(tokens: int | None) -> str:
    """Token sayısının kısa metni (ör. 34.6k, 1.25M, 7.31B)."""
    if tokens is None:
        return "-"
    if tokens < 1_000_000:
        return f"{tokens / 1000:.1f}k"
    return f"{tokens / 1e6:.2f}M" if tokens < 1_000_000_000 else f"{tokens / 1e9:.2f}B"


def render_doctor(anatomy: Anatomy, simulation: SimulationResult | None) -> str:
    """Raporun metni."""
    total = anatomy.total_usd
    unpriced = (
        f"; {anatomy.unpriced_requests:,} requests of {', '.join(anatomy.unpriced_models)} "
        "have no list price and are left out"
        if anatomy.unpriced_requests
        else ""
    )
    rewritten = sum(share.usd for share in anatomy.rewrites)
    lines = [
        f"CimriHook doctor (Claude Code, last {anatomy.days} days): {anatomy.transcripts:,} "
        f"transcripts, {anatomy.requests:,} API requests, ${total:,.0f} at API list prices"
        f"{unpriced}",
        f"  main sessions ${anatomy.main_usd:,.0f} ({percent(anatomy.main_usd, total)}), "
        f"subagents ${anatomy.subagent_usd:,.0f} ({percent(anatomy.subagent_usd, total)})",
        "Spend by the context size of the request (every request re-reads the whole conversation):",
        *(
            f"  {share.label:<12} ${share.usd:>10,.0f} {percent(share.usd, total):>5}"
            f"  {share.count:>8,} requests"
            for share in anatomy.bands
        ),
        "Spend by token type:",
        *(
            f"  {share.label:<15} ${share.usd:>10,.0f} {percent(share.usd, total):>5}"
            f"  {tokens_text(share.count):>9} tokens"
            for share in anatomy.token_types
        ),
        f"Cache rewrites over {REWRITE_TOKENS // 1000}k tokens in one request: "
        f"${rewritten:,.0f} ({percent(rewritten, total)}) re-caching context that was already "
        "paid for:",
        *(
            f"  {share.label:<36} {share.count:>5} req  ${share.usd:>8,.0f}"
            for share in anatomy.rewrites
            if share.count
        ),
        f"Static prefix (first request of a session): main {tokens_text(anatomy.prefix_main)}, "
        f"subagent {tokens_text(anatomy.prefix_subagent)} tokens; bare Claude Code is about "
        f"{tokens_text(BARE_PREFIX_TOKENS)} (plugins, skills, MCP servers and CLAUDE.md add the "
        "rest to every request)",
        f"Compactions: {anatomy.compactions:,}; median trigger "
        f"{tokens_text(anatomy.compaction_trigger)} tokens, next request "
        f"{tokens_text(anatomy.after_compaction_context)} tokens",
        *recommendations(anatomy, simulation),
    ]
    return "\n".join(lines)


def recommendations(anatomy: Anatomy, simulation: SimulationResult | None) -> list[str]:
    """Önerilen değişiklikler ve tahmini etkileri; tahminler doğrulanmamış olarak etiketlenir."""
    idle = next(share for share in anatomy.rewrites if share.label == IDLE_HOUR)
    lines = ["What would change it (estimates):"]
    if simulation is None:
        lines.append(
            "  compaction window: no real compaction in these logs, so its cost cannot be "
            "measured; run `cimrihook simulate` with --post-compact-tokens, "
            "--post-compact-cached and --summary-tokens"
        )
    else:
        baseline = simulation.outcomes[0].cost
        windows = [outcome for outcome in simulation.outcomes if outcome.policy.window]
        best = min(windows, key=lambda outcome: outcome.cost)
        window = best.policy.window
        if window is not None:
            lines.append(
                f"  compact earlier: '{best.policy.name}' would cost "
                f"{100 * (best.cost - baseline) / baseline:+.1f}% (simulation, not validated by "
                "an A/B run yet; `cimrihook bench-calibrate` measures its error). Apply with "
                f"{claude_hint(window)}"
            )
    if idle.count:
        lines.append(
            f"  ask before cold sends: ${idle.usd:,.0f} went to re-caching contexts after more "
            "than an hour idle; compacting first would re-cache a much smaller context"
        )
    if anatomy.prefix_main is not None and anatomy.prefix_main > BARE_PREFIX_TOKENS:
        lines.append(
            f"  trim the prefix: {tokens_text(anatomy.prefix_main - BARE_PREFIX_TOKENS)} tokens "
            "above bare Claude Code ride along on every main request and every re-cache"
        )
    return lines
