"""Transcript'in sonundan oturumun son durumu: son yanıtın zamanı, önbellek ömrü ve bağlamı.

Durum satırı ve soğuk istem koruması, bütün transcript'i okumadan son birkaç yüz kilobayttan
yararlanır. Önbellek ömrü son önbellek yazımının türünden (5 dakika / 1 saat) gelir; ömür her
istekte yenilendiğinden önbellek son yanıttan bu kadar süre sonra soğur.
"""

import os
from dataclasses import dataclass
from typing import Final

from cimrihook.audit import Usage, message_usage, parse_line
from cimrihook.claude import JsonObject
from cimrihook.doctor import entry_time
from cimrihook.simulate import SYNTHETIC_MODEL, context_of

TAIL_BYTES: Final = 262_144  # son istekleri bulmak için transcript'in okunan son kısmı
ONE_HOUR: Final = 3_600.0
FIVE_MINUTES: Final = 300.0


@dataclass(frozen=True, slots=True)
class SessionTail:
    """Oturumun son API yanıtına göre durumu."""

    last_response_at: float  # epoch saniye
    ttl_seconds: float  # önbellek ömrü
    context_tokens: int  # bir sonraki isteğin taşıyacağı bağlam (son istek + yanıtı)
    model: str


def read_session_tail(transcript_path: str) -> SessionTail | None:
    """Transcript'in sonundan oturum durumu; yanıt ya da önbellek yazımı bulunamazsa None."""
    try:
        size = os.path.getsize(transcript_path)
    except FileNotFoundError:
        return None
    with open(transcript_path, "rb") as handle:
        handle.seek(max(0, size - TAIL_BYTES))
        lines = handle.read().split(b"\n")
    responses = [
        (entry, usage)
        for raw in reversed(lines)
        if (entry := parse_line(raw)) is not None and (usage := response_usage(entry)) is not None
    ]
    written = next((usage for _, usage in responses if usage.write_5m + usage.write_1h > 0), None)
    if not responses or written is None:
        return None
    entry, usage = responses[0]
    when = entry_time(entry)
    message = entry.get("message")
    model = message.get("model") if isinstance(message, dict) else None
    if when is None or not isinstance(model, str):
        return None
    return SessionTail(
        last_response_at=when,
        ttl_seconds=ONE_HOUR if written.write_1h > 0 else FIVE_MINUTES,
        context_tokens=context_of(usage) + usage.output,
        model=model,
    )


def response_usage(entry: JsonObject) -> Usage | None:
    """API'den gelmiş asistan satırının kullanımı; yerel ya da kullanımsız satırda None."""
    message = entry.get("message")
    if entry.get("type") != "assistant" or not isinstance(message, dict):
        return None
    if message.get("model") == SYNTHETIC_MODEL:
        return None
    return message_usage(message)
