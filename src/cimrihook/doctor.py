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
from pathlib import Path
from typing import Final

from cimrihook.audit import (
    SECONDS_PER_DAY,
    average_write_weight,
    bench_transcripts,
    recent_transcripts,
)
from cimrihook.errors import TranscriptError
from cimrihook.install import MARKER, WINDOW_ENV, env_of, hooks_of, is_ours, load_settings
from cimrihook.lifetime import (
    MAIN,
    LifetimeReplay,
    recommended_lifetime,
    replay_lifetimes,
)
from cimrihook.scan import (
    Request,
    TranscriptScan,
    recent_requests,
    scan_transcript,
    token_costs,
    unique_scans,
)
from cimrihook.simulate import (
    MEASURED_REFETCH_REQUESTS,
    MEASURED_REFETCH_TOKENS,
    CostOverrides,
    SimulationResult,
    claude_hint,
    context_of,
    dollar_weight,
    load_claude_traces,
    recommendation_text,
    recommended_window,
    simulate_traces,
    total_usage,
    usd_per_token,
    written_of,
)
from cimrihook.tail import FIVE_MINUTES, ONE_HOUR, read_session_tail

BANDS: Final = ((100_000, "up to 100k"), (200_000, "100k-200k"), (400_000, "200k-400k"))
TOP_BAND: Final = "over 400k"
REWRITE_TOKENS: Final = 100_000  # tek istekte bundan fazla girdi yazan istek büyük yeniden yazımdır
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
    firsts = [request for request in requests if request.first]
    after = [context_of(request.usage) for request in requests if request.after_compaction]
    triggers = [compaction.trigger for scan in scans for compaction in scan.compactions]
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


@dataclass(frozen=True, slots=True)
class Diagnosis:
    """doctor raporunun verisi."""

    anatomy: Anatomy
    simulation: SimulationResult | None  # kayıtlarda gerçek sıkıştırma yoksa ölçülemez
    guard_check: str
    bench_transcripts: int  # dışarıda bırakılan CimriHook A/B çalıştırma transcript'leri
    setup: str  # ayar dosyasındaki pencere, koruma ve durum satırı
    lifetimes: tuple[LifetimeReplay, LifetimeReplay]  # ana oturumlar ve alt ajanlar


def diagnose_claude(projects_dir: Path, settings_path: Path, days: int, now: float) -> Diagnosis:
    """Son `days` gündeki istekler üzerinde maliyet anatomisi, pencere politikalarının
    simülasyonu ve soğuk istem korumasının öz denetimi.

    Anatomi yalnızca penceredeki istekleri sayar; çatallanmış ya da sürdürülmüş oturumların
    kopyaladığı istekler ve sıkıştırmalar bir kez sayılır. Simülasyon pencerede etkin oturumları
    bütün olarak yeniden oynatır. Kayıtlarda hiç gerçek sıkıştırma yoksa sıkıştırmanın bedeli
    ölçülemez; simülasyon yapılmaz ve rapor bunu söyler.
    """
    files = recent_transcripts(projects_dir, days, now)
    scans = recent_requests(
        unique_scans([scan_transcript(path, "subagents" in path.parts) for path in files]),
        now - days * SECONDS_PER_DAY,
    )
    anatomy = build_anatomy(scans, days)
    guard = guard_check(files)
    bench = bench_transcripts(projects_dir, days, now)
    setup = setup_line(settings_path)
    lifetimes = replay_lifetimes(scans)
    if anatomy.compactions == 0:
        return Diagnosis(anatomy, None, guard, bench, setup, lifetimes)
    traces = load_claude_traces(files)
    requests = [usage for trace in traces for usage in trace.requests]
    simulation = simulate_traces(
        "Claude Code",
        traces,
        days,
        average_write_weight(total_usage(requests)),
        CostOverrides(None, None, None, MEASURED_REFETCH_TOKENS, MEASURED_REFETCH_REQUESTS, None),
        dollar_weight,
    )
    return Diagnosis(anatomy, simulation, guard, bench, setup, lifetimes)


def setup_line(settings_path: Path) -> str:
    """Ayar dosyasında CimriHook'un neyi etkin: sıkıştırma penceresi, koruma, durum satırı."""
    settings = load_settings(settings_path)
    override = env_of(settings).get(WINDOW_ENV)
    window = settings.get("autoCompactWindow")
    if override is not None:
        compaction = f"{WINDOW_ENV}={override} (overrides every window setting)"
    elif isinstance(window, int) and not isinstance(window, bool):
        compaction = (
            f"autoCompactWindow {window} (compacts at about {tokens_text(window - 33_000)} on "
            "models with a larger context window)"
        )
    else:
        compaction = "Claude Code's default (about 33k below each model's context window)"
    guard = any(
        f"{MARKER}guard" in command
        for entry in hooks_of(settings).get("UserPromptSubmit", [])
        for command in hook_commands(entry)
    )
    status = is_ours(settings.get("statusLine"))
    return (
        f"Setup ({settings_path}): compaction window {compaction}; cold-prompt guard "
        f"{'on' if guard else 'off'}; status line {'on' if status else 'off'}"
    )


def hook_commands(entry: object) -> list[str]:
    """Bir hook grubundaki komut metinleri."""
    handlers = entry.get("hooks") if isinstance(entry, dict) else None
    if not isinstance(handlers, list):
        return []
    return [
        str(handler.get("command"))
        for handler in handlers
        if isinstance(handler, dict) and isinstance(handler.get("command"), str)
    ]


def guard_check(files: Sequence[Path]) -> str:
    """Soğuk istem korumasının en yeni ana oturumun önbellek durumunu okuyabildiği: Claude Code
    kayıt biçimini değiştirirse koruma ya hata verir ya da sessizce devre dışı kalır."""
    mains = [path for path in files if "subagents" not in path.parts]
    if not mains:
        return "Guard check: no main session transcript in these days"
    newest = mains[-1]
    try:
        tail = read_session_tail(str(newest))
    except TranscriptError as error:
        return f"Guard check: FAILS on the newest session: {error}"
    if tail is None:
        return (
            f"Guard check: the newest session ({newest.stem}) has no cache write in its last 4 MiB "
            "or was just compacted, so the guard stays quiet there"
        )
    cache = "1-hour" if tail.ttl_seconds >= ONE_HOUR else "5-minute"
    return (
        f"Guard check: reads the newest session's cache state ({cache} cache, "
        f"{tokens_text(tail.context_tokens)} tokens of context)"
    )


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


def render_doctor(diagnosis: Diagnosis) -> str:
    """Raporun metni."""
    anatomy = diagnosis.anatomy
    simulation = diagnosis.simulation
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
        f"{unpriced}"
        + (
            f"; {diagnosis.bench_transcripts:,} transcripts of CimriHook's A/B runs left out"
            if diagnosis.bench_transcripts
            else ""
        ),
        bottom_line(anatomy, simulation),
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
        "Cache lifetime (each request re-priced with your own pauses; cache input only):",
        *(lifetime_line(replay) for replay in diagnosis.lifetimes if replay.requests),
        *recommendations(anatomy, simulation),
        *lifetime_recommendations(diagnosis.lifetimes),
        diagnosis.setup,
        diagnosis.guard_check,
    ]
    return "\n".join(lines)


def bottom_line(anatomy: Anatomy, simulation: SimulationResult | None) -> str:
    """Raporun tek sayısı: harcamanın ne kadarı önlenebilirdi (tahmin) ve nasıl."""
    idle = next(share for share in anatomy.rewrites if share.label == IDLE_HOUR)
    found = None if simulation is None else recommended_window(simulation)
    chosen = None if found is None else found[0]
    window = None if chosen is None else chosen.policy.window
    if simulation is None or chosen is None or window is None:
        return (
            f"Bottom line: ${idle.usd:,.0f} went to re-caching sessions after an hour idle; no "
            "compaction window lowers the simulated cost of these logs"
        )
    saving = simulation.outcomes[0].cost - chosen.cost
    share = saving / simulation.outcomes[0].cost
    return (
        f"Bottom line: about ${saving:,.0f} ({100 * share:.0f}%) was avoidable by compacting "
        f"above {tokens_text(window)} (simulated), plus ${idle.usd:,.0f} of re-caching after an "
        f"hour idle; apply with {claude_hint(window)}"
    )


def lifetime_line(replay: LifetimeReplay) -> str:
    """Bir grubun iki ömürle maliyeti ve yeniden oynatmanın hata payı."""
    error = replay.replay_error()
    now = replay.current or "no cache writes"
    base = replay.replayed(replay.current) if replay.current is not None else 0.0
    parts = [
        f"{lifetime} ${replay.replayed(lifetime):,.0f}"
        + (
            f" ({100 * (replay.replayed(lifetime) - base) / base:+.0f}%)"
            if base > 0 and lifetime != replay.current
            else ""
        )
        for lifetime in ("5m", "1h")
    ]
    check = "" if error is None else f"; replay check {100 * error:+.1f}%"
    return f"  {replay.bucket:<14} {now:>3} now: {', '.join(parts)}{check}"


def lifetime_recommendations(lifetimes: Sequence[LifetimeReplay]) -> list[str]:
    """Önbellek ömrü önerileri: yalnızca diğer ömür hata payından fazla ucuzsa."""
    lines: list[str] = []
    for replay in lifetimes:
        better = recommended_lifetime(replay)
        if better is None or replay.current is None:
            continue
        flag = "--cache-ttl" if replay.bucket == MAIN else "--subagent-cache-ttl"
        saving = 1 - replay.replayed(better) / replay.replayed(replay.current)
        lines.append(
            f"  cache lifetime: {replay.bucket} would spend {100 * saving:.0f}% less on cache "
            f"input with {better} caches. Apply with `cimrihook init {flag} {better}`"
        )
    return lines


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
        found = recommended_window(simulation)
        window = None if found is None else found[0].policy.window
        if found is None or window is None:
            lines.append("  compaction window: no window lowers the simulated cost of these logs")
        else:
            chosen, cheapest = found
            lines.append(
                "  compact earlier: "
                f"{recommendation_text(chosen, cheapest, simulation.outcomes[0].cost)} "
                f"(simulation with {MEASURED_REFETCH_TOKENS // 1000}k tokens re-read after each "
                "compaction; the A/B runs so far found the simulator a few points optimistic, see "
                f"`cimrihook bench-calibrate`). Apply with {claude_hint(window)}"
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
