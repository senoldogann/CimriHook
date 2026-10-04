"""CimriHook's typed data models."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class QuotaSample:
    """One observation of a subscription usage limit (from the Claude Code status line input)."""

    window: str  # five_hour or seven_day
    resets_at: int  # when the window resets (epoch seconds)
    used_percentage: float
    taken_at: float
    session_id: str
    model: str
