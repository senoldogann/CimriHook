"""Durum satırı: oturumun bağlam ekonomisi ve kullanım limiti, her güncellemede tek satır.

Claude Code durum satırı komutuna oturumun durumunu JSON olarak verir: modeli, bağlamdaki token
sayısını (önbellek okuma ve yazmaları dahil), oturum maliyetini ve abonelikte 5 saatlik ve 7 günlük
kullanım limitlerini. Önbelleğin ne zaman soğuyacağı girdide yoktur; son isteğin zamanı ve
önbelleğe hangi ömürle (5 dakika / 1 saat) yazıldığı transcript'in sonundan okunur. Bir sonraki
isteğin tahmini maliyeti önbellek sıcaksa bağlamın okunması, soğuksa yeniden yazılmasıdır.
Kullanım limiti gözlemleri, planın token türlerini nasıl saydığını öğrenmek için deftere yazılır.
"""

import json
import os
from dataclasses import dataclass
from typing import Final

from cimrihook.audit import message_usage, parse_line
from cimrihook.claude import JsonObject, as_object, require_str
from cimrihook.config import Config
from cimrihook.doctor import entry_time, usd_per_token
from cimrihook.errors import HookPayloadError
from cimrihook.hook import ledger_path
from cimrihook.ledger import Ledger
from cimrihook.model import QuotaSample
from cimrihook.simulate import SYNTHETIC_MODEL, claude_prices

TAIL_BYTES: Final = 262_144  # son isteği bulmak için transcript'in okunan son kısmı
ONE_HOUR: Final = 3_600.0
FIVE_MINUTES: Final = 300.0
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


@dataclass(frozen=True, slots=True)
class CacheState:
    """Önbelleğin durumu: son yanıtın zamanı ve önbellek ömrü."""

    last_response_at: float  # epoch saniye
    ttl_seconds: float


def run_statusline(raw: str, config: Config, now: float) -> str:
    """Durum satırını üretir ve kullanım limiti gözlemlerini deftere yazar."""
    status = parse_status_input(raw)
    samples = quota_samples(status, now)
    if samples:
        with Ledger(ledger_path(config.home)) as ledger:
            ledger.record_quota(samples)
    return render_status(status, read_cache_state(status.transcript_path), now)


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
    context = payload.get("context_window")
    cost = payload.get("cost")
    limits = payload.get("rate_limits")
    return StatusInput(
        session_id=require_str(payload, "session_id", "statusline"),
        transcript_path=require_str(payload, "transcript_path", "statusline"),
        model=require_str(model, "id", "statusline.model"),
        context_tokens=optional_integer(context, "total_input_tokens"),
        session_usd=optional_number(cost, "total_cost_usd"),
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


def read_cache_state(transcript_path: str) -> CacheState | None:
    """Transcript'in sonundan son yanıtın zamanı ve önbelleğe yazım ömrü; bulunamazsa None."""
    try:
        size = os.path.getsize(transcript_path)
    except FileNotFoundError:
        return None
    with open(transcript_path, "rb") as handle:
        handle.seek(max(0, size - TAIL_BYTES))
        lines = handle.read().split(b"\n")
    entries = [entry for raw in reversed(lines) if (entry := parse_line(raw)) is not None]
    responses = [entry for entry in entries if is_response(entry)]
    last = responses[0] if responses else None
    written = next((entry for entry in responses if cache_written(entry) > 0), None)
    when = None if last is None else entry_time(last)
    if when is None or written is None:
        return None
    ttl = ONE_HOUR if cache_written_1h(written) > 0 else FIVE_MINUTES
    return CacheState(last_response_at=when, ttl_seconds=ttl)


def is_response(entry: JsonObject) -> bool:
    """API'den gelmiş, kullanım verisi taşıyan asistan satırı mı?"""
    message = entry.get("message")
    return (
        entry.get("type") == "assistant"
        and isinstance(message, dict)
        and message.get("model") != SYNTHETIC_MODEL
        and message_usage(message) is not None
    )


def cache_written(entry: JsonObject) -> int:
    """Asistan satırının önbelleğe yazdığı token."""
    message = entry.get("message")
    usage = message_usage(message) if isinstance(message, dict) else None
    return 0 if usage is None else usage.write_5m + usage.write_1h


def cache_written_1h(entry: JsonObject) -> int:
    """Asistan satırının bir saatlik ömürle yazdığı token."""
    message = entry.get("message")
    usage = message_usage(message) if isinstance(message, dict) else None
    return 0 if usage is None else usage.write_1h


def render_status(status: StatusInput, cache: CacheState | None, now: float) -> str:
    """Durum satırının metni; bilinmeyen parçalar atlanır."""
    context = status.context_tokens
    parts = [] if context is None else [f"{compact_tokens(context)} ctx"]
    if cache is not None and context is not None:
        parts.extend(cache_parts(status.model, context, cache, now))
    labels = dict(LIMIT_LABELS)
    parts.extend(f"{labels[use.window]} {use.used_percentage:.0f}%" for use in status.limits)
    if status.session_usd is not None:
        parts.append(f"${status.session_usd:.2f}")
    return SEPARATOR.join(parts)


def cache_parts(model: str, context: int, cache: CacheState, now: float) -> list[str]:
    """Önbellek sıcaklığı ve bir sonraki isteğin bağlam maliyeti (fiyatı biliniyorsa)."""
    left = cache.ttl_seconds - (now - cache.last_response_at)
    prices = claude_prices(model)
    base = usd_per_token(model)
    if left > 0:
        state = f"cache warm {duration(left)}"
        weight = prices.read
    else:
        state = "cache cold"
        weight = prices.write_1h if cache.ttl_seconds >= ONE_HOUR else prices.write_5m
    return [state] if base is None else [state, f"next ${context * weight * base:.2f}"]


def compact_tokens(tokens: int) -> str:
    """Token sayısının kısa hali (ör. 412k, 1.05M)."""
    return f"{tokens / 1000:.0f}k" if tokens < 1_000_000 else f"{tokens / 1e6:.2f}M"


def duration(seconds: float) -> str:
    """Kalan sürenin kısa hali (ör. 38m, 45s)."""
    return f"{int(seconds // 60)}m" if seconds >= 60 else f"{int(seconds)}s"
