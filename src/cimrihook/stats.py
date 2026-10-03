"""Küçük örneklemler için A/B istatistikleri: log ölçeğinde oranlar ve oran farkları.

Maliyetler çarpımsal davrandığı için karşılaştırmalar log ölçeğinde yapılır ve geometrik ortalama
oranı olarak raporlanır. Örneklem küçükken sahte dar aralık üretmemek için yeniden örnekleme
(bootstrap) yerine t dağılımı kullanılır; bir kolda ikiden az ölçüm varsa aralık verilmez.
Başarı oranları için Wilson aralıkları ve Newcombe'un fark aralığı kullanılır; ikisi de hiç
başarısızlık olmayan küçük örneklemlerde de anlamlı sınır verir.
"""

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

# Student t dağılımının %97.5 yüzdeliği, serbestlik derecesi 1..30. Daha büyük dereceler için
# 30'un değeri kullanılır; gerçek değerden biraz büyük olduğu için aralık temkinli kalır.
T_975: Final = (
    12.706,
    4.303,
    3.182,
    2.776,
    2.571,
    2.447,
    2.365,
    2.306,
    2.262,
    2.228,
    2.201,
    2.179,
    2.160,
    2.145,
    2.131,
    2.120,
    2.110,
    2.101,
    2.093,
    2.086,
    2.080,
    2.074,
    2.069,
    2.064,
    2.060,
    2.056,
    2.052,
    2.048,
    2.045,
    2.042,
)
Z_975: Final = 1.959964


@dataclass(frozen=True, slots=True)
class RatioEstimate:
    """Geometrik ortalama oranı ve %95 güven aralığı (aralık yoksa sınırlar None)."""

    ratio: float
    low: float | None
    high: float | None


@dataclass(frozen=True, slots=True)
class DifferenceEstimate:
    """İki oranın farkı (tedavi − baseline) ve %95 güven aralığı."""

    difference: float
    low: float
    high: float


def t_975(degrees_of_freedom: float) -> float:
    """t dağılımının %97.5 yüzdeliği; kesirli serbestlik derecesi aşağı yuvarlanır (temkinli)."""
    if degrees_of_freedom < 1:
        raise ValueError(f"degrees of freedom must be >= 1, got {degrees_of_freedom}")
    index = min(math.floor(degrees_of_freedom), len(T_975)) - 1
    return T_975[index]


def log_values(values: Sequence[float]) -> tuple[float, ...]:
    """Pozitif değerlerin doğal logaritması."""
    if any(value <= 0 for value in values):
        raise ValueError(f"log-scale statistics need positive values, got {list(values)}")
    return tuple(math.log(value) for value in values)


def geometric_mean(values: Sequence[float]) -> float:
    """Pozitif değerlerin geometrik ortalaması."""
    return math.exp(statistics.fmean(log_values(values)))


def ratio_estimate(base: Sequence[float], treated: Sequence[float]) -> RatioEstimate:
    """Tedavi/baseline geometrik ortalama oranı; aralık log ölçeğinde Welch t aralığıdır."""
    log_base = log_values(base)
    log_treated = log_values(treated)
    difference = statistics.fmean(log_treated) - statistics.fmean(log_base)
    if len(log_base) < 2 or len(log_treated) < 2:
        return RatioEstimate(math.exp(difference), None, None)
    base_term = statistics.variance(log_base) / len(log_base)
    treated_term = statistics.variance(log_treated) / len(log_treated)
    variance = base_term + treated_term
    if variance == 0:
        return RatioEstimate(math.exp(difference), math.exp(difference), math.exp(difference))
    degrees = variance**2 / (
        base_term**2 / (len(log_base) - 1) + treated_term**2 / (len(log_treated) - 1)
    )
    margin = t_975(degrees) * math.sqrt(variance)
    return RatioEstimate(
        math.exp(difference), math.exp(difference - margin), math.exp(difference + margin)
    )


def pooled_ratio(log_ratios: Sequence[float]) -> RatioEstimate:
    """Senaryo log-oranlarının eşit ağırlıklı ortalaması; aralık senaryolar arası t aralığıdır."""
    mean = statistics.fmean(log_ratios)
    if len(log_ratios) < 2:
        return RatioEstimate(math.exp(mean), None, None)
    margin = t_975(len(log_ratios) - 1) * statistics.stdev(log_ratios) / math.sqrt(len(log_ratios))
    return RatioEstimate(math.exp(mean), math.exp(mean - margin), math.exp(mean + margin))


def wilson_interval(successes: int, trials: int) -> tuple[float, float]:
    """Bir başarı oranının %95 Wilson aralığı."""
    if trials <= 0 or not 0 <= successes <= trials:
        raise ValueError(f"need 0 <= successes <= trials and trials > 0, got {successes}/{trials}")
    rate = successes / trials
    z2 = Z_975**2
    denominator = 1 + z2 / trials
    centre = (rate + z2 / (2 * trials)) / denominator
    half = Z_975 * math.sqrt(rate * (1 - rate) / trials + z2 / (4 * trials**2)) / denominator
    return max(0.0, centre - half), min(1.0, centre + half)


def rate_difference(
    treated_successes: int, treated_trials: int, base_successes: int, base_trials: int
) -> DifferenceEstimate:
    """Başarı oranı farkı (tedavi − baseline) ve Newcombe'un hibrit skor aralığı."""
    treated_rate = treated_successes / treated_trials
    base_rate = base_successes / base_trials
    treated_low, treated_high = wilson_interval(treated_successes, treated_trials)
    base_low, base_high = wilson_interval(base_successes, base_trials)
    difference = treated_rate - base_rate
    return DifferenceEstimate(
        difference=difference,
        low=difference - math.hypot(treated_rate - treated_low, base_high - base_rate),
        high=difference + math.hypot(treated_high - treated_rate, base_rate - base_low),
    )
