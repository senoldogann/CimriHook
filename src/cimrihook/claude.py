"""Typed access to the JSON Claude Code hands to hook and status line commands."""

from collections.abc import Mapping

from cimrihook.errors import HookPayloadError

type JsonObject = dict[str, object]


def as_object(value: object, where: str) -> JsonObject:
    """Expects a JSON object."""
    if not isinstance(value, dict):
        raise HookPayloadError(f"{where}: expected a JSON object, got {type(value).__name__}")
    return {str(key): item for key, item in value.items()}


def require_str(obj: Mapping[str, object], key: str, where: str) -> str:
    """Required string field."""
    value = obj.get(key)
    if not isinstance(value, str):
        raise HookPayloadError(f"{where}.{key}: expected a string, got {value!r:.80}")
    return value


def optional_str(obj: Mapping[str, object], key: str, where: str) -> str | None:
    """Optional string field."""
    value = obj.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise HookPayloadError(f"{where}.{key}: expected a string, got {value!r:.80}")
    return value
