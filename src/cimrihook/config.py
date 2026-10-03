"""Ortam değişkenlerinden CimriHook yapılandırması."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from cimrihook.codec import CodecConfig
from cimrihook.errors import ConfigError
from cimrihook.model import Encoding

DEFAULT_OUTLINE_MIN_TOKENS: Final = 6000
DEFAULT_DELTA_MAX_RATIO: Final = 0.5
DEFAULT_MIN_SAVING_TOKENS: Final = 150
# Soğuk istem koruması bu bağlamın altında durdurmaz: Opus 5.x'te 1 saatlik yeniden yazım ~$1.2.
DEFAULT_GUARD_MIN_TOKENS: Final = 150_000
SWITCHABLE: Final = frozenset({Encoding.REF, Encoding.DELTA, Encoding.OUTLINE})
GUARD: Final = "guard"


@dataclass(frozen=True, slots=True)
class Config:
    """Çalışma zamanı yapılandırması."""

    home: Path  # defter veritabanının bulunduğu dizin
    codec: CodecConfig
    guard_enabled: bool
    guard_min_tokens: int


def load_config(env: Mapping[str, str]) -> Config:
    """CIMRIHOOK_* ortam değişkenlerini okur; tanımsız olanlar için belgelenmiş varsayılanlar."""
    home = Path(env["CIMRIHOOK_HOME"]) if "CIMRIHOOK_HOME" in env else Path.home() / ".cimrihook"
    disabled = parse_disabled(env.get("CIMRIHOOK_DISABLE", ""))
    return Config(
        home=home,
        guard_enabled=GUARD not in disabled,
        guard_min_tokens=parse_int(env, "CIMRIHOOK_GUARD_MIN_TOKENS", DEFAULT_GUARD_MIN_TOKENS),
        codec=CodecConfig(
            enabled=frozenset(
                encoding for encoding in SWITCHABLE if encoding.value not in disabled
            ),
            outline_min_tokens=parse_int(
                env, "CIMRIHOOK_OUTLINE_MIN_TOKENS", DEFAULT_OUTLINE_MIN_TOKENS
            ),
            delta_max_ratio=parse_ratio(env, "CIMRIHOOK_DELTA_MAX_RATIO", DEFAULT_DELTA_MAX_RATIO),
            min_saving_tokens=parse_int(
                env, "CIMRIHOOK_MIN_SAVING_TOKENS", DEFAULT_MIN_SAVING_TOKENS
            ),
        ),
    )


def parse_disabled(raw: str) -> frozenset[str]:
    """Virgülle ayrılmış kapatılacak parçalar: kodlamalar (ref,delta,outline) ve guard."""
    names = [name.strip().lower() for name in raw.split(",") if name.strip()]
    allowed = {encoding.value for encoding in SWITCHABLE} | {GUARD}
    unknown = [name for name in names if name not in allowed]
    if unknown:
        raise ConfigError(
            f"CIMRIHOOK_DISABLE has unknown names {unknown}; allowed: {sorted(allowed)}"
        )
    return frozenset(names)


def parse_int(env: Mapping[str, str], key: str, default: int) -> int:
    """Negatif olmayan tam sayı ortam değişkeni."""
    if key not in env:
        return default
    raw = env[key]
    if not raw.isdigit():
        raise ConfigError(f"{key} must be a non-negative integer, got {raw!r}")
    return int(raw)


def parse_ratio(env: Mapping[str, str], key: str, default: float) -> float:
    """(0, 1] aralığında oran ortam değişkeni."""
    if key not in env:
        return default
    raw = env[key]
    try:
        value = float(raw)
    except ValueError as error:
        raise ConfigError(f"{key} must be a number in (0, 1], got {raw!r}") from error
    if not 0 < value <= 1:
        raise ConfigError(f"{key} must be in (0, 1], got {value}")
    return value
