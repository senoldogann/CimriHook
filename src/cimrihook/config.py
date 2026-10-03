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
SWITCHABLE: Final = frozenset({Encoding.REF, Encoding.DELTA, Encoding.OUTLINE})


@dataclass(frozen=True, slots=True)
class Config:
    """Çalışma zamanı yapılandırması."""

    home: Path  # defter veritabanının bulunduğu dizin
    codec: CodecConfig


def load_config(env: Mapping[str, str]) -> Config:
    """CIMRIHOOK_* ortam değişkenlerini okur; tanımsız olanlar için belgelenmiş varsayılanlar."""
    home = Path(env["CIMRIHOOK_HOME"]) if "CIMRIHOOK_HOME" in env else Path.home() / ".cimrihook"
    return Config(
        home=home,
        codec=CodecConfig(
            enabled=SWITCHABLE - parse_disabled(env.get("CIMRIHOOK_DISABLE", "")),
            outline_min_tokens=parse_int(
                env, "CIMRIHOOK_OUTLINE_MIN_TOKENS", DEFAULT_OUTLINE_MIN_TOKENS
            ),
            delta_max_ratio=parse_ratio(env, "CIMRIHOOK_DELTA_MAX_RATIO", DEFAULT_DELTA_MAX_RATIO),
            min_saving_tokens=parse_int(
                env, "CIMRIHOOK_MIN_SAVING_TOKENS", DEFAULT_MIN_SAVING_TOKENS
            ),
        ),
    )


def parse_disabled(raw: str) -> frozenset[Encoding]:
    """Virgülle ayrılmış kodlama adlarını (ref,delta,outline) çözer."""
    names = [name.strip().lower() for name in raw.split(",") if name.strip()]
    allowed = {encoding.value: encoding for encoding in SWITCHABLE}
    unknown = [name for name in names if name not in allowed]
    if unknown:
        raise ConfigError(
            f"CIMRIHOOK_DISABLE has unknown encodings {unknown}; allowed: {sorted(allowed)}"
        )
    return frozenset(allowed[name] for name in names)


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
