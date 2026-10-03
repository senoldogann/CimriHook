"""Durum satırı: oturumun bağlam ekonomisi ve kullanım limiti, her güncellemede tek satır.

Claude Code durum satırı komutuna oturumun durumunu JSON olarak verir: modeli, bağlamdaki token
sayısını (önbellek okuma ve yazmaları dahil), oturum maliyetini ve abonelikte 5 saatlik ve 7 günlük
kullanım limitlerini. Önbelleğin ne zaman soğuyacağı girdide yoktur; son isteğin zamanı ve
önbelleğe hangi ömürle (5 dakika / 1 saat) yazıldığı transcript'in sonundan okunur. Bir sonraki
isteğin tahmini maliyeti önbellek sıcaksa bağlamın okunması, soğuksa yeniden yazılmasıdır.
Kullanım limiti gözlemleri, planın token türlerini nasıl saydığını öğrenmek için deftere yazılır.
"""

import json
from dataclasses import dataclass
from typing import Final

from cimrihook.claude import as_object, require_str
from cimrihook.config import Config
from cimrihook.doctor import usd_per_token
from cimrihook.errors import HookPayloadError
from cimrihook.hook import ledger_path
from cimrihook.ledger import Ledger
from cimrihook.model import QuotaSample
from cimrihook.simulate import claude_prices
from cimrihook.tail import ONE_HOUR, SessionTail, read_session_tail

LIMIT_LABELS: Final = (("five_hour", "5h"), ("seven_day", "7d"))
SEPARATOR: Final = " · "


@dataclass(frozen=True, slots=True)
class LimitUse:
    """Bir kullanım limiti penceresinin doluluğu."""

    window: str  # five_hour ya da seven_day
    used_percentage: float
    resets_at: int  # epoch saniye


@dataclass(frozen=True, slots=True)
class StatusInput:
    """Durum satırı komutunun girdisinden kullanılan alanlar."""

    session_id: str
    transcript_path: str
    model: str
    context_tokens: int | None  # bağlamdaki token (önbellek dahil); ilk yanıttan önce None
    session_usd: float | None
    limits: tuple[LimitUse, ...]


def run_statusline(raw: str, config: Config, now: float) -> str:
    """Durum satırını üretir ve kullanım limiti gözlemlerini deftere yazar."""
    status = parse_status_input(raw)
    samples = quota_samples(status, now)
    if samples:
        with Ledger(ledger_path(config.home)) as ledger:
            ledger.record_quota(samples)
    return render_status(status, read_session_tail(status.transcript_path), now)


def parse_status_input(raw: str) -> StatusInput:
    """stdin'deki durum satırı girdisini ayrıştırır; isteğe bağlı alanlar yoksa None."""
    try:
        decoded: object = json.loads(raw)
    except json.JSONDecodeError as error:
        raise HookPayloadError(
            f"statusline input is not valid JSON ({error.msg}); first bytes: {raw[:120]!r}"
        ) from error
    payload = as_object(decoded, "statusline")
    model = as_object(payload.get("model"), "statusline.model")
    limits = payload.get("rate_limits")
    return StatusInput(
        session_id=require_str(payload, "session_id", "statusline"),
        transcript_path=require_str(payload, "transcript_path", "statusline"),
        model=require_str(model, "id", "statusline.model"),
        context_tokens=optional_integer(payload.get("context_window"), "total_input_tokens"),
        session_usd=optional_number(payload.get("cost"), "total_cost_usd"),
        limits=tuple(
            use for window, _ in LIMIT_LABELS if (use := limit_use(limits, window)) is not None
        ),
    )


def optional_integer(container: object, key: str) -> int | None:
    """Sözlükteki tam sayı alanı; sözlük ya da alan yoksa None."""
    if not isinstance(container, dict):
        return None
    value = container.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def optional_number(container: object, key: str) -> float | None:
    """Sözlükteki sayı alanı; sözlük ya da alan yoksa None."""
    if not isinstance(container, dict):
        return None
    value = container.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def limit_use(limits: object, window: str) -> LimitUse | None:
    """rate_limits içindeki bir pencere; abonelik dışında ya da pencere bitmişse None."""
    entry = limits.get(window) if isinstance(limits, dict) else None
    used = optional_number(entry, "used_percentage")
    resets_at = optional_integer(entry, "resets_at")
    if used is None or resets_at is None:
        return None
    return LimitUse(window=window, used_percentage=used, resets_at=resets_at)


def quota_samples(status: StatusInput, now: float) -> tuple[QuotaSample, ...]:
    """Girdideki kullanım limiti doluluklarının defter kaydı."""
    return tuple(
        QuotaSample(
            window=use.window,
            resets_at=use.resets_at,
            used_percentage=use.used_percentage,
            taken_at=now,
            session_id=status.session_id,
            model=status.model,
        )
        for use in status.limits
    )


def render_status(status: StatusInput, tail: SessionTail | None, now: float) -> str:
    """Durum satırının metni; bilinmeyen parçalar atlanır."""
    context = status.context_tokens
    parts = [] if context is None else [f"{compact_tokens(context)} ctx"]
    if tail is not None and context is not None:
        parts.extend(cache_parts(status.model, context, tail, now))
    labels = dict(LIMIT_LABELS)
    parts.extend(f"{labels[use.window]} {use.used_percentage:.0f}%" for use in status.limits)
    if status.session_usd is not None:
        parts.append(f"${status.session_usd:.2f}")
    return SEPARATOR.join(parts)


def cache_parts(model: str, context: int, tail: SessionTail, now: float) -> list[str]:
    """Önbellek sıcaklığı ve bir sonraki isteğin bağlam maliyeti (fiyatı biliniyorsa)."""
    left = tail.ttl_seconds - (now - tail.last_response_at)
    prices = claude_prices(model)
    base = usd_per_token(model)
    if left > 0:
        state = f"cache warm {duration(left)}"
        weight = prices.read
    else:
        state = "cache cold"
        weight = prices.write_1h if tail.ttl_seconds >= ONE_HOUR else prices.write_5m
    return [state] if base is None else [state, f"next ${context * weight * base:.2f}"]


def compact_tokens(tokens: int) -> str:
    """Token sayısının kısa hali (ör. 412k, 1.05M)."""
    return f"{tokens / 1000:.0f}k" if tokens < 1_000_000 else f"{tokens / 1e6:.2f}M"


def duration(seconds: float) -> str:
    """Sürenin kısa hali (ör. 38m, 45s)."""
    return f"{int(seconds // 60)}m" if seconds >= 60 else f"{int(seconds)}s"
