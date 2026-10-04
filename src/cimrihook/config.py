"""CimriHook configuration from environment variables."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from cimrihook.errors import ConfigError

# The cold-prompt guard stays quiet below this context: on Opus 5.x re-writing a 1-hour cache costs
# about $1.2 there.
DEFAULT_GUARD_MIN_TOKENS: Final = 150_000
GUARD: Final = "guard"


@dataclass(frozen=True, slots=True)
class Config:
    """Runtime configuration."""

    home: Path  # directory of the ledger database
    guard_enabled: bool
    guard_min_tokens: int


def load_config(env: Mapping[str, str]) -> Config:
    """Reads the CIMRIHOOK_* environment variables; documented defaults for the unset ones."""
    home = Path(env["CIMRIHOOK_HOME"]) if "CIMRIHOOK_HOME" in env else Path.home() / ".cimrihook"
    disabled = parse_disabled(env.get("CIMRIHOOK_DISABLE", ""))
    return Config(
        home=home,
        guard_enabled=GUARD not in disabled,
        guard_min_tokens=parse_int(env, "CIMRIHOOK_GUARD_MIN_TOKENS", DEFAULT_GUARD_MIN_TOKENS),
    )


def parse_disabled(raw: str) -> frozenset[str]:
    """Comma-separated parts to switch off; the only part that can be switched off is `guard`."""
    names = [name.strip().lower() for name in raw.split(",") if name.strip()]
    unknown = [name for name in names if name != GUARD]
    if unknown:
        raise ConfigError(f"CIMRIHOOK_DISABLE has unknown names {unknown}; allowed: {[GUARD]}")
    return frozenset(names)


def parse_int(env: Mapping[str, str], key: str, default: int) -> int:
    """Non-negative integer environment variable."""
    if key not in env:
        return default
    raw = env[key]
    if not raw.isdigit():
        raise ConfigError(f"{key} must be a non-negative integer, got {raw!r}")
    return int(raw)
