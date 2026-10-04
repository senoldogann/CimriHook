"""Limit ölçer: aboneliğin 5 saatlik ve haftalık pencerelerinin liste fiyatıyla kuru.

Pro ve Max aboneliklerinde fatura yoktur; kullanım 5 saatlik ve haftalık pencerelerin yüzdesi
olarak dolar. Anthropic bir token türünün (önbellek okuma, yazma, çıktı) pencerede ne kadar
saydığını yayımlamaz, ama Claude Code'un kendi arayüzü model ya da efor değiştirmenin "her şeyi
yeniden okuduğunu ve kullanımına eklendiğini" söyler: pencereyi dolduran, CimriHook'un liste
fiyatıyla saydığı token akışlarıdır.

CimriHook mod'u her ölçümde (her turdan sonra ve bir pencere bir puan ilerlediğinde) oturumun o
ana kadarki liste fiyatı harcamasını ve pencerelerin yüzdesini `limits/<oturum>.jsonl` dosyasına
ekler. Burada tüm oturumların kayıtları birleştirilir: her pencere döneminde (aynı sıfırlanma
zamanı) gözlenen yüzde artışı, oturumların aynı dönemdeki harcama artışlarının toplamıyla
karşılaştırılır ve "pencerenin bir puanı kaç dolarlık kullanım" kuru çıkar. claude.ai sohbetleri,
mod'u olmayan oturumlar ve A/B koşuları da pencereyi doldurur ama kayda geçmez; böyle kullanım
varken kur bir puanın dolar karşılığını olduğundan düşük gösterir.
"""

import json
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TypeGuard

from cimrihook.errors import ConfigError

LIMITS_DIR: Final = "limits"
MIN_POINTS: Final = 3.0  # bir kurdan söz etmek için gözlenmesi gereken toplam puan
WINDOW_NAMES: Final = {"five_hour": "5-hour window", "seven_day": "weekly window"}


@dataclass(frozen=True, slots=True)
class WindowUse:
    """Bir ölçümde bir pencerenin kullanılan yüzdesi."""

    kind: str  # five_hour, seven_day ya da bir ağ geçidinin spend_limit'i
    percent: float
    resets_at: str  # dönemi ayırır; aynı sıfırlanma zamanı aynı dönemdir


@dataclass(frozen=True, slots=True)
class LimitSample:
    """Mod'un bir oturumda yaptığı tek ölçüm."""

    session: str
    time: float  # epoch saniye
    usd: float  # oturumun o ana kadarki liste fiyatı harcaması
    windows: tuple[WindowUse, ...]


@dataclass(frozen=True, slots=True)
class WindowRate:
    """Bir pencere türünün gözlenen dönemlerdeki puan ve harcama toplamı."""

    kind: str
    periods: int  # en az bir puan ilerlediği gözlenen dönem
    points: float
    usd: float

    def usd_per_point(self) -> float | None:
        """Pencerenin bir puanına düşen liste fiyatı harcama; yeterli puan yoksa None."""
        return self.usd / self.points if self.points >= MIN_POINTS else None


def limits_dir(home: Path) -> Path:
    """Mod'un ölçüm dosyalarının dizini."""
    return home / LIMITS_DIR


def read_samples(directory: Path) -> list[LimitSample]:
    """Tüm oturumların ölçümleri, zamana göre sıralı."""
    if not directory.is_dir():
        raise ConfigError(
            f"no limit samples in {directory}: the CimriHook mod writes them on a subscription "
            "once it is enabled (`cimrihook init --mod`)"
        )
    samples = [
        sample for path in sorted(directory.glob("*.jsonl")) for sample in session_samples(path)
    ]
    return sorted(samples, key=lambda sample: sample.time)


def session_samples(path: Path) -> list[LimitSample]:
    """Bir oturum dosyasının ölçümleri; bozuk satır dosya ve satır numarasıyla hata verir."""
    return [
        parse_sample(path.stem, line, f"{path}:{number}")
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if line.strip()
    ]


def parse_sample(session: str, line: str, where: str) -> LimitSample:
    """Mod'un yazdığı tek satır: `{"t": ms, "usd": ..., "limits": [{kind, percentUsed, ...}]}`."""
    try:
        raw: object = json.loads(line)
    except ValueError as error:
        raise ConfigError(f"{where}: unreadable limit sample ({error})") from error
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: a limit sample must be a JSON object, got {line[:80]!r}")
    stamp, usd, limits = raw.get("t"), raw.get("usd"), raw.get("limits")
    if not is_number(stamp) or not is_number(usd) or not isinstance(limits, list):
        raise ConfigError(f"{where}: a limit sample needs numeric t and usd and a limits list")
    return LimitSample(
        session=session,
        time=float(stamp) / 1000,
        usd=float(usd),
        windows=tuple(parse_window(window, where) for window in limits),
    )


def parse_window(window: object, where: str) -> WindowUse:
    """Bir pencerenin kullanımı: tür, yüzde ve sıfırlanma zamanı."""
    if not isinstance(window, dict):
        raise ConfigError(f"{where}: a window must be a JSON object")
    kind, percent, resets_at = window.get("kind"), window.get("percentUsed"), window.get("resetsAt")
    if not isinstance(kind, str) or not is_number(percent):
        raise ConfigError(f"{where}: a window needs a kind and a numeric percentUsed")
    return WindowUse(kind, float(percent), resets_at if isinstance(resets_at, str) else "")


def is_number(value: object) -> TypeGuard[int | float]:
    """JSON sayısı mı (bool hariç)?"""
    return isinstance(value, int | float) and not isinstance(value, bool)


def window_rates(samples: Sequence[LimitSample]) -> list[WindowRate]:
    """Her pencere türü için gözlenen puan artışı ve aynı dönemlerdeki harcama artışı.

    Bir oturumun art arda iki ölçümü arasındaki harcama, sonraki ölçümün dönemine yazılır. Bir
    dönemin puanı, o dönemde gözlenen en yüksek ve en düşük yüzdenin farkıdır.
    """
    percents: dict[tuple[str, str], list[float]] = defaultdict(list)
    spend: dict[tuple[str, str], float] = defaultdict(float)
    previous: dict[str, LimitSample] = {}
    for sample in samples:
        for window in sample.windows:
            percents[(window.kind, window.resets_at)].append(window.percent)
        before = previous.get(sample.session)
        if before is not None and sample.usd >= before.usd:
            for window in sample.windows:
                spend[(window.kind, window.resets_at)] += sample.usd - before.usd
        previous[sample.session] = sample
    rates: dict[str, WindowRate] = {}
    for (kind, period), values in percents.items():
        points = max(values) - min(values)
        if points <= 0:
            continue
        current = rates.get(kind, WindowRate(kind, 0, 0.0, 0.0))
        rates[kind] = WindowRate(
            kind, current.periods + 1, current.points + points, current.usd + spend[(kind, period)]
        )
    return [rates[kind] for kind in sorted(rates)]


def render_limits(samples: Sequence[LimitSample], rates: Sequence[WindowRate]) -> str:
    """`cimrihook limits` çıktısı."""
    sessions = len({sample.session for sample in samples})
    lines = [
        f"CimriHook limits: {len(samples):,} measurements from {sessions:,} sessions "
        "(list-price spend of each session against your subscription windows)"
    ]
    if not rates:
        lines.append("  no window has moved a whole point between measurements yet")
    for rate in rates:
        name = WINDOW_NAMES.get(rate.kind, rate.kind)
        per_point = rate.usd_per_point()
        measured = (
            f"1 point is about ${per_point:,.2f} of usage at API list prices"
            if per_point is not None
            else f"needs at least {MIN_POINTS:.0f} points to estimate"
        )
        lines.append(
            f"  {name}: {rate.points:.0f} points over {rate.periods} periods, "
            f"${rate.usd:,.2f} measured; {measured}"
        )
    lines.append(
        "  Use outside these sessions (claude.ai, sessions without the mod, A/B runs) also "
        "fills the windows; with such use a point looks cheaper than it is."
    )
    return "\n".join(lines)
