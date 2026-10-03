"""Bağlam ekonomisi simülatörü: kayıtlı oturumlarda sıkıştırma politikalarının maliyete etkisi.

Her API isteği o ana kadarki bağlamın tamamını yeniden okur; maliyetin büyük kısmı bu yüzden
bağlamın boyutundan gelir. Simülatör ajan kayıtlarındaki gerçek istek dizisini (API kullanım
verisi) alır ve sıkıştırma politikalarını aynı dizi üzerinde yeniden oynatır. Claude Code
transcript'leri ve Codex CLI rollout kayıtları desteklenir; maliyet her sağlayıcının fiyat
oranlarıyla taban girdi fiyatı cinsinden hesaplanır. Sıkıştırmanın bedeli modele dahildir:
özetleme isteği bağlamı bir kez okur, özet çıktı fiyatıyla üretilir, ajan ardından bir miktar
içeriği birkaç ek istekle yeniden okur. Ajanın davranışının bunun dışında değişmediği
varsayılır; kalite etkisi A/B deneyiyle ölçülmelidir.
"""

import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
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
from cimrihook.errors import ConfigError

COLD_WRITE_SHARE: Final = 0.5  # girdinin yarısından fazlası yeniden yazıldıysa önbellek soğuktur
CLAUDE_MIN_COMPACT_WINDOW: Final = 100_000  # CLAUDE_CODE_AUTO_COMPACT_WINDOW belgelenmiş alt sınırı


@dataclass(frozen=True, slots=True)
class PriceSheet:
    """Sağlayıcının fiyat oranları (taban girdi fiyatı = 1)."""

    name: str
    read: float
    write_5m: float
    write_1h: float
    uncached: float
    output: float


# Anthropic: önbellek okuma 0.1, yazma 1.25 (5 dk) / 2.0 (1 saat), çıktı 5 kat.
ANTHROPIC: Final = PriceSheet("Anthropic", 0.1, 1.25, 2.0, 1.0, 5.0)
# OpenAI GPT-5.x/6.x: önbellekli girdi %90 indirimli; 5.6 ve sonrası önbellek yazımını 1.25 katla
# faturalar (eski modeller yazım raporlamaz); çıktı yaklaşık 6 kat (GPT-5.5: 6, GPT-5.6-sol: 5).
OPENAI: Final = PriceSheet("OpenAI", 0.1, 1.25, 1.25, 1.0, 6.0)


@dataclass(frozen=True, slots=True)
class SessionTrace:
    """Tek bağlam penceresinin istek dizisi ve gerçek sıkıştırmalarının sonucu."""

    requests: tuple[Usage, ...]
    post_compact_tokens: tuple[int, ...]  # gerçek sıkıştırmalardan sonraki bağlam boyutları


@dataclass(frozen=True, slots=True)
class Policy:
    """Sıkıştırma politikası."""

    name: str
    window: int | None  # bağlam bu boyutu aşınca sıkıştır; None: gözlenen davranış
    cold_window: int | None  # önbellek soğukken bağlam bu boyutu aşıyorsa önce sıkıştır


@dataclass(frozen=True, slots=True)
class CostModel:
    """Simülasyon varsayımları."""

    prices: PriceSheet
    write_weight: float  # simülasyonda yeni yazılan girdinin ortalama çarpanı
    post_compact_tokens: int  # sıkıştırma sonrası bağlam boyutu
    summary_tokens: int  # özetin çıktı tokenı
    refetch_tokens: int  # sıkıştırma sonrası ajanın yeniden okuduğu içerik
    refetch_requests: int  # bu yeniden okuma için ek istek sayısı


@dataclass(frozen=True, slots=True)
class Outcome:
    """Bir politikanın tüm oturumlardaki sonucu."""

    policy: Policy
    cost: float
    compactions: int
    mean_context: float


@dataclass(frozen=True, slots=True)
class SimulationResult:
    """Simülasyon raporunun verisi."""

    agent: str
    sessions: int
    requests: int
    days: int
    exact_cost: float  # kullanım kayıtlarından birebir hesaplanan maliyet
    observed_compactions: int
    static_prefix: int  # oturumların ilk isteğindeki bağlamın medyanı (sistem istemi + araçlar)
    model: CostModel
    outcomes: tuple[Outcome, ...]


type ApplyHint = Callable[[int, int], str]

POLICIES: Final = (
    Policy("observed behaviour", None, None),
    Policy("compact above 400k", 400_000, None),
    Policy("compact above 300k", 300_000, None),
    Policy("compact above 200k", 200_000, None),
    Policy("compact above 150k", 150_000, None),
    Policy("compact above 100k", 100_000, None),
    Policy("cold-cache compact above 80k", None, 80_000),
    Policy("above 200k + cold above 80k", 200_000, 80_000),
)


def simulate_claude(
    logs_dir: Path,
    days: int,
    now: float,
    summary_tokens: int,
    refetch_tokens: int,
    refetch_requests: int,
    post_compact_override: int | None,
    read_weight: float | None,
) -> SimulationResult:
    """Claude Code transcript'leri üzerinde politikaları çalıştırır.

    read_weight verilirse önbellek okuma çarpanını değiştirir (Opus 5.5: 0.05, Fable 5.1: 0.025).
    """
    files = transcript_files(logs_dir, now - days * SECONDS_PER_DAY)
    traces = [load_claude_trace(path) for path in files]
    requests = [usage for trace in traces for usage in trace.requests]
    return simulate_traces(
        "Claude Code",
        traces,
        days,
        ANTHROPIC if read_weight is None else replace(ANTHROPIC, read=read_weight),
        average_write_weight(total_usage(requests)),
        summary_tokens,
        refetch_tokens,
        refetch_requests,
        post_compact_override,
    )


def simulate_codex(
    logs_dir: Path,
    days: int,
    now: float,
    summary_tokens: int,
    refetch_tokens: int,
    refetch_requests: int,
    post_compact_override: int | None,
    read_weight: float | None,
) -> SimulationResult:
    """Codex CLI rollout kayıtları üzerinde politikaları çalıştırır."""
    min_mtime = now - days * SECONDS_PER_DAY
    files = sorted(
        path for path in logs_dir.rglob("rollout-*.jsonl") if path.stat().st_mtime >= min_mtime
    )
    return simulate_traces(
        "Codex CLI",
        [load_codex_trace(path) for path in files],
        days,
        OPENAI if read_weight is None else replace(OPENAI, read=read_weight),
        OPENAI.uncached,
        summary_tokens,
        refetch_tokens,
        refetch_requests,
        post_compact_override,
    )


def simulate_traces(
    agent: str,
    traces: Sequence[SessionTrace],
    days: int,
    prices: PriceSheet,
    write_weight: float,
    summary_tokens: int,
    refetch_tokens: int,
    refetch_requests: int,
    post_compact_override: int | None,
) -> SimulationResult:
    """Sağlayıcıdan bağımsız çekirdek: tüm politikaları aynı istek dizileri üzerinde oynatır."""
    used = [trace for trace in traces if trace.requests]
    requests = [usage for trace in used for usage in trace.requests]
    observed = [tokens for trace in used for tokens in trace.post_compact_tokens]
    model = CostModel(
        prices=prices,
        write_weight=write_weight,
        post_compact_tokens=post_compact_size(observed, post_compact_override),
        summary_tokens=summary_tokens,
        refetch_tokens=refetch_tokens,
        refetch_requests=refetch_requests,
    )
    return SimulationResult(
        agent=agent,
        sessions=len(used),
        requests=len(requests),
        days=days,
        exact_cost=sum(exact_cost(usage, prices) for usage in requests),
        observed_compactions=len(observed),
        static_prefix=int(statistics.median(context_of(trace.requests[0]) for trace in used)),
        model=model,
        outcomes=tuple(run_policy(used, policy, model) for policy in POLICIES),
    )


def load_claude_trace(path: Path) -> SessionTrace:
    """Claude Code transcript'i: mesaj kimliği başına son kullanım ve compact_boundary boyutları."""
    order: list[str] = []
    usages: dict[str, Usage] = {}
    post: list[int] = []
    with path.open("rb") as handle:
        for raw_line in handle:
            entry = parse_line(raw_line)
            if entry is None:
                continue
            if entry.get("type") == "system" and entry.get("subtype") == "compact_boundary":
                tokens = post_compact_tokens(entry)
                if tokens is not None:
                    post.append(tokens)
                continue
            message = entry.get("message")
            if entry.get("type") != "assistant" or not isinstance(message, dict):
                continue
            message_id = message.get("id")
            usage = message_usage(message)
            if not isinstance(message_id, str) or usage is None:
                continue
            if message_id not in usages:
                order.append(message_id)
            usages[message_id] = usage  # aynı kimliğin son satırı geçerlidir
    return SessionTrace(tuple(usages[message_id] for message_id in order), tuple(post))


def load_codex_trace(path: Path) -> SessionTrace:
    """Codex rollout kaydından istek dizisi (token_count) ve sıkıştırmalar (compacted).

    Sıkıştırma sonrası boyut, compacted kaydından sonraki ilk isteğin bağlamıdır.
    """
    requests: list[Usage] = []
    post: list[int] = []
    last_total: int | None = None
    after_compaction = False
    with path.open("rb") as handle:
        for raw_line in handle:
            entry = parse_line(raw_line)
            if entry is None:
                continue
            if entry.get("type") == "compacted":
                after_compaction = True
                continue
            request = codex_request(entry)
            if request is None or request[1] == last_total:
                continue  # aynı kümülatif toplam: yeni istek yok
            usage, last_total = request
            if after_compaction:
                post.append(context_of(usage))
                after_compaction = False
            requests.append(usage)
    return SessionTrace(tuple(requests), tuple(post))


def codex_request(entry: JsonObject) -> tuple[Usage, int] | None:
    """token_count olayından (son isteğin kullanımı, kümülatif toplam)."""
    payload = entry.get("payload")
    if entry.get("type") != "event_msg" or not isinstance(payload, dict):
        return None
    info = payload.get("info")
    if payload.get("type") != "token_count" or not isinstance(info, dict):
        return None
    last = info.get("last_token_usage")
    total = info.get("total_token_usage")
    if not isinstance(last, dict) or not isinstance(total, dict):
        return None
    tokens = last.get("input_tokens")
    cached = last.get("cached_input_tokens")
    written = last.get("cache_write_input_tokens")
    output = last.get("output_tokens")
    cumulative = total.get("total_tokens")
    if not (
        isinstance(tokens, int)
        and isinstance(cached, int)
        and isinstance(output, int)
        and isinstance(cumulative, int)
    ):
        return None
    # input_tokens okunan ve yazılan önbellek tokenlarını içerir; eski kayıtlarda yazım alanı yok.
    write = written if isinstance(written, int) else 0
    usage = Usage(
        uncached=tokens - cached - write, write_5m=write, write_1h=0, read=cached, output=output
    )
    return usage, cumulative


def post_compact_tokens(entry: JsonObject) -> int | None:
    """compact_boundary satırındaki sıkıştırma sonrası bağlam boyutu."""
    metadata = entry.get("compactMetadata")
    if not isinstance(metadata, dict):
        return None
    tokens = metadata.get("postTokens")
    return tokens if isinstance(tokens, int) and tokens > 0 else None


def post_compact_size(observed: Sequence[int], override: int | None) -> int:
    """Sıkıştırma sonrası bağlam: verilmişse o, yoksa gözlenen sıkıştırmaların medyanı."""
    if override is not None:
        return override
    if not observed:
        raise ConfigError(
            "no real compactions found in the logs; pass --post-compact-tokens explicitly"
        )
    return int(statistics.median(observed))


def total_usage(requests: Sequence[Usage]) -> Usage:
    """İsteklerin toplam kullanımı."""
    return Usage(
        uncached=sum(usage.uncached for usage in requests),
        write_5m=sum(usage.write_5m for usage in requests),
        write_1h=sum(usage.write_1h for usage in requests),
        read=sum(usage.read for usage in requests),
        output=sum(usage.output for usage in requests),
    )


def context_of(usage: Usage) -> int:
    """İstekte modele giden toplam girdi."""
    return usage.uncached + usage.write_5m + usage.write_1h + usage.read


def written_of(usage: Usage) -> int:
    """İstekte önbellekten okunmayan (yeni yazılan ya da önbelleksiz) girdi."""
    return usage.uncached + usage.write_5m + usage.write_1h


def is_cold(usage: Usage) -> bool:
    """Önbellek soğumuş mu (bağlamın çoğu yeniden yazıldı mı)?"""
    return written_of(usage) > COLD_WRITE_SHARE * context_of(usage)


def exact_cost(usage: Usage, prices: PriceSheet) -> float:
    """Kullanım kaydından birebir maliyet (taban girdi fiyatı cinsinden)."""
    return (
        usage.uncached * prices.uncached
        + usage.write_5m * prices.write_5m
        + usage.write_1h * prices.write_1h
        + usage.read * prices.read
        + usage.output * prices.output
    )


def should_compact(policy: Policy, context: int, cold: bool, model: CostModel) -> bool:
    """Politika bu istekten önce sıkıştırma istiyor mu?"""
    if context <= model.post_compact_tokens + model.refetch_tokens:
        return False  # sıkıştırma bağlamı küçültmez
    if policy.window is not None and context > policy.window:
        return True
    return cold and policy.cold_window is not None and context > policy.cold_window


def simulate_trace(trace: SessionTrace, policy: Policy, model: CostModel) -> tuple[float, int, int]:
    """Bir oturumu politikayla yeniden oynatır: (maliyet, sıkıştırma sayısı, bağlam toplamı)."""
    prices = model.prices
    cost = 0.0
    compactions = 0
    context_sum = 0
    simulated = 0
    previous_actual = 0
    for index, usage in enumerate(trace.requests):
        actual = context_of(usage)
        growth = actual - previous_actual
        previous_actual = actual
        if index == 0:
            simulated = actual
        elif growth < 0:
            simulated = min(simulated, actual)  # gerçek oturum da burada küçüldü
        else:
            simulated += growth
        cold = is_cold(usage)
        fresh = cold
        if should_compact(policy, simulated, cold, model):
            read_weight = prices.uncached if cold else prices.read
            cost += simulated * read_weight + model.summary_tokens * prices.output
            simulated = model.post_compact_tokens + model.refetch_tokens
            cost += model.refetch_requests * simulated * prices.read
            compactions += 1
            fresh = True
        written = simulated if fresh else min(written_of(usage), simulated)
        cost += (
            written * model.write_weight
            + (simulated - written) * prices.read
            + usage.output * prices.output
        )
        context_sum += simulated
    return cost, compactions, context_sum


def run_policy(traces: Sequence[SessionTrace], policy: Policy, model: CostModel) -> Outcome:
    """Politikayı tüm oturumlarda çalıştırıp toplar."""
    results = [simulate_trace(trace, policy, model) for trace in traces]
    requests = sum(len(trace.requests) for trace in traces)
    return Outcome(
        policy=policy,
        cost=sum(cost for cost, _, _ in results),
        compactions=sum(count for _, count, _ in results),
        mean_context=sum(total for _, _, total in results) / requests if requests else 0.0,
    )


def claude_hint(window: int, static_prefix: int) -> str:
    """Claude Code pencereyi sistem istemi hariç uygular; belgelenmiş alt sınır korunur."""
    setting = max(window - static_prefix, CLAUDE_MIN_COMPACT_WINDOW)
    return f"`cimrihook settings --compact-window {setting}`"


def codex_hint(window: int, static_prefix: int) -> str:
    """Codex sıkıştırma eşiğini toplam bağlam üzerinden uygular."""
    return f"`model_auto_compact_token_limit = {window}` in ~/.codex/config.toml"


def render_simulation(result: SimulationResult, hint: ApplyHint) -> str:
    """Simülasyon raporunun metni."""
    model = result.model
    baseline = result.outcomes[0].cost
    calibration = 100 * (baseline - result.exact_cost) / result.exact_cost
    lines = [
        f"CimriHook simulate ({result.agent}, {model.prices.name} prices): {result.sessions} "
        f"sessions, {result.requests:,} requests, last {result.days} days",
        f"Cost model (base input price = 1): read {model.prices.read}, write "
        f"{model.write_weight:.2f}, output {model.prices.output:.0f}; after compaction "
        f"{model.post_compact_tokens:,} context tokens (median of {result.observed_compactions} "
        f"real compactions) + {model.refetch_tokens:,} re-read in {model.refetch_requests} extra "
        f"requests; summary {model.summary_tokens:,} output tokens",
        f"Exact cost from usage logs: {result.exact_cost / 1e9:.3f}B; model replay of observed "
        f"behaviour: {baseline / 1e9:.3f}B (calibration {calibration:+.1f}%)",
        f"  {'policy':<30}{'cost (B)':>10}{'vs observed':>13}{'compactions':>13}"
        f"{'mean context':>14}",
    ]
    lines.extend(
        f"  {outcome.policy.name:<30}{outcome.cost / 1e9:>10.3f}"
        f"{100 * (outcome.cost - baseline) / baseline:>12.1f}%{outcome.compactions:>13,}"
        f"{outcome.mean_context:>14,.0f}"
        for outcome in result.outcomes
    )
    return "\n".join([*lines, *recommendation(result, hint)])


def recommendation(result: SimulationResult, hint: ApplyHint) -> list[str]:
    """En düşük simüle maliyetli pencere politikası ve nasıl uygulanacağı."""
    windows = [
        (outcome, outcome.policy.window)
        for outcome in result.outcomes
        if outcome.policy.window is not None
    ]
    if not windows:
        return []
    best, window = min(windows, key=lambda pair: pair[0].cost)
    return [
        f"Lowest simulated cost with a window: {best.policy.name}. Apply it with "
        f"{hint(window, result.static_prefix)} and confirm task quality with an A/B run first.",
    ]
