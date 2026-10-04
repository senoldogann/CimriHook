"""Bağlam ekonomisi simülatörü: kayıtlı oturumlarda sıkıştırma politikalarının maliyete etkisi.

Her API isteği o ana kadarki bağlamın tamamını yeniden okur; maliyetin büyük kısmı bu yüzden
bağlamın boyutundan gelir. Simülatör ajan kayıtlarındaki gerçek istek dizisini (API kullanım
verisi) alır ve sıkıştırma politikalarını aynı dizi üzerinde yeniden oynatır. Claude Code
transcript'leri ve Codex CLI rollout kayıtları desteklenir; maliyet her oturumun modelinin fiyat
oranlarıyla taban girdi fiyatı cinsinden hesaplanır.

Sıkıştırmanın bedeli kayıtlardaki gerçek sıkıştırmalardan ölçülür: özetleme isteği bağlamı bir kez
okur ve özeti çıktı fiyatıyla üretir; ardından gelen ilk istek sistem istemi, araçlar, özet ve
yeniden eklenen dosyalarla yeni bağlamı taşır ve bunun yalnızca önbellekte kalmayan kısmı yeniden
yazılır. Ajanın davranışının bunun dışında değişmediği varsayılır; bu varsayım ve kalite etkisi
A/B deneyiyle sınanır (`cimrihook bench-calibrate`).
"""

import statistics
from collections import Counter
from collections.abc import Callable, Sequence, Set
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

from cimrihook.audit import (
    BENCH_PROJECT_MARKER,
    SECONDS_PER_DAY,
    Usage,
    average_write_weight,
    message_usage,
    parse_line,
    recent_transcripts,
)
from cimrihook.claude import JsonObject
from cimrihook.codec import estimate_tokens
from cimrihook.errors import ConfigError, TranscriptError

# Önbellek soğuk: istek bir önceki bağlamın yarısından azını önbellekten okuduysa. Büyük yeni içerik
# eklenen sıcak istek soğuk sayılmaz (okunan kısım önceki bağlamın tamamıdır).
COLD_READ_SHARE: Final = 0.5
# Sıkıştırmadan sonra ajanın yeniden okuduğu içerik: A/B çalıştırmalarında simülatörün ölçülen
# maliyetin altında kaldığı payı açıklayan yeniden okuma, sıkıştırma başına medyan (0-13k arası).
MEASURED_REFETCH_TOKENS: Final = 7_000
MEASURED_REFETCH_REQUESTS: Final = 1
# Önerilen pencere, en düşük simüle maliyetin bu kadar puan yakınındaki en büyük penceredir. Her
# sıkıştırma ayrıntıyı özete indirir; neredeyse aynı tasarruf için daha az sıkıştırma daha az risk.
RECOMMENDATION_SLACK: Final = 0.01
CLAUDE_MIN_COMPACT_WINDOW: Final = 100_000  # autoCompactWindow ve ortam değişkeninin alt sınırı
CLAUDE_MAX_COMPACT_WINDOW: Final = 1_000_000  # autoCompactWindow ayarının üst sınırı
# Claude Code 2.1.288 otomatik sıkıştırmayı pencere − min(çıktı sınırı, 20000) − 13000 tokenlık
# bağlamda tetikler; bugünkü modellerin çıktı sınırı 20000'in üstündedir.
CLAUDE_COMPACT_OFFSET: Final = 33_000
SYNTHETIC_MODEL: Final = "<synthetic>"  # Claude Code'un API'ye gitmeyen yerel mesajları


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
# Opus 5.x önbellek okumayı 0.05, Fable 5.1 0.025 çarpanıyla fiyatlar; diğer Claude modelleri 0.1.
CLAUDE_READ_WEIGHTS: Final = (("claude-opus-5", 0.05), ("claude-fable", 0.025))
# OpenAI GPT-5.x/6.x: önbellekli girdi %90 indirimli; 5.6 ve sonrası önbellek yazımını 1.25 katla
# faturalar (eski modeller yazım raporlamaz); çıktı yaklaşık 6 kat (GPT-5.5: 6, GPT-5.6-sol: 5).
OPENAI: Final = PriceSheet("OpenAI", 0.1, 1.25, 1.25, 1.0, 6.0)


@dataclass(frozen=True, slots=True)
class SessionTrace:
    """Tek bağlam penceresinin istek dizisi, modeli ve gerçek sıkıştırmaları."""

    requests: tuple[Usage, ...]
    model: str  # isteklerin çoğunu yanıtlayan model; bilinmiyorsa boş
    prices: PriceSheet  # modelin fiyat oranları
    pre_compact_tokens: tuple[int, ...]  # sıkıştırmayı tetikleyen bağlam boyutları
    post_compact_tokens: tuple[int, ...]  # sıkıştırmadan sonraki ilk isteğin bağlamı
    post_compact_cached: tuple[int, ...]  # o isteğin önbellekten okunan kısmı
    summary_tokens: tuple[int, ...]  # özetin çıktı tokenı (Claude: özet metninden tahmin)


@dataclass(frozen=True, slots=True)
class Policy:
    """Sıkıştırma politikası."""

    name: str
    window: int | None  # bağlam bu boyutu aşınca sıkıştır; None: gözlenen davranış
    cold_window: int | None  # önbellek soğukken bağlam bu boyutu aşıyorsa önce sıkıştır


@dataclass(frozen=True, slots=True)
class CostModel:
    """Sıkıştırmanın bedeli için simülasyon varsayımları (fiyatlar oturumun modelinden gelir)."""

    write_weight: float  # simülasyonda yeni yazılan girdinin ortalama çarpanı
    post_compact_tokens: int  # sıkıştırmadan sonraki ilk isteğin bağlamı
    post_compact_cached: int  # o bağlamın önbellekte kalan kısmı (sistem istemi ve araçlar)
    summary_tokens: int  # özetin çıktı tokenı
    refetch_tokens: int  # sıkıştırma sonrası ajanın ayrıca yeniden okuduğu içerik
    refetch_requests: int  # bu yeniden okuma için ek istek sayısı


@dataclass(frozen=True, slots=True)
class CostOverrides:
    """Kullanıcının verdiği varsayımlar; None olanlar kayıtlardaki sıkıştırmalardan ölçülür."""

    post_compact_tokens: int | None
    post_compact_cached: int | None
    summary_tokens: int | None
    refetch_tokens: int
    refetch_requests: int
    read_weight: float | None  # tüm modellerin önbellek okuma çarpanını değiştirir


@dataclass(frozen=True, slots=True)
class Outcome:
    """Bir politikanın tüm oturumlardaki sonucu."""

    policy: Policy
    cost: float
    compactions: int
    mean_context: float


@dataclass(frozen=True, slots=True)
class ModelShare:
    """Simülasyondaki bir modelin okuma çarpanı ve oturum sayısı."""

    model: str
    read: float
    sessions: int


@dataclass(frozen=True, slots=True)
class SimulationResult:
    """Simülasyon raporunun verisi."""

    agent: str
    sessions: int
    requests: int
    days: int
    unit: str  # USD ya da taban girdi birimi
    unweighted_sessions: int  # birimi çevrilemediği için dışarıda kalan oturumlar
    exact_cost: float  # kullanım kayıtlarından birebir hesaplanan maliyet
    observed_compactions: int
    static_prefix: int  # oturumların ilk isteğindeki bağlamın medyanı (sistem istemi + araçlar)
    model: CostModel
    models: tuple[ModelShare, ...]
    outcomes: tuple[Outcome, ...]


type ApplyHint = Callable[[int], str]
type TraceWeight = Callable[[SessionTrace], float | None]  # oturum maliyetinin birim çevirisi

OBSERVED: Final = Policy("observed behaviour", None, None)
POLICIES: Final = (
    OBSERVED,
    Policy("compact above 400k", 400_000, None),
    Policy("compact above 300k", 300_000, None),
    Policy("compact above 200k", 200_000, None),
    Policy("compact above 150k", 150_000, None),
    Policy("compact above 100k", 100_000, None),
    Policy("cold-cache compact above 80k", None, 80_000),
    Policy("above 200k + cold above 80k", 200_000, 80_000),
)


def simulate_claude(
    logs_dir: Path, days: int, now: float, overrides: CostOverrides
) -> SimulationResult:
    """Claude Code transcript'leri (alt ajanlar dahil) üzerinde politikaları çalıştırır."""
    traces = load_claude_traces(recent_transcripts(logs_dir, days, now))
    requests = [usage for trace in traces for usage in trace.requests]
    return simulate_traces(
        "Claude Code",
        traces,
        days,
        average_write_weight(total_usage(requests)),
        overrides,
        dollar_weight,
    )


def simulate_codex(
    logs_dir: Path, days: int, now: float, overrides: CostOverrides
) -> SimulationResult:
    """Codex CLI rollout kayıtları üzerinde politikaları çalıştırır."""
    min_mtime = now - days * SECONDS_PER_DAY
    files = sorted(
        path
        for path in logs_dir.rglob("rollout-*.jsonl")
        if path.stat().st_mtime >= min_mtime
        and BENCH_PROJECT_MARKER not in (rollout_cwd(path) or "")
    )
    if not files:
        raise ConfigError(f"no Codex rollouts under {logs_dir} modified in the last {days} days")
    return simulate_traces(
        "Codex CLI",
        [load_codex_trace(path) for path in files],
        days,
        OPENAI.uncached,
        overrides,
        base_unit_weight,
    )


def simulate_traces(
    agent: str,
    traces: Sequence[SessionTrace],
    days: int,
    write_weight: float,
    overrides: CostOverrides,
    weight: TraceWeight,
) -> SimulationResult:
    """Sağlayıcıdan bağımsız çekirdek: tüm politikaları aynı istek dizileri üzerinde oynatır.

    Her oturumun maliyeti kendi modelinin fiyat oranlarıyla taban girdi biriminde hesaplanır ve
    `weight` ile ortak birime (Claude: USD) çevrilir; böylece pahalı modelin bir tokenı ucuz
    modelinkinden ağır basar. Birimi çevrilemeyen oturumlar simülasyon dışında kalır ve sayılır.
    """
    active = [trace for trace in traces if trace.requests]
    weighted = [(trace, w) for trace in active if (w := weight(trace)) is not None]
    used = [trace for trace, _ in weighted]
    model = CostModel(
        write_weight=write_weight,
        post_compact_tokens=observed_median(
            [tokens for trace in used for tokens in trace.post_compact_tokens],
            overrides.post_compact_tokens,
            "--post-compact-tokens",
        ),
        post_compact_cached=observed_median(
            [tokens for trace in used for tokens in trace.post_compact_cached],
            overrides.post_compact_cached,
            "--post-compact-cached",
        ),
        summary_tokens=observed_median(
            [tokens for trace in used for tokens in trace.summary_tokens],
            overrides.summary_tokens,
            "--summary-tokens",
        ),
        refetch_tokens=overrides.refetch_tokens,
        refetch_requests=overrides.refetch_requests,
    )
    shares = Counter(
        (trace.model, trace_prices(trace, overrides.read_weight).read) for trace in used
    )
    return SimulationResult(
        agent=agent,
        sessions=len(used),
        requests=sum(len(trace.requests) for trace in used),
        days=days,
        unit="USD" if weight is dollar_weight else "base input units",
        unweighted_sessions=len(active) - len(used),
        exact_cost=sum(
            w * exact_cost(usage, trace_prices(trace, overrides.read_weight))
            for trace, w in weighted
            for usage in trace.requests
        ),
        observed_compactions=sum(len(trace.pre_compact_tokens) for trace in used),
        static_prefix=int(statistics.median(context_of(trace.requests[0]) for trace in used)),
        model=model,
        models=tuple(ModelShare(name, read, count) for (name, read), count in shares.most_common()),
        outcomes=tuple(
            run_policy(weighted, policy, model, overrides.read_weight) for policy in POLICIES
        ),
    )


def observed_median(observed: Sequence[int], override: int | None, flag: str) -> int:
    """Verilmişse kullanıcının değeri, yoksa kayıtlardaki gerçek sıkıştırmaların medyanı."""
    if override is not None:
        return override
    if not observed:
        raise ConfigError(f"no real compactions found in the logs; pass {flag} explicitly")
    return int(statistics.median(observed))


# API liste fiyatları: taban girdi, USD / milyon token (2026-10, platform.claude.com fiyatları).
# Önbellek ve çıktı çarpanları claude_prices'tan gelir; listede olmayan modeller
# fiyatlandırılmaz ve raporda ayrıca sayılır.
USD_PER_MTOK: Final = (
    ("claude-opus-5", 4.0),
    ("claude-opus-4", 5.0),
    ("claude-sonnet-5", 2.0),
    ("claude-sonnet-4", 3.0),
    ("claude-haiku-4-5", 1.0),
)


def usd_per_token(model: str) -> float | None:
    """Modelin taban girdi liste fiyatı (USD/token); listede olmayan model için None."""
    for marker, usd in USD_PER_MTOK:
        if marker in model.lower():
            return usd / 1e6
    return None


def dollar_weight(trace: SessionTrace) -> float | None:
    """Oturumun taban girdi birimi başına USD değeri; listede olmayan model için None (oturum
    dolarla tartılamaz, simülasyon dışında kalır ve raporda sayılır)."""
    return usd_per_token(trace.model)


def base_unit_weight(trace: SessionTrace) -> float | None:
    """Fiyat listesi olmayan sağlayıcı (Codex): taban girdi biriminde, her oturum aynı ağırlıkta."""
    return 1.0


def claude_prices(model: str) -> PriceSheet:
    """Claude modelinin önbellek okuma çarpanıyla Anthropic fiyat oranları."""
    for marker, read in CLAUDE_READ_WEIGHTS:
        if marker in model.lower():
            return replace(ANTHROPIC, read=read)
    return ANTHROPIC


def trace_prices(trace: SessionTrace, read_weight: float | None) -> PriceSheet:
    """Oturumun fiyat oranları; okuma çarpanı verilmişse onunla."""
    return trace.prices if read_weight is None else replace(trace.prices, read=read_weight)


def load_claude_traces(paths: Sequence[Path]) -> list[SessionTrace]:
    """Transcript'ler verilen sırayla; çatallanmış ya da sürdürülmüş oturumun önceki dosyadan
    kopyaladığı mesajlar (aynı mesaj kimliği) yalnızca ilk görüldüğü dosyada sayılır."""
    seen: set[str] = set()
    traces: list[SessionTrace] = []
    for path in paths:
        trace, ids = read_claude_trace(path, frozenset(seen))
        traces.append(trace)
        seen.update(ids)
    return traces


def load_claude_trace(path: Path) -> SessionTrace:
    """Tek Claude Code transcript'i, başka dosyalarla tekilleştirilmeden."""
    return read_claude_trace(path, frozenset())[0]


def read_claude_trace(path: Path, skip: Set[str]) -> tuple[SessionTrace, frozenset[str]]:
    """Claude Code transcript'i: mesaj kimliği başına son kullanım, model ve gerçek sıkıştırmalar;
    `skip` içindeki mesaj kimlikleri atlanır. Dosyadaki mesaj kimlikleriyle birlikte döner.

    Sıkıştırmadan sonraki bağlam compact_boundary'deki postTokens değil, ardından gelen ilk
    gerçek isteğin bağlamıdır: postTokens sistem istemini, araçları ve yeniden eklenen dosyaları
    içermez. Özetin çıktı tokenı özet metninden tahmin edilir (özetleme isteği transcript'e
    yazılmaz; düşünme tokenlarıyla gerçek çıktı daha büyüktür).
    """
    order: list[str] = []
    usages: dict[str, Usage] = {}
    models: Counter[str] = Counter()
    pre: list[int] = []
    summaries: list[int] = []
    after: list[str] = []  # her sıkıştırmadan sonraki ilk gerçek isteğin kimliği
    ids: set[str] = set()  # dosyadaki tüm gerçek isteklerin kimlikleri (atlananlar dahil)
    awaiting = False
    with path.open("rb") as handle:
        for raw_line in handle:
            entry = parse_line(raw_line)
            if entry is None:
                continue
            if entry.get("type") == "system" and entry.get("subtype") == "compact_boundary":
                pre.append(compact_metadata_tokens(entry, "preTokens", str(path)))
                awaiting = True
                continue
            message = entry.get("message")
            if not isinstance(message, dict):
                continue
            if entry.get("isCompactSummary") is True:
                summaries.append(estimate_tokens(content_text(message.get("content"))))
                continue
            message_id = message.get("id")
            model = message.get("model")
            usage = message_usage(message)
            if (
                entry.get("type") != "assistant"
                or not isinstance(message_id, str)
                or not isinstance(model, str)
                or model == SYNTHETIC_MODEL  # API'ye gitmeyen yerel mesaj; bağlamı 0 gösterir
                or usage is None
                or not is_api_usage(usage)
            ):
                continue
            ids.add(message_id)
            if message_id in skip:
                continue
            if message_id not in usages:
                order.append(message_id)
                models[model] += 1
                if awaiting and context_of(usage) > 0:
                    after.append(message_id)
                    awaiting = False
            usages[message_id] = usage  # aynı kimliğin son satırı geçerlidir
    name = models.most_common(1)[0][0] if models else ""
    first_after = [usages[message_id] for message_id in after]
    trace = SessionTrace(
        requests=tuple(usages[message_id] for message_id in order),
        model=name,
        prices=claude_prices(name),
        pre_compact_tokens=tuple(pre),
        post_compact_tokens=tuple(context_of(usage) for usage in first_after),
        post_compact_cached=tuple(usage.read for usage in first_after),
        summary_tokens=tuple(summaries),
    )
    return trace, frozenset(ids)


def content_text(content: object) -> str:
    """Mesaj içeriğinin metni (metin bloklarının birleşimi)."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(block.get("text", ""))
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


def rollout_cwd(path: Path) -> str | None:
    """Codex rollout'unun çalışma dizini (session_meta); kayıt yoksa None. CimriHook'un kendi A/B
    çalıştırmaları bununla dışarıda bırakılır."""
    with path.open("rb") as handle:
        for raw_line in handle:
            entry = parse_line(raw_line)
            payload = None if entry is None else entry.get("payload")
            if (
                entry is not None
                and entry.get("type") == "session_meta"
                and isinstance(payload, dict)
            ):
                cwd = payload.get("cwd")
                return cwd if isinstance(cwd, str) else None
    return None


def load_codex_trace(path: Path) -> SessionTrace:
    """Codex rollout kaydından istek dizisi (token_count) ve sıkıştırmalar (compacted).

    Sıkıştırmayı tetikleyen boyut compacted kaydından önceki son isteğin, sıkıştırma sonrası boyut
    ise ondan sonraki ilk isteğin bağlamıdır. token_count olayları sıkıştırma isteğinin kendisini
    içermez; özetin çıktı tokenı o isteğin token_usage_record kaydından okunur.
    """
    requests: list[Usage] = []
    post: list[Usage] = []
    pre: list[int] = []
    models: Counter[str] = Counter()
    outputs: dict[str, int] = {}  # yanıt kimliği -> çıktı tokenı (token_usage_record)
    compaction_ids: list[str] = []
    last_total: int | None = None
    after_compaction = False
    with path.open("rb") as handle:
        for raw_line in handle:
            entry = parse_line(raw_line)
            if entry is None:
                continue
            payload = entry.get("payload")
            kind = entry.get("type")
            if kind == "turn_context" and isinstance(payload, dict):
                if isinstance(payload.get("model"), str):
                    models[str(payload["model"])] += 1
                continue
            if kind == "token_usage_record" and isinstance(payload, dict):
                output = record_output(payload)
                if output is not None:
                    outputs[output[0]] = output[1]
                continue
            if kind == "compacted":
                if isinstance(payload, dict) and isinstance(
                    payload.get("compaction_response_id"), str
                ):
                    compaction_ids.append(str(payload["compaction_response_id"]))
                if requests:
                    pre.append(context_of(requests[-1]))
                after_compaction = True
                continue
            request = codex_request(entry)
            if request is None or request[1] == last_total:
                continue  # aynı kümülatif toplam: yeni istek yok
            usage, last_total = request
            if after_compaction:
                post.append(usage)
                after_compaction = False
            requests.append(usage)
    return SessionTrace(
        requests=tuple(requests),
        model=models.most_common(1)[0][0] if models else "",
        prices=OPENAI,
        pre_compact_tokens=tuple(pre),
        post_compact_tokens=tuple(context_of(usage) for usage in post),
        post_compact_cached=tuple(usage.read for usage in post),
        summary_tokens=tuple(outputs[key] for key in compaction_ids if key in outputs),
    )


def record_output(payload: JsonObject) -> tuple[str, int] | None:
    """token_usage_record kaydının (yanıt kimliği, çıktı tokenı) çifti; alanlar yoksa None."""
    response_id = payload.get("response_id")
    usage = payload.get("usage")
    if not isinstance(response_id, str) or not isinstance(usage, dict):
        return None
    output = usage.get("output_tokens")
    return (response_id, output) if isinstance(output, int) else None


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


def compact_metadata_tokens(entry: JsonObject, key: str, where: str) -> int:
    """compact_boundary satırındaki bağlam boyutu (preTokens: tetikleyen, postTokens: sonraki).

    Değer yoksa sıkıştırma sessizce sayılmamış olurdu; bu bilinmeyen bir kayıt biçimidir: hata.
    """
    metadata = entry.get("compactMetadata")
    tokens = metadata.get(key) if isinstance(metadata, dict) else None
    if not isinstance(tokens, int) or tokens <= 0:
        raise TranscriptError(
            f"{where}: compact_boundary without a positive compactMetadata.{key} ({metadata!r})"
        )
    return tokens


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


def is_api_usage(usage: Usage) -> bool:
    """Kullanım bir API yanıtının mı? Bir eklentinin sıkıştırmasından sonra transcript'e yeniden
    yazılan satırlar aynı mesaj kimliğini sıfır kullanımla taşır; onlar istek sayılmaz."""
    return context_of(usage) + usage.output > 0


def written_of(usage: Usage) -> int:
    """İstekte önbellekten okunmayan (yeni yazılan ya da önbelleksiz) girdi."""
    return usage.uncached + usage.write_5m + usage.write_1h


def is_cold(usage: Usage, previous_context: int) -> bool:
    """Önbellek soğumuş mu: istek bir önceki bağlamın yarısından azını önbellekten mi okudu?

    Oturumun ilk isteğinin önceki bağlamı yoktur; o istek zaten her şeyi yazar.
    """
    return previous_context > 0 and usage.read < COLD_READ_SHARE * previous_context


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


def simulate_trace(
    trace: SessionTrace, policy: Policy, model: CostModel, prices: PriceSheet
) -> tuple[float, int, int]:
    """Bir oturumu politikayla yeniden oynatır: (maliyet, sıkıştırma sayısı, bağlam toplamı).

    Yeni yazılan girdi: önbellek soğuksa bağlamın tamamı, simüle sıkıştırmadan sonra önbellekte
    kalan önek dışındaki kısım, diğer isteklerde gerçek istekte yazılan kadarı.
    """
    cost = 0.0
    compactions = 0
    context_sum = 0
    simulated = 0
    previous_actual = 0
    for index, usage in enumerate(trace.requests):
        actual = context_of(usage)
        growth = actual - previous_actual
        cold = is_cold(usage, previous_actual)
        previous_actual = actual
        if index == 0:
            simulated = actual
        elif growth < 0:
            simulated = min(simulated, actual)  # gerçek oturum da burada küçüldü
        else:
            simulated += growth
        written = min(written_of(usage), simulated)
        if should_compact(policy, simulated, cold, model):
            read_weight = prices.uncached if cold else prices.read
            cost += simulated * read_weight + model.summary_tokens * prices.output
            simulated = model.post_compact_tokens + model.refetch_tokens
            cost += model.refetch_requests * simulated * prices.read
            compactions += 1
            written = simulated - min(model.post_compact_cached, simulated)
        if cold:
            written = simulated
        cost += (
            written * model.write_weight
            + (simulated - written) * prices.read
            + usage.output * prices.output
        )
        context_sum += simulated
    return cost, compactions, context_sum


def run_policy(
    weighted: Sequence[tuple[SessionTrace, float]],
    policy: Policy,
    model: CostModel,
    read_weight: float | None,
) -> Outcome:
    """Politikayı her oturumda kendi fiyatlarıyla çalıştırır ve ortak birimde toplar."""
    results = [
        (w, simulate_trace(trace, policy, model, trace_prices(trace, read_weight)))
        for trace, w in weighted
    ]
    requests = sum(len(trace.requests) for trace, _ in weighted)
    return Outcome(
        policy=policy,
        cost=sum(w * cost for w, (cost, _, _) in results),
        compactions=sum(count for _, (_, count, _) in results),
        mean_context=sum(total for _, (_, _, total) in results) / requests if requests else 0.0,
    )


def claude_hint(window: int) -> str:
    """Claude Code ayarı: sıkıştırma pencere − 33000 bağlamda tetiklenir; ayarın alt sınırı 100000.

    Bu yüzden 67000 tokendan daha erken sıkıştırma belgelenmiş ayarla mümkün değildir.
    """
    setting = max(window + CLAUDE_COMPACT_OFFSET, CLAUDE_MIN_COMPACT_WINDOW)
    return f"`cimrihook init --compact-window {setting}`"


def codex_hint(window: int) -> str:
    """Codex sıkıştırma eşiğini toplam bağlam üzerinden uygular."""
    return f"`cimrihook init --agent codex --compact-window {window}`"


def render_simulation(result: SimulationResult, hint: ApplyHint) -> str:
    """Simülasyon raporunun metni."""
    model = result.model
    baseline = result.outcomes[0].cost
    calibration = 100 * (baseline - result.exact_cost) / result.exact_cost
    prices = ", ".join(
        f"{share.model or 'unknown'} read {share.read} ({share.sessions} sessions)"
        for share in result.models
    )
    written = (
        "observed cache-write mix"
        if result.agent == "Claude Code"
        else "uncached input; Codex does not price cache writes"
    )
    lines = [
        f"CimriHook simulate ({result.agent}): {result.sessions} sessions active in the last "
        f"{result.days} days (replayed whole), {result.requests:,} requests; static prefix "
        f"(median first request) {result.static_prefix:,} tokens",
        f"Prices (base input = 1, per session model; sessions are summed in {result.unit}"
        + (
            f", {result.unweighted_sessions} sessions of models without a list price left out"
            if result.unweighted_sessions
            else ""
        )
        + f"): {prices}; new input written at {model.write_weight:.2f} ({written})",
        f"Compaction (medians of {result.observed_compactions} real compactions unless given): "
        f"summary {model.summary_tokens:,} output tokens (estimated from the summary text; the "
        f"real output is larger), next request carries {model.post_compact_tokens:,} context "
        f"tokens of which {model.post_compact_cached:,} stay cached, + "
        f"{model.refetch_tokens:,} re-read in {model.refetch_requests} extra requests",
        f"Exact cost from usage logs: {amount(result.exact_cost, result.unit)}; model replay of "
        f"observed "
        f"behaviour: {amount(baseline, result.unit)} (replay check {calibration:+.1f}%; this "
        "checks the "
        "cost accounting, not the policy predictions: see `cimrihook bench-calibrate`)",
        f"  {'policy':<30}{cost_header(result.unit):>10}{'vs observed':>13}{'compactions':>13}"
        f"{'mean context':>14}",
    ]
    lines.extend(
        f"  {outcome.policy.name:<30}{amount(outcome.cost, result.unit):>10}"
        f"{100 * (outcome.cost - baseline) / baseline:>12.1f}%{outcome.compactions:>13,}"
        f"{outcome.mean_context:>14,.0f}"
        for outcome in result.outcomes
    )
    return "\n".join([*lines, *recommendation(result, hint)])


def amount(cost: float, unit: str) -> str:
    """Maliyetin birimiyle kısa metni: dolar ya da milyar taban girdi birimi."""
    return f"${cost:,.0f}" if unit == "USD" else f"{cost / 1e9:.3f}B"


def cost_header(unit: str) -> str:
    """Politika tablosunun maliyet sütun başlığı."""
    return "cost ($)" if unit == "USD" else "cost (B)"


def recommended_window(result: SimulationResult) -> tuple[Outcome, Outcome] | None:
    """(önerilen, en ucuz) pencere politikası; hiçbir pencere maliyeti düşürmüyorsa None.

    Önerilen, maliyeti en ucuzun RECOMMENDATION_SLACK puan yakınındaki en büyük penceredir. Soğuk
    önbellek koşulu olan politikalar bir ayarla uygulanamadığı için aday değildir.
    """
    baseline = result.outcomes[0].cost
    windows = [
        outcome
        for outcome in result.outcomes
        if outcome.policy.window is not None and outcome.policy.cold_window is None
    ]
    if not windows:
        return None
    cheapest = min(windows, key=lambda outcome: outcome.cost)
    if cheapest.cost >= baseline:
        return None
    near = [o for o in windows if o.cost <= cheapest.cost + RECOMMENDATION_SLACK * baseline]
    return max(near, key=lambda outcome: outcome.policy.window or 0), cheapest


def recommendation_text(chosen: Outcome, cheapest: Outcome, baseline: float) -> str:
    """Önerilen pencerenin etkisi; en ucuzdan farklıysa neden seçildiği."""
    effect = (
        f"{100 * (chosen.cost - baseline) / baseline:+.1f}%, {chosen.compactions:,} compactions"
    )
    if chosen is cheapest:
        return f"'{chosen.policy.name}' ({effect})"
    return (
        f"'{chosen.policy.name}' ({effect}); the cheapest, '{cheapest.policy.name}' "
        f"({100 * (cheapest.cost - baseline) / baseline:+.1f}%, {cheapest.compactions:,} "
        f"compactions), saves at most {100 * RECOMMENDATION_SLACK:.0f} point more with more "
        "compactions, and each compaction loses detail"
    )


def recommendation(result: SimulationResult, hint: ApplyHint) -> list[str]:
    """Önerilen pencere politikası ve nasıl uygulanacağı."""
    found = recommended_window(result)
    if found is None:
        return ["No compaction window lowers the simulated cost of these logs; keep the default."]
    chosen, cheapest = found
    window = chosen.policy.window
    if window is None:
        raise ValueError(f"recommended policy {chosen.policy.name!r} has no window")
    return [
        f"Recommended window: {recommendation_text(chosen, cheapest, result.outcomes[0].cost)}. "
        f"Apply it with {hint(window)} and confirm task quality with an A/B run first.",
    ]
