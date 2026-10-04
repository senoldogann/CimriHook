"""Kazanç: CimriHook'un ayarları değiştiren son kurulumundan önceki ve sonraki dönem.

RTK'nın `gain` raporu gibi kullanıcının kendi kayıtlarından okunur: son `cimrihook init`'ten bu yana
geçen süre, ondan hemen önceki aynı uzunluktaki dönemle karşılaştırılır. Her istek doctor'daki gibi
kendi kullanım verisiyle API liste fiyatından fiyatlanır; çatallanmış oturumların kopyaladığı
istekler bir kez sayılır, zamanı olmayan istek bir döneme atanamadığı için sayılmaz. İki dönemin
iş yükü farklıdır: bu bir A/B testi değil, önce-sonra görünümüdür. Bu yüzden toplam harcama
yanında işin miktarından daha az etkilenen oranlar gösterilir: istek başına harcama, ortalama
bağlam ve harcamanın büyük bağlamlı isteklerdeki payı.

Makbuz ise eşleştirilmiştir: kurulumdan sonraki oturumların kendi istekleri, o dönemdeki otomatik
sıkıştırmalar hiç olmasaydı ne tutacağıyla yeniden fiyatlanır. Sıkıştırmanın sildiği bağlam
sonraki her istekte önbellekten yeniden okunurdu; sıkıştırmadan sonraki ilk isteğin yazdığı özet ve
yeniden eklenen dosyalar ise yazılmaz, eski bağlam okunurdu. Sıkıştırma çağrıları transcript'e
yazılmadığı için gerçek tarafa tahminle eklenir (bağlamın bir kez okunması ve özetin çıktısı).
Bağlam modelin varsayılan eşiğini geçecek olsaydı Claude Code yine sıkıştırırdı; orada taşınan
bağlam sıfırlanır. Yeniden okumayı ve ajanın değişen davranışını göremez: A/B koşularında bu tür
yeniden oynatma 0-11 puan iyimser çıktı.
"""

import math
import sqlite3
import statistics
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from cimrihook.audit import SECONDS_PER_DAY, recent_transcripts
from cimrihook.doctor import IDLE_HOUR, percent, rewrite_cause, tokens_text
from cimrihook.errors import ConfigError, LedgerError
from cimrihook.hook import ledger_path
from cimrihook.install import load_record
from cimrihook.scan import TranscriptScan, scan_transcript, token_costs, unique_scans
from cimrihook.simulate import claude_prices, context_of, usd_per_token

LARGE_CONTEXT: Final = 200_000  # bu bağlamın üstündeki istekler büyük bağlamlı sayılır
# Pencere olmasaydı Claude Code'un 1M bağlamlı modellerde sıkıştıracağı bağlam (pencere − 33k).
DEFAULT_COMPACTION_CONTEXT: Final = 967_000
MIN_PERIOD_SECONDS: Final = 3_600.0  # bir saatten kısa dönemler karşılaştırılmaz


@dataclass(frozen=True, slots=True)
class Period:
    """Bir dönemin istek ve harcama özeti."""

    start: float
    end: float
    requests: int
    usd: float  # API liste fiyatlarıyla
    mean_context: float
    large_context_usd: float  # bağlamı LARGE_CONTEXT'i aşan isteklerin harcaması
    compactions: int
    idle_rewrite_usd: float  # bir saatten uzun boşluktan sonra önbelleğe yeniden yazma
    guard_stops: int


@dataclass(frozen=True, slots=True)
class Receipt:
    """Kurulumdan sonraki oturumlar: gerçek maliyet ve otomatik sıkıştırmalar olmasaydı tahmini."""

    compactions: int  # dönemdeki otomatik sıkıştırmalar
    sessions: int  # bu sıkıştırmaların olduğu oturumlar
    requests_usd: float  # o oturumların dönemdeki istekleri
    compaction_calls_usd: float  # sıkıştırma çağrılarının tahmini (transcript'te yok)
    without_compactions_usd: float  # aynı istekler, sıkıştırmasız yeniden fiyatlanmış


@dataclass(frozen=True, slots=True)
class Gain:
    """Kurulumdan önceki ve sonraki dönem, ve sonraki dönemin makbuzu."""

    installed_at: float
    before: Period
    after: Period
    receipt: Receipt


def measure_gain(projects_dir: Path, home: Path, settings_path: Path, now: float) -> Gain:
    """Son kurulumdan bu yana geçen süreyi ondan önceki aynı uzunluktaki süreyle karşılaştırır."""
    installed_at = load_record(home, settings_path).installed_at
    if installed_at is None:
        raise ConfigError(
            f"no install time is recorded for {settings_path}; run `cimrihook init` first, gain "
            "compares the time since then with the same time before it"
        )
    length = now - installed_at
    if length < MIN_PERIOD_SECONDS:
        raise ConfigError(
            f"the last init was {length / 60:.0f} minutes ago; gain needs at least an hour after it"
        )
    start = installed_at - length
    files = recent_transcripts(projects_dir, math.ceil((now - start) / SECONDS_PER_DAY), now)
    scans = unique_scans([scan_transcript(path, "subagents" in path.parts) for path in files])
    stops = guard_stop_times(ledger_path(home))
    return Gain(
        installed_at=installed_at,
        before=period(scans, start, installed_at, stops),
        after=period(scans, installed_at, now, stops),
        receipt=receipt(scans, installed_at, now),
    )


def receipt(scans: Sequence[TranscriptScan], start: float, end: float) -> Receipt:
    """[start, end) aralığındaki otomatik sıkıştırmaların eşleştirilmiş karşı-olgusalı."""
    parts = [session_receipt(scan, start, end) for scan in scans]
    touched = [part for part in parts if part.compactions]
    return Receipt(
        compactions=sum(part.compactions for part in touched),
        sessions=len(touched),
        requests_usd=sum(part.requests_usd for part in touched),
        compaction_calls_usd=sum(part.compaction_calls_usd for part in touched),
        without_compactions_usd=sum(part.without_compactions_usd for part in touched),
    )


def session_receipt(scan: TranscriptScan, start: float, end: float) -> Receipt:
    """Tek oturum: sıkıştırmadan sonraki ilk istek, sırayla o sıkıştırmayla eşleşir."""
    boundaries = iter(scan.compactions)
    carried = 0  # sıkıştırmaların sildiği, karşı-olgusalda bağlamda kalan token
    compactions = 0
    actual = calls = counterfactual = 0.0
    for request in scan.requests:
        compaction = next(boundaries, None) if request.after_compaction else None
        inside = request.timestamp is not None and start <= request.timestamp < end
        base = usd_per_token(request.model)
        if not inside or base is None:
            carried = 0 if compaction is not None else carried  # pencere dışı: zincir kopar
            continue
        prices = claude_prices(request.model)
        context = context_of(request.usage)
        cost = sum(token_costs(request, base))
        actual += cost
        if compaction is not None and compaction.automatic:
            compactions += 1
            calls += (
                compaction.trigger * prices.read + compaction.summary_tokens * prices.output
            ) * base
            carried += max(0, compaction.trigger - context)
            if context + carried > DEFAULT_COMPACTION_CONTEXT:
                carried = 0  # varsayılan eşik de burada sıkıştırırdı
            # Karşı-olgusal: sıkıştırma sonrası yazım yok, bütün bağlam önbellekten okunur.
            counterfactual += (
                (context + carried) * prices.read + request.usage.output * prices.output
            ) * base
            continue
        if context + carried > DEFAULT_COMPACTION_CONTEXT:
            carried = 0
        counterfactual += cost + carried * prices.read * base
    return Receipt(
        compactions=compactions,
        sessions=1 if compactions else 0,
        requests_usd=actual,
        compaction_calls_usd=calls,
        without_compactions_usd=counterfactual,
    )


def period(
    scans: Sequence[TranscriptScan], start: float, end: float, stops: Sequence[float]
) -> Period:
    """[start, end) aralığındaki istekler, sıkıştırmalar ve koruma durdurmaları."""
    requests = [
        request
        for scan in scans
        for request in scan.requests
        if request.timestamp is not None and start <= request.timestamp < end
    ]
    priced = [
        (request, token_costs(request, base))
        for request in requests
        if (base := usd_per_token(request.model)) is not None
    ]
    contexts = [context_of(request.usage) for request in requests]
    return Period(
        start=start,
        end=end,
        requests=len(requests),
        usd=sum(sum(costs) for _, costs in priced),
        mean_context=statistics.fmean(contexts) if contexts else 0.0,
        large_context_usd=sum(
            sum(costs) for request, costs in priced if context_of(request.usage) > LARGE_CONTEXT
        ),
        compactions=sum(
            1
            for scan in scans
            for compaction in scan.compactions
            if compaction.timestamp is not None and start <= compaction.timestamp < end
        ),
        idle_rewrite_usd=sum(
            costs[1] + costs[2] for request, costs in priced if rewrite_cause(request) == IDLE_HOUR
        ),
        guard_stops=sum(1 for stopped in stops if start <= stopped < end),
    )


def guard_stop_times(ledger: Path) -> tuple[float, ...]:
    """Soğuk istem korumasının durdurduğu istemlerin zamanları; defter ya da tablo yoksa boş."""
    if not ledger.exists():
        return ()
    try:
        with closing(sqlite3.connect(f"file:{ledger}?mode=ro", uri=True, timeout=2.0)) as db:
            table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'guard_blocks'"
            ).fetchone()
            if table is None:
                return ()
            rows = db.execute("SELECT blocked_at FROM guard_blocks").fetchall()
    except sqlite3.Error as error:
        raise LedgerError(f"cannot read guard stops from {ledger}: {error}") from error
    return tuple(float(row[0]) for row in rows)


def render_gain(gain: Gain) -> str:
    """Raporun metni."""
    before, after = gain.before, gain.after
    since = datetime.fromtimestamp(gain.installed_at).strftime("%Y-%m-%d %H:%M")
    days = (after.end - after.start) / SECONDS_PER_DAY
    rows = [
        ("API requests", f"{before.requests:,}", f"{after.requests:,}", ""),
        ("spend at list prices", f"${before.usd:,.0f}", f"${after.usd:,.0f}", ""),
        (
            "spend per request",
            f"${per_request(before):.3f}",
            f"${per_request(after):.3f}",
            change(per_request(before), per_request(after)),
        ),
        (
            "mean context",
            tokens_text(round(before.mean_context)),
            tokens_text(round(after.mean_context)),
            change(before.mean_context, after.mean_context),
        ),
        (
            f"spend in requests over {LARGE_CONTEXT // 1000}k",
            percent(before.large_context_usd, before.usd),
            percent(after.large_context_usd, after.usd),
            "",
        ),
        ("compactions", f"{before.compactions:,}", f"{after.compactions:,}", ""),
        (
            "re-cache after 1 h idle",
            f"${before.idle_rewrite_usd:,.0f}",
            f"${after.idle_rewrite_usd:,.0f}",
            "",
        ),
        ("prompts stopped by the guard", f"{before.guard_stops:,}", f"{after.guard_stops:,}", ""),
    ]
    return "\n".join(
        [
            f"CimriHook gain: the {days:.1f} days since your last `cimrihook init` ({since}) vs "
            "the same time before it",
            f"  {'':<30}{'before':>12}{'after':>12}{'change':>10}",
            *(f"  {label:<30}{old:>12}{new:>12}{delta:>10}" for label, old, new, delta in rows),
            "Workloads differ between the two periods, so this is a before/after view, not an A/B "
            "test; per-request spend and mean context depend least on how much you worked.",
            *receipt_lines(gain.receipt),
        ]
    )


def receipt_lines(receipt: Receipt) -> list[str]:
    """Eşleştirilmiş makbuzun metni."""
    if not receipt.compactions:
        return ["Receipt: no automatic compaction since the last init, so nothing to compare yet."]
    actual = receipt.requests_usd + receipt.compaction_calls_usd
    saved = receipt.without_compactions_usd - actual
    return [
        f"Receipt for the {receipt.sessions:,} sessions with automatic compactions since then "
        f"({receipt.compactions:,} compactions): they cost ${actual:,.2f} (requests "
        f"${receipt.requests_usd:,.2f} + compaction calls about "
        f"${receipt.compaction_calls_usd:,.2f}). The same requests without those compactions: "
        f"about ${receipt.without_compactions_usd:,.2f}, so the window saved about ${saved:,.2f} "
        f"({change(receipt.without_compactions_usd, actual)}). This replay keeps the removed "
        "context and re-reads it on every later request; in A/B runs such replays were 0-11 points "
        "optimistic.",
    ]


def per_request(summary: Period) -> float:
    """İstek başına liste fiyatı harcaması; istek yoksa 0."""
    return summary.usd / summary.requests if summary.requests else 0.0


def change(before: float, after: float) -> str:
    """Göreli değişim; önceki değer sıfırsa '-'."""
    return f"{100 * (after - before) / before:+.0f}%" if before > 0 else "-"
