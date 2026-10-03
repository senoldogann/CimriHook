"""Önbellek ömrü danışmanı: kullanıcının kendi duraklamalarıyla 5 dakikalık ve 1 saatlik önbellek.

Claude Code önbelleğe ana konuşmada abonelikte 1 saatlik, API anahtarıyla ve alt ajanlarda 5
dakikalık ömürle yazar; `promptCacheTtl` ve `subagentPromptCacheTtl` bunu değiştirir. 1 saatlik
yazım 2×, 5 dakikalık 1.25× fiyatlıdır, ama iki istek arasındaki boşluk ömrü aşarsa sonraki istek
bütün bağlamı yeniden yazar. Hangisinin ucuz olduğu duraklamalara ve bağlamın büyüklüğüne bağlıdır.

Her istek iki ömür için yeniden fiyatlanır: önceki istekte önbelleğe giren bağlam (girdisi ve
yanıtı) boşluk ömrü aşmadıysa okunur, aşmışsa yeniden yazılır; geri kalan girdi yazılır. Aynı
yeniden oynatma gözlenen ömürle kayıttaki maliyeti verir; farkı yöntemin hata payıdır, çünkü
araç ya da model değişikliği gibi diğer önbellek kırılmaları burada görünmez. Öneri yalnızca diğer
ömür bu hata payından daha fazla ucuzsa verilir.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from cimrihook.scan import Request, TranscriptScan, token_costs
from cimrihook.simulate import claude_prices, context_of, usd_per_token

FIVE_MINUTES: Final = "5m"
ONE_HOUR: Final = "1h"
LIFETIME_SECONDS: Final = {FIVE_MINUTES: 300.0, ONE_HOUR: 3_600.0}
MAIN: Final = "main sessions"
SUBAGENTS: Final = "subagents"
# Önerinin geçmesi gereken en küçük fark: yeniden oynatmanın gözlenen ömürdeki hatasının üstünde
# olmalı ve en az bu kadar.
MIN_GAIN: Final = 0.05


@dataclass(frozen=True, slots=True)
class LifetimeReplay:
    """Bir istek grubunun önbellek girdisi maliyeti: kayıttaki ve iki ömürle yeniden oynatılmış."""

    bucket: str  # ana oturumlar ya da alt ajanlar
    requests: int
    current: str | None  # kayıtlarda baskın ömür; önbellek yazımı yoksa None
    observed_usd: float  # okuma, yazma ve önbelleksiz girdi (çıktı iki ömürde de aynı)
    five_minutes_usd: float
    one_hour_usd: float

    def replayed(self, lifetime: str) -> float:
        """Verilen ömürle yeniden oynatılmış maliyet."""
        return self.five_minutes_usd if lifetime == FIVE_MINUTES else self.one_hour_usd

    def replay_error(self) -> float | None:
        """Gözlenen ömürle yeniden oynatmanın kayıttaki maliyetten göreli farkı."""
        if self.current is None or self.observed_usd <= 0:
            return None
        return (self.replayed(self.current) - self.observed_usd) / self.observed_usd


def replay_lifetimes(scans: Sequence[TranscriptScan]) -> tuple[LifetimeReplay, LifetimeReplay]:
    """Ana oturumlar ve alt ajanlar için iki ömürle yeniden fiyatlama."""
    return (
        bucket_replay(MAIN, [scan for scan in scans if not is_subagent(scan)]),
        bucket_replay(SUBAGENTS, [scan for scan in scans if is_subagent(scan)]),
    )


def is_subagent(scan: TranscriptScan) -> bool:
    """Transcript bir alt ajanın mı (istekleri alt ajan olarak işaretli)?"""
    return any(request.subagent for request in scan.requests)


def bucket_replay(bucket: str, scans: Sequence[TranscriptScan]) -> LifetimeReplay:
    """Bir grubun her transcript'ini sırayla yeniden fiyatlar ve toplar."""
    priced = [
        (request, cached, base)
        for scan in scans
        for request, cached, base in priced_requests(scan.requests)
    ]
    five_written = sum(request.usage.write_5m for request, _, _ in priced)
    hour_written = sum(request.usage.write_1h for request, _, _ in priced)
    current = (
        None
        if five_written + hour_written == 0
        else (ONE_HOUR if hour_written > five_written else FIVE_MINUTES)
    )
    return LifetimeReplay(
        bucket=bucket,
        requests=len(priced),
        current=current,
        observed_usd=sum(sum(token_costs(request, base)[:3]) for request, _, base in priced),
        five_minutes_usd=sum(
            replayed_cost(request, cached, base, FIVE_MINUTES) for request, cached, base in priced
        ),
        one_hour_usd=sum(
            replayed_cost(request, cached, base, ONE_HOUR) for request, cached, base in priced
        ),
    )


def priced_requests(requests: Sequence[Request]) -> list[tuple[Request, int, float]]:
    """Fiyatı bilinen istekler, önceki istekte önbelleğe giren bağlam ve USD/taban token.

    Fiyatı bilinmeyen bir istek zinciri koparır: sonraki isteğin önceki bağlamı bilinmez sayılır.
    """
    result: list[tuple[Request, int, float]] = []
    previous: int | None = None
    for request in requests:
        base = usd_per_token(request.model)
        if base is None:
            previous = None
            continue
        result.append((request, 0 if previous is None or request.first else previous, base))
        previous = context_of(request.usage) + request.usage.output
    return result


def replayed_cost(request: Request, cached_before: int, base: float, lifetime: str) -> float:
    """İsteğin girdi maliyeti, önbellek bu ömürle yazılsaydı (USD)."""
    prices = claude_prices(request.model)
    context = context_of(request.usage)
    warm = request.gap_seconds is not None and request.gap_seconds <= LIFETIME_SECONDS[lifetime]
    cached = min(cached_before, context) if warm else 0
    write = prices.write_5m if lifetime == FIVE_MINUTES else prices.write_1h
    return (cached * prices.read + (context - cached) * write) * base


def recommended_lifetime(replay: LifetimeReplay) -> str | None:
    """Önerilen ömür: diğeri hata payından ve MIN_GAIN'den fazla ucuzsa o, değilse None."""
    error = replay.replay_error()
    if replay.current is None or error is None:
        return None
    other = ONE_HOUR if replay.current == FIVE_MINUTES else FIVE_MINUTES
    now, then = replay.replayed(replay.current), replay.replayed(other)
    gain = (now - then) / now if now > 0 else 0.0
    return other if gain > max(MIN_GAIN, abs(error)) else None
