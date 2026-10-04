"""Transcript taraması: gerçek API istekleri, sıkıştırmalar, tekilleştirme ve zaman penceresi.

Claude Code aynı mesaj kimliğini birden çok satıra yazar; zaman ilk satırdan, kullanım son
satırdan alınır. Yerel sentetik mesajlar API'ye gitmediği için sayılmaz. Çatallanmış ya da
sürdürülmüş oturumlar önceki dosyanın geçmişini kopyalar; kopyalar yalnızca ilk görüldükleri dosyada
sayılır. doctor, gain ve önbellek ömrü danışmanı bu taramayı paylaşır.
"""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from cimrihook.audit import Usage, entry_time, message_usage, parse_line
from cimrihook.codec import estimate_tokens
from cimrihook.simulate import (
    SYNTHETIC_MODEL,
    claude_prices,
    compact_metadata_tokens,
    content_text,
)


@dataclass(frozen=True, slots=True)
class Request:
    """Tek gerçek API isteği ve oturum içindeki yeri."""

    message_id: str
    timestamp: float | None  # epoch saniye; satırda zaman yoksa None
    model: str
    usage: Usage
    subagent: bool
    first: bool  # transcript'teki ilk istek
    after_compaction: bool  # sıkıştırmadan sonraki ilk istek
    model_switch: bool  # bir önceki istekten farklı model
    gap_seconds: float | None  # bir önceki istekten bu yana geçen süre


@dataclass(frozen=True, slots=True)
class Compaction:
    """Transcript'teki bir sıkıştırma sınırı."""

    timestamp: float | None
    trigger: int  # sıkıştırmayı tetikleyen bağlam
    automatic: bool  # Claude Code'un kendi tetiklediği (elle /compact değil)
    summary_tokens: int  # özetin tahmini token sayısı (özet metninden); özet yoksa 0


@dataclass(frozen=True, slots=True)
class TranscriptScan:
    """Bir transcript'teki istekler ve sıkıştırmalar."""

    requests: tuple[Request, ...]
    compactions: tuple[Compaction, ...]


def scan_transcript(path: Path, subagent: bool) -> TranscriptScan:
    """Transcript'teki gerçek API istekleri (yerel sentetik mesajlar hariç) ve sıkıştırmalar.

    Aynı mesaj kimliği birden çok satıra yazılır; zaman ilk satırdan, kullanım son satırdan alınır.
    """
    order: list[str] = []
    times: dict[str, float | None] = {}
    models: dict[str, str] = {}
    usages: dict[str, Usage] = {}
    after: set[str] = set()
    compactions: list[Compaction] = []
    awaiting = False
    with path.open("rb") as handle:
        for raw_line in handle:
            entry = parse_line(raw_line)
            if entry is None:
                continue
            if entry.get("type") == "system" and entry.get("subtype") == "compact_boundary":
                trigger = compact_metadata_tokens(entry, "preTokens", str(path))
                metadata = entry.get("compactMetadata")
                kind = metadata.get("trigger") if isinstance(metadata, dict) else None
                compactions.append(Compaction(entry_time(entry), trigger, kind == "auto", 0))
                awaiting = True
                continue
            message = entry.get("message")
            if entry.get("isCompactSummary") is True and compactions and isinstance(message, dict):
                summary = estimate_tokens(content_text(message.get("content")))
                compactions[-1] = replace(compactions[-1], summary_tokens=summary)
                continue
            if entry.get("type") != "assistant" or not isinstance(message, dict):
                continue
            message_id = message.get("id")
            model = message.get("model")
            usage = message_usage(message)
            if (
                not isinstance(message_id, str)
                or not isinstance(model, str)
                or model == SYNTHETIC_MODEL
                or usage is None
            ):
                continue
            if message_id not in usages:
                order.append(message_id)
                times[message_id] = entry_time(entry)
                models[message_id] = model
                if awaiting:
                    after.add(message_id)
                    awaiting = False
            usages[message_id] = usage
    return TranscriptScan(
        requests=tuple(
            Request(
                message_id=message_id,
                timestamp=times[message_id],
                model=models[message_id],
                usage=usages[message_id],
                subagent=subagent,
                first=index == 0,
                after_compaction=message_id in after,
                model_switch=index > 0 and models[order[index - 1]] != models[message_id],
                gap_seconds=gap(times[order[index - 1]], times[message_id]) if index > 0 else None,
            )
            for index, message_id in enumerate(order)
        ),
        compactions=tuple(compactions),
    )


def gap(previous: float | None, current: float | None) -> float | None:
    """İki istek arasındaki süre; zamanlardan biri yoksa None."""
    return None if previous is None or current is None else current - previous


def token_costs(request: Request, base: float) -> tuple[float, float, float, float]:
    """İsteğin USD maliyeti token türüne göre: (önbellek okuma, yazma, önbelleksiz, çıktı)."""
    prices = claude_prices(request.model)
    usage = request.usage
    return (
        usage.read * prices.read * base,
        (usage.write_5m * prices.write_5m + usage.write_1h * prices.write_1h) * base,
        usage.uncached * prices.uncached * base,
        usage.output * prices.output * base,
    )


def unique_scans(scans: Sequence[TranscriptScan]) -> list[TranscriptScan]:
    """Çatallanmış ya da sürdürülmüş oturumların önceki dosyadan kopyaladığı istekler (aynı mesaj
    kimliği) ve sıkıştırmalar (aynı zaman ve boyut) yalnızca ilk görüldükleri dosyada kalır."""
    seen_requests: set[str] = set()
    seen_compactions: set[Compaction] = set()
    unique: list[TranscriptScan] = []
    for scan in scans:
        requests = tuple(r for r in scan.requests if r.message_id not in seen_requests)
        compactions = tuple(c for c in scan.compactions if c not in seen_compactions)
        seen_requests.update(r.message_id for r in scan.requests)
        seen_compactions.update(scan.compactions)
        unique.append(replace(scan, requests=requests, compactions=compactions))
    return unique


def recent_requests(scans: Sequence[TranscriptScan], cutoff: float) -> list[TranscriptScan]:
    """Pencere dışındaki istekler ve sıkıştırmalar atılır: pencerede değişmiş bir transcript eski
    istekler de taşır. Zamanı olmayan kayıt dosyası pencerede değiştiği için pencerede sayılır."""
    return [
        replace(
            scan,
            requests=tuple(
                r for r in scan.requests if r.timestamp is None or r.timestamp >= cutoff
            ),
            compactions=tuple(
                c for c in scan.compactions if c.timestamp is None or c.timestamp >= cutoff
            ),
        )
        for scan in scans
    ]
