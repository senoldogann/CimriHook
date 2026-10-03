"""Durum satırı: oturumun bağlam ekonomisi ve kullanım limiti, her güncellemede tek satır.

Claude Code durum satırı komutuna oturumun durumunu JSON olarak verir: modeli, bağlamdaki token
sayısını (son isteğin girdi, önbellek okuma ve yazma toplamı), oturum maliyetini, abonelikte 5
saatlik ve 7 günlük kullanım limitlerini ve kendi önbellek izlemesini (prompt_cache: sıcak mı,
ömrü, ne zaman soğuyacağı, soğursa kaç token yeniden yazılacak). Claude Code satırı önbelleğin
soğuduğu anda da yeniden çalıştırır. Bir sonraki isteğin tahmini maliyeti önbellek sıcaksa bağlamın
okunması, soğuksa yeniden yazılmasıdır. Kullanım limiti gözlemleri, planın token türlerini nasıl
saydığını öğrenmek için deftere yazılır.

Kullanıcının önceki durum satırı komutu varsa önce o çalışır ve çıktısının tüm satırları korunur.
Claude Code sıfır olmayan çıkışta satırı tamamen sildiği için CimriHook'un kendi hatası satırın
içinde gösterilir; böylece ne kullanıcının satırı kaybolur ne de hata görünmez kalır.
"""

import contextlib
import json
import os
import signal
import subprocess
from dataclasses import dataclass
from typing import Final

from cimrihook.claude import as_object, require_str
from cimrihook.config import Config
from cimrihook.doctor import usd_per_token
from cimrihook.errors import CimriHookError, HookPayloadError
from cimrihook.hook import ledger_path
from cimrihook.ledger import record_new_quota
from cimrihook.model import QuotaSample
from cimrihook.simulate import claude_prices

LIMIT_LABELS: Final = (("five_hour", "5h"), ("seven_day", "7d"))
CACHE_TTLS: Final = {"5m": 300.0, "1h": 3_600.0}  # prompt_cache.ttl değerleri, saniye
SEPARATOR: Final = " · "
CHAIN_TIMEOUT_SECONDS: Final = 5.0  # kullanıcının önceki durum satırı komutuna tanınan süre
STATUS_BUSY_TIMEOUT_SECONDS: Final = 0.25  # durum satırı defter kilidini uzun beklemez
ERROR_CHARS: Final = 120  # satırda gösterilen hata metninin en fazla uzunluğu


@dataclass(frozen=True, slots=True)
class LimitUse:
    """Bir kullanım limiti penceresinin doluluğu."""

    window: str  # five_hour ya da seven_day
    used_percentage: float
    resets_at: int  # epoch saniye


@dataclass(frozen=True, slots=True)
class PromptCache:
    """Claude Code'un oturum için tuttuğu önbellek durumu."""

    warm: bool
    ttl_seconds: float
    expires_at: float | None  # epoch saniye
    recache_tokens: int | None  # önbellek soğuksa bir sonraki isteğin yeniden yazacağı token


@dataclass(frozen=True, slots=True)
class StatusInput:
    """Durum satırı komutunun girdisinden kullanılan alanlar."""

    session_id: str
    model: str
    context_tokens: int | None  # bağlamdaki token; ilk yanıttan önce ve /compact sonrası None
    session_usd: float | None
    limits: tuple[LimitUse, ...]
    cache: PromptCache | None  # ilk istekten önce Claude Code vermez


def run_chained_statusline(raw: str, config: Config, now: float, previous_command: str) -> str:
    """Kullanıcının önceki durum satırını aynı girdiyle çalıştırır ve CimriHook'unkini ekler.

    Önceki komut (ör. başka bir aracın köprüsü) girdiyi kendisi de kullanabilir; ona aynen iletilir.
    """
    return joined(previous_status(raw, previous_command), status_or_error(raw, config, now))


def previous_status(raw: str, previous_command: str) -> str:
    """Önceki durum satırı komutunun çıktısı; çıkış kodu ya da süre aşımı metne eklenir.

    Komut kendi süreç grubunda çalışır; süre aşımında başlattığı tüm süreçler sonlandırılır.
    """
    with subprocess.Popen(
        previous_command,
        shell=True,  # kullanıcının kendi ayarındaki komut, Claude Code'un çalıştırdığı gibi
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    ) as process:
        try:
            stdout, _ = process.communicate(raw.encode("utf-8"), timeout=CHAIN_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):  # grup kendiliğinden bitmiş olabilir
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            return f"(previous status line timed out after {CHAIN_TIMEOUT_SECONDS:.0f}s)"
    text = stdout.decode("utf-8", errors="replace").rstrip()
    if process.returncode != 0:
        return f"{text} (previous status line exited {process.returncode})".strip()
    return text


def joined(previous: str, ours: str) -> str:
    """Önceki çıktının satırları korunur; CimriHook'un parçası son satırın sonuna eklenir."""
    lines = previous.split("\n") if previous else []
    if not lines:
        return ours
    last = SEPARATOR.join(part for part in (lines[-1], ours) if part)
    return "\n".join([*lines[:-1], last])


def status_or_error(raw: str, config: Config, now: float) -> str:
    """CimriHook'un durum satırı; kendi hatası satırda kısa bir iletiyle gösterilir."""
    try:
        return run_statusline(raw, config, now)
    except CimriHookError as error:
        return f"cimrihook: {str(error)[:ERROR_CHARS]}"


def run_statusline(raw: str, config: Config, now: float) -> str:
    """Durum satırını üretir ve yeni kullanım limiti gözlemlerini deftere yazar."""
    status = parse_status_input(raw)
    samples = quota_samples(status, now)
    if samples:
        record_new_quota(ledger_path(config.home), samples, STATUS_BUSY_TIMEOUT_SECONDS)
    return render_status(status, now)


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
    context = optional_integer(payload.get("context_window"), "total_input_tokens")
    return StatusInput(
        session_id=require_str(payload, "session_id", "statusline"),
        model=require_str(model, "id", "statusline.model"),
        context_tokens=context if context else None,  # 0: henüz yanıt yok ya da yeni sıkıştırıldı
        session_usd=optional_number(payload.get("cost"), "total_cost_usd"),
        limits=tuple(
            use for window, _ in LIMIT_LABELS if (use := limit_use(limits, window)) is not None
        ),
        cache=prompt_cache(payload.get("prompt_cache")),
    )


def prompt_cache(value: object) -> PromptCache | None:
    """Girdideki prompt_cache; Claude Code ilk istekten önce bu alanı vermez."""
    if value is None:
        return None
    cache = as_object(value, "statusline.prompt_cache")
    warm = cache.get("warm")
    ttl = cache.get("ttl")
    if not isinstance(warm, bool) or not isinstance(ttl, str) or ttl not in CACHE_TTLS:
        raise HookPayloadError(f"statusline.prompt_cache: unexpected warm {warm!r} or ttl {ttl!r}")
    return PromptCache(
        warm=warm,
        ttl_seconds=CACHE_TTLS[ttl],
        expires_at=optional_number(cache, "expires_at"),
        recache_tokens=optional_integer(cache, "recache_tokens_if_cold"),
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


def render_status(status: StatusInput, now: float) -> str:
    """Durum satırının metni; bilinmeyen parçalar atlanır."""
    context = status.context_tokens
    parts = [] if context is None else [f"{compact_tokens(context)} ctx"]
    if status.cache is not None:
        parts.extend(cache_parts(status.model, context, status.cache, now))
    labels = dict(LIMIT_LABELS)
    parts.extend(f"{labels[use.window]} {use.used_percentage:.0f}%" for use in status.limits)
    if status.session_usd is not None:
        parts.append(f"${status.session_usd:.2f}")
    return SEPARATOR.join(parts)


def cache_parts(model: str, context: int | None, cache: PromptCache, now: float) -> list[str]:
    """Önbellek sıcaklığı ve bir sonraki isteğin bağlam maliyeti (fiyatı biliniyorsa).

    Sıcak önbellekte bağlam okunur; soğuk önbellekte Claude Code'un tahmin ettiği token yeniden
    yazılır (yazma fiyatı önbelleğin ömrüne göre).
    """
    prices = claude_prices(model)
    base = usd_per_token(model)
    left = None if cache.expires_at is None else cache.expires_at - now
    if cache.warm and left is not None and left > 0:
        state = f"cache warm {duration(left)}"
        tokens = context
        weight = prices.read
    else:
        state = "cache cold"
        tokens = cache.recache_tokens
        weight = prices.write_1h if cache.ttl_seconds >= CACHE_TTLS["1h"] else prices.write_5m
    if base is None or tokens is None:
        return [state]
    return [state, f"next ${tokens * weight * base:.2f}"]


def compact_tokens(tokens: int) -> str:
    """Token sayısının kısa hali (ör. 412k, 1.05M)."""
    return f"{tokens / 1000:.0f}k" if tokens < 1_000_000 else f"{tokens / 1e6:.2f}M"


def duration(seconds: float) -> str:
    """Sürenin kısa hali (ör. 38m, 45s)."""
    return f"{int(seconds // 60)}m" if seconds >= 60 else f"{int(seconds)}s"
