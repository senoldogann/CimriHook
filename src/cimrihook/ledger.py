"""SQLite tabanlı bağlam defteri: her bağlamda ajanın hangi bilgiyi hangi biçimde aldığını tutar."""

import sqlite3
import time
import zlib
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Final, Self

from cimrihook.errors import LedgerError
from cimrihook.model import Decision, Encoding, Observation, QuotaSample, SavingsRow, View

SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    generation INTEGER NOT NULL,
    transcript_offset INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS steps (
    context_key TEXT NOT NULL,
    generation INTEGER NOT NULL,
    step INTEGER NOT NULL,
    tool TEXT NOT NULL,
    request_key TEXT NOT NULL,
    mutating INTEGER NOT NULL,
    PRIMARY KEY (context_key, generation, step)
);
CREATE TABLE IF NOT EXISTS views (
    context_key TEXT NOT NULL,
    generation INTEGER NOT NULL,
    step INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    tool TEXT NOT NULL,
    stream TEXT NOT NULL,
    request_key TEXT NOT NULL,
    encoding TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    total_lines INTEGER NOT NULL,
    whole INTEGER NOT NULL,
    line_count INTEGER NOT NULL,
    body BLOB NOT NULL,
    tokens_raw INTEGER NOT NULL,
    tokens_sent INTEGER NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (context_key, generation, step)
);
CREATE INDEX IF NOT EXISTS views_by_stream ON views (context_key, generation, stream, step);
CREATE INDEX IF NOT EXISTS views_by_request ON views (context_key, generation, request_key, step);
CREATE INDEX IF NOT EXISTS views_by_session ON views (session_id);
CREATE TABLE IF NOT EXISTS quota_samples (
    limit_window TEXT NOT NULL,
    resets_at INTEGER NOT NULL,
    used_percentage REAL NOT NULL,
    taken_at REAL NOT NULL,
    session_id TEXT NOT NULL,
    model TEXT NOT NULL,
    PRIMARY KEY (limit_window, resets_at, used_percentage)
);
CREATE TABLE IF NOT EXISTS guard_blocks (
    session_id TEXT NOT NULL,
    last_response_at REAL NOT NULL,
    blocked_at REAL NOT NULL,
    PRIMARY KEY (session_id, last_response_at)
);
"""
VIEW_COLUMNS: Final = (
    "step, stream, request_key, encoding, start_line, total_lines, whole, line_count, body"
)
SAVINGS_COLUMNS: Final = "encoding, tool, COUNT(*), SUM(tokens_raw), SUM(tokens_sent)"
# Kilit bekleme süresi; hook zaman aşımının (10 s) altında kalmalı ki hook bekleyişte öldürülmesin.
BUSY_TIMEOUT_SECONDS: Final = 5.0
PRIVATE_DIR_MODE: Final = 0o700  # defter araç çıktılarını tutabilir; yalnızca kullanıcı okur

type ViewRow = tuple[int, str, str, str, int, int, int, int, bytes]
type SavingsTuple = tuple[str, str, int, int, int]


@dataclass(frozen=True, slots=True)
class SessionState:
    """Oturumun bağlam kuşağı ve transcript tarama konumu."""

    generation: int  # sıkıştırma ya da oturum (yeniden) başlangıcıyla artar
    transcript_offset: int  # transcript'in en son taranan bayt konumu


class Ledger:
    """Defter veritabanına bağlayıcı; her kullanım tek bir yazma işlemidir (BEGIN IMMEDIATE)."""

    def __init__(self, path: Path, busy_timeout_seconds: float) -> None:
        path.parent.mkdir(mode=PRIVATE_DIR_MODE, parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, timeout=busy_timeout_seconds, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)

    def __enter__(self) -> Self:
        self._db.execute("BEGIN IMMEDIATE")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._db.execute("COMMIT" if exc_type is None else "ROLLBACK")
        self._db.close()

    def session_state(self, session_id: str, transcript_size: int) -> SessionState:
        """Oturum durumunu döndürür; ilk görülen oturum transcript'in sonundan izlenmeye başlar."""
        row = self._db.execute(
            "SELECT generation, transcript_offset FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is not None:
            return SessionState(generation=int(row[0]), transcript_offset=int(row[1]))
        self._db.execute("INSERT INTO sessions VALUES (?, 0, ?)", (session_id, transcript_size))
        return SessionState(generation=0, transcript_offset=transcript_size)

    def reset_session(self, session_id: str, transcript_size: int) -> None:
        """Yeni bağlam kuşağı başlatır; önceki kuşağın bilgisi artık kullanılmaz."""
        self._db.execute(
            "INSERT INTO sessions VALUES (?, 1, ?) ON CONFLICT (session_id) DO UPDATE SET "
            "generation = generation + 1, transcript_offset = excluded.transcript_offset",
            (session_id, transcript_size),
        )

    def bump_generation(self, session_id: str) -> None:
        """Transcript'te sıkıştırma sınırı görüldü: kuşağı ilerletir."""
        self._db.execute(
            "UPDATE sessions SET generation = generation + 1 WHERE session_id = ?", (session_id,)
        )

    def set_transcript_offset(self, session_id: str, offset: int) -> None:
        """Transcript'in taranan son konumunu kaydeder."""
        self._db.execute(
            "UPDATE sessions SET transcript_offset = ? WHERE session_id = ?", (offset, session_id)
        )

    def next_step(self, context_key: str, generation: int) -> int:
        """Bağlam içindeki bir sonraki araç sonucu sırası."""
        row = self._db.execute(
            "SELECT COALESCE(MAX(step), 0) + 1 FROM steps WHERE context_key = ? AND generation = ?",
            (context_key, generation),
        ).fetchone()
        return int(row[0])

    def record_step(
        self,
        context_key: str,
        generation: int,
        step: int,
        tool: str,
        request_key: str,
        mutating: bool,
    ) -> None:
        """Her araç sonucunu (yeniden kodlanmasa da) sırasıyla kaydeder."""
        self._db.execute(
            "INSERT INTO steps VALUES (?, ?, ?, ?, ?, ?)",
            (context_key, generation, step, tool, request_key, int(mutating)),
        )

    def stream_views(self, context_key: str, generation: int, stream: str) -> tuple[View, ...]:
        """Aynı akışın bu kuşaktaki tüm görünümleri, eskiden yeniye."""
        rows = self._db.execute(
            f"SELECT {VIEW_COLUMNS} FROM views "
            "WHERE context_key = ? AND generation = ? AND stream = ? ORDER BY step",
            (context_key, generation, stream),
        ).fetchall()
        return tuple(view_from_row(row) for row in rows)

    def latest_request_view(
        self, context_key: str, generation: int, request_key: str
    ) -> View | None:
        """Birebir aynı isteğin bu kuşaktaki en son görünümü."""
        row = self._db.execute(
            f"SELECT {VIEW_COLUMNS} FROM views "
            "WHERE context_key = ? AND generation = ? AND request_key = ? "
            "ORDER BY step DESC LIMIT 1",
            (context_key, generation, request_key),
        ).fetchone()
        return None if row is None else view_from_row(row)

    def mutated_since(self, context_key: str, generation: int, step: int, request_key: str) -> bool:
        """Verilen adımdan sonra (bu istek dışında) sonucu değiştirebilecek bir çağrı oldu mu?"""
        row = self._db.execute(
            "SELECT 1 FROM steps WHERE context_key = ? AND generation = ? AND step > ? "
            "AND mutating = 1 AND request_key != ? LIMIT 1",
            (context_key, generation, step, request_key),
        ).fetchone()
        return row is not None

    def record_view(
        self,
        session_id: str,
        context_key: str,
        generation: int,
        step: int,
        obs: Observation,
        decision: Decision,
    ) -> None:
        """Ajanın bu adımda edindiği bilgiyi ve token muhasebesini kaydeder."""
        known = () if decision.encoding is Encoding.OUTLINE else obs.lines
        self._db.execute(
            "INSERT INTO views VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                context_key,
                generation,
                step,
                session_id,
                obs.tool.value,
                obs.stream,
                obs.request_key,
                decision.encoding.value,
                obs.start_line,
                obs.total_lines,
                int(obs.whole),
                len(known),
                encode_lines(known),
                decision.tokens_raw,
                decision.tokens_sent,
                time.time(),
            ),
        )

    def savings_all(self) -> tuple[SavingsRow, ...]:
        """Tüm oturumlardaki kararların token muhasebesi."""
        rows = self._db.execute(
            f"SELECT {SAVINGS_COLUMNS} FROM views GROUP BY encoding, tool ORDER BY encoding, tool"
        ).fetchall()
        return tuple(savings_from_row(row) for row in rows)

    def savings_for_session(self, session_id: str) -> tuple[SavingsRow, ...]:
        """Tek oturumdaki kararların token muhasebesi."""
        rows = self._db.execute(
            f"SELECT {SAVINGS_COLUMNS} FROM views WHERE session_id = ? "
            "GROUP BY encoding, tool ORDER BY encoding, tool",
            (session_id,),
        ).fetchall()
        return tuple(savings_from_row(row) for row in rows)

    def claim_guard_block(self, session_id: str, last_response_at: float, now: float) -> bool:
        """Bu boşluk dönemindeki ilk uyarıysa kaydeder ve True döner; uyarılmışsa False."""
        cursor = self._db.execute(
            "INSERT OR IGNORE INTO guard_blocks VALUES (?, ?, ?)",
            (session_id, last_response_at, now),
        )
        return cursor.rowcount == 1

    def record_quota(self, samples: Sequence[QuotaSample]) -> None:
        """Kullanım limiti gözlemlerini kaydeder; aynı penceredeki aynı yüzde bir kez tutulur."""
        self._db.executemany(
            "INSERT OR IGNORE INTO quota_samples VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    sample.window,
                    sample.resets_at,
                    sample.used_percentage,
                    sample.taken_at,
                    sample.session_id,
                    sample.model,
                )
                for sample in samples
            ],
        )


def encode_lines(lines: Sequence[str]) -> bytes:
    """Satırları sıkıştırılmış UTF-8 olarak saklar (yalnız vekil karakterler de korunur)."""
    return zlib.compress("\n".join(lines).encode("utf-8", "surrogatepass"))


def decode_lines(body: bytes, line_count: int) -> tuple[str, ...]:
    """encode_lines'ın tersi; boş liste ile tek boş satırı line_count ayırt eder."""
    if line_count == 0:
        return ()
    return tuple(zlib.decompress(body).decode("utf-8", "surrogatepass").split("\n"))


def view_from_row(row: ViewRow) -> View:
    """Veritabanı satırını görünüme çevirir."""
    step, stream, request_key, encoding, start_line, total_lines, whole, line_count, body = row
    return View(
        step=step,
        stream=stream,
        request_key=request_key,
        encoding=Encoding(encoding),
        start_line=start_line,
        lines=decode_lines(body, line_count),
        total_lines=total_lines,
        whole=bool(whole),
    )


def savings_from_row(row: SavingsTuple) -> SavingsRow:
    """Toplama sorgusunun satırını tasarruf kaydına çevirir."""
    encoding, tool, results, tokens_raw, tokens_sent = row
    return SavingsRow(Encoding(encoding), tool, results, tokens_raw, tokens_sent)


def claim_guard(
    path: Path, session_id: str, last_response_at: float, now: float, busy_timeout: float
) -> bool:
    """Soğuk istem uyarısını bu boşluk dönemi için kaydeder; ilk uyarıysa True."""
    try:
        with Ledger(path, busy_timeout) as ledger:
            return ledger.claim_guard_block(session_id, last_response_at, now)
    except (sqlite3.Error, OSError) as error:
        raise LedgerError(f"cannot record the guard decision in {path}: {error}") from error


def record_new_quota(path: Path, samples: Sequence[QuotaSample], busy_timeout: float) -> None:
    """Defterde henüz olmayan kullanım limiti gözlemlerini yazar.

    Durum satırı her güncellemede çağırır; gözlemlerin çoğu zaten kayıtlıdır. Önce kilitsiz bir
    okumayla yenileri ayıklanır, yazma kilidi yalnızca yeni gözlem varsa alınır.
    """
    try:
        new = unrecorded_quota(path, samples, busy_timeout)
        if new:
            with Ledger(path, busy_timeout) as ledger:
                ledger.record_quota(new)
    except (sqlite3.Error, OSError) as error:
        raise LedgerError(f"cannot record usage-limit samples in {path}: {error}") from error


def unrecorded_quota(
    path: Path, samples: Sequence[QuotaSample], busy_timeout: float
) -> tuple[QuotaSample, ...]:
    """Defterde olmayan gözlemler; defter ya da tablo henüz yoksa hepsi (salt okunur bağlantı)."""
    if not path.exists():
        return tuple(samples)
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=busy_timeout)) as db:
        table = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'quota_samples'"
        ).fetchone()
        if table is None:
            return tuple(samples)
        return tuple(
            sample
            for sample in samples
            if db.execute(
                "SELECT 1 FROM quota_samples WHERE limit_window = ? AND resets_at = ? "
                "AND used_percentage = ?",
                (sample.window, sample.resets_at, sample.used_percentage),
            ).fetchone()
            is None
        )
