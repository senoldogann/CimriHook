"""Transcript sonundan yanıt zamanı, cache süresi ve bağlamı oku.

Guard tüm dosyayı okumaz; son yanıt bulunana kadar okunan kısmı büyütür. TTL son cache
yazımının türünden gelir (5 dakika / 1 saat), fakat istek başlangıcından itibaren işler.
Yanıtın transcript zaman damgası istek başlangıcını vermez: guard'ın yanıt yaşı kontrolü
geç kalabilen temkinli bir soğuk-cache uyarısıdır, kesin sıcaklık ölçümü değildir.
Son yanıttan sonra compact sınırı varsa eski bağlam boyutu artık geçersizdir ve durum
bilinmiyor sayılır. Statusline, sağlayıcının kendi prompt_cache bilgisini kullanır.
"""

import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from cimrihook.claude import JsonObject
from cimrihook.errors import TranscriptError
from cimrihook.simulate import SYNTHETIC_MODEL, context_of
from cimrihook.transcripts import Usage, entry_time, message_usage, parse_line

# Tail of the transcript read to find the last response and cache write; it grows step by step.
TAIL_CHUNKS: Final = (262_144, 1_048_576, 4_194_304)
ONE_HOUR: Final = 3_600.0
FIVE_MINUTES: Final = 300.0


@dataclass(frozen=True, slots=True)
class SessionTail:
    """State of the session by its last API response."""

    last_response_at: float  # epoch seconds
    ttl_seconds: float  # cache lifetime
    context_tokens: int  # context the next request will carry (last request + its response)
    model: str


@dataclass(frozen=True, slots=True)
class TailScan:
    """A scan of one tail part of the transcript, from the end backwards."""

    compacted: bool  # a compaction boundary follows the last response
    last: tuple[JsonObject, Usage] | None  # the latest API response and its usage
    ttl_seconds: float | None  # lifetime of the latest cache write


def read_session_tail(transcript_path: str) -> SessionTail | None:
    """Session state from the end of the transcript.

    None: no transcript, a compaction after the last response, or no response showing the cache
    lifetime in the last 4 MiB. An unreadable file and an inconsistent record are errors.
    """
    try:
        size = os.path.getsize(transcript_path)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise TranscriptError(f"cannot read {transcript_path}: {error}") from error
    for chunk in TAIL_CHUNKS:
        scan = scan_tail(last_lines(transcript_path, size, chunk), transcript_path)
        if scan.compacted:
            return None
        if scan.last is not None and scan.ttl_seconds is not None:
            return session_tail(scan.last, scan.ttl_seconds, transcript_path)
        if chunk >= size:
            return None
    return None


def last_lines(transcript_path: str, size: int, chunk: int) -> list[bytes]:
    """Lines of the file's last `chunk` bytes; the first may be cut off (unparseable)."""
    try:
        with open(transcript_path, "rb") as handle:
            handle.seek(max(0, size - chunk))
            return handle.read().split(b"\n")
    except OSError as error:
        raise TranscriptError(f"cannot read {transcript_path}: {error}") from error


def scan_tail(lines: Sequence[bytes], where: str) -> TailScan:
    """Scans the lines from the end: whether a compaction boundary or a response comes first, which
    response is the latest and what lifetime the latest cache write has."""
    last: tuple[JsonObject, Usage] | None = None
    for raw in reversed(lines):
        entry = parse_line(raw)
        if entry is None:
            continue
        if last is None and entry.get("subtype") == "compact_boundary":
            return TailScan(compacted=True, last=None, ttl_seconds=None)
        usage = response_usage(entry)
        if usage is None:
            continue
        if last is None:
            last = (entry, usage)
        ttl = write_ttl(entry, where)
        if ttl is not None:
            return TailScan(compacted=False, last=last, ttl_seconds=ttl)
    return TailScan(compacted=False, last=last, ttl_seconds=None)


def session_tail(last: tuple[JsonObject, Usage], ttl_seconds: float, where: str) -> SessionTail:
    """Session state from the last response; one without a time or model is inconsistent."""
    entry, usage = last
    try:
        when = entry_time(entry)
    except ValueError as error:
        stamp = entry.get("timestamp")
        raise TranscriptError(f"{where}: unreadable timestamp {stamp!r}") from error
    message = entry.get("message")
    model = message.get("model") if isinstance(message, dict) else None
    if when is None or not isinstance(model, str):
        raise TranscriptError(f"{where}: the last response has no timestamp or model")
    return SessionTail(
        last_response_at=when,
        ttl_seconds=ttl_seconds,
        context_tokens=context_of(usage) + usage.output,
        model=model,
    )


def write_ttl(entry: JsonObject, where: str) -> float | None:
    """Lifetime of the response's cache write; None if there is no write.

    The lifetime is read only from the cache_creation detail. Without the detail, or if it does not
    add up, the lifetime cannot be known; that is an error, so the guard never warns with a wrong
    duration.
    """
    message = entry.get("message")
    usage = message.get("usage") if isinstance(message, dict) else None
    written = usage.get("cache_creation_input_tokens") if isinstance(usage, dict) else None
    if not isinstance(usage, dict) or not isinstance(written, int) or written == 0:
        return None
    split = usage.get("cache_creation")
    hour = split.get("ephemeral_1h_input_tokens") if isinstance(split, dict) else None
    five = split.get("ephemeral_5m_input_tokens") if isinstance(split, dict) else None
    if not isinstance(hour, int) or not isinstance(five, int) or hour + five != written:
        raise TranscriptError(
            f"{where}: a cache write of {written} tokens has the split {split!r}, so the cache "
            "lifetime is unknown"
        )
    return ONE_HOUR if hour > 0 else FIVE_MINUTES


def response_usage(entry: JsonObject) -> Usage | None:
    """Usage of an assistant line that came from the API; None for a local or usage-less line."""
    message = entry.get("message")
    if entry.get("type") != "assistant" or not isinstance(message, dict):
        return None
    if message.get("model") == SYNTHETIC_MODEL:
        return None
    return message_usage(message)
