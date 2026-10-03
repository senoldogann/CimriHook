"""Transcript'in sonundan oturumun son durumu: son yanıtın zamanı, önbellek ömrü ve bağlamı.

Soğuk istem koruması bütün transcript'i okumadan sonundan yararlanır; son yanıt bulunana dek okunan
kısım büyütülür (Claude Code yanıttan sonra büyük ek kayıtlar yazabilir). Önbellek ömrü son önbellek
yazımının türünden (5 dakika / 1 saat) gelir; ömür her istekte yenilendiğinden önbellek son yanıttan
bu kadar süre sonra soğur. Son yanıttan sonra bir sıkıştırma sınırı varsa bağlam artık o yanıttaki
kadar büyük değildir: durum bilinmez sayılır.
"""

import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from cimrihook.audit import Usage, entry_time, message_usage, parse_line
from cimrihook.claude import JsonObject
from cimrihook.errors import TranscriptError
from cimrihook.simulate import SYNTHETIC_MODEL, context_of

# Son yanıtı ve önbellek yazımını bulmak için transcript'in okunan son kısmı, adım adım büyür.
TAIL_CHUNKS: Final = (262_144, 1_048_576, 4_194_304)
ONE_HOUR: Final = 3_600.0
FIVE_MINUTES: Final = 300.0


@dataclass(frozen=True, slots=True)
class SessionTail:
    """Oturumun son API yanıtına göre durumu."""

    last_response_at: float  # epoch saniye
    ttl_seconds: float  # önbellek ömrü
    context_tokens: int  # bir sonraki isteğin taşıyacağı bağlam (son istek + yanıtı)
    model: str


@dataclass(frozen=True, slots=True)
class TailScan:
    """Transcript'in bir son kısmının sondan başa taranması."""

    compacted: bool  # son yanıttan sonra sıkıştırma sınırı var
    last: tuple[JsonObject, Usage] | None  # en son API yanıtı ve kullanımı
    ttl_seconds: float | None  # en son önbellek yazımının ömrü


def read_session_tail(transcript_path: str) -> SessionTail | None:
    """Transcript'in sonundan oturum durumu.

    None: transcript yok, son yanıttan sonra sıkıştırma yapılmış ya da son 4 MiB'ta önbellek ömrünü
    gösteren bir yanıt yok. Okunamayan dosya ve tutarsız kayıt hatadır.
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
    """Dosyanın son `chunk` baytının satırları; ilk satır yarım olabilir (ayrıştırılamaz)."""
    try:
        with open(transcript_path, "rb") as handle:
            handle.seek(max(0, size - chunk))
            return handle.read().split(b"\n")
    except OSError as error:
        raise TranscriptError(f"cannot read {transcript_path}: {error}") from error


def scan_tail(lines: Sequence[bytes], where: str) -> TailScan:
    """Satırları sondan başa tarar: önce sıkıştırma sınırı mı yoksa yanıt mı geliyor, en son
    yanıt hangisi ve en son önbellek yazımının ömrü ne."""
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
    """Son yanıttan oturum durumu; zamanı ya da modeli olmayan yanıt tutarsız kayıttır."""
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
    """Yanıtın önbellek yazımının ömrü; yazım yoksa None.

    Ömür yalnızca cache_creation ayrıntısından okunur. Ayrıntı yoksa ya da toplamı tutmuyorsa ömür
    bilinemez; koruma yanlış süreyle uyarmasın diye bu hatadır.
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
    """API'den gelmiş asistan satırının kullanımı; yerel ya da kullanımsız satırda None."""
    message = entry.get("message")
    if entry.get("type") != "assistant" or not isinstance(message, dict):
        return None
    if message.get("model") == SYNTHETIC_MODEL:
        return None
    return message_usage(message)
