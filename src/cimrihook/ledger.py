"""SQLite ledger: the guard's cold-prompt warnings and the status line's usage-limit samples."""

import sqlite3
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path
from types import TracebackType
from typing import Final, Self

from cimrihook.errors import LedgerError
from cimrihook.model import QuotaSample

LEDGER_FILE: Final = "ledger.sqlite3"
SCHEMA: Final = """
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
PRIVATE_DIR_MODE: Final = 0o700  # the ledger holds session ids and usage; only the user reads it


def ledger_path(home: Path) -> Path:
    """Path of the ledger database."""
    return home / LEDGER_FILE


class Ledger:
    """Connector to the ledger database; each use is one write transaction (BEGIN IMMEDIATE)."""

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

    def claim_guard_block(self, session_id: str, last_response_at: float, now: float) -> bool:
        """Records the first warning of this idle period and returns True; False if warned."""
        cursor = self._db.execute(
            "INSERT OR IGNORE INTO guard_blocks VALUES (?, ?, ?)",
            (session_id, last_response_at, now),
        )
        return cursor.rowcount == 1

    def record_quota(self, samples: Sequence[QuotaSample]) -> None:
        """Records usage-limit samples; the same percentage in the same window is kept once."""
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


def claim_guard(
    path: Path, session_id: str, last_response_at: float, now: float, busy_timeout: float
) -> bool:
    """Records the cold-prompt warning for this idle period; True if it is the first one."""
    try:
        with Ledger(path, busy_timeout) as ledger:
            return ledger.claim_guard_block(session_id, last_response_at, now)
    except (sqlite3.Error, OSError) as error:
        raise LedgerError(f"cannot record the guard decision in {path}: {error}") from error


def record_new_quota(path: Path, samples: Sequence[QuotaSample], busy_timeout: float) -> None:
    """Writes the usage-limit samples the ledger does not have yet.

    The status line calls this on every update and most samples are already recorded. A read-only
    lookup filters out the new ones first; the write lock is taken only if there is a new sample.
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
    """Samples the ledger lacks: all of them if the ledger or the table does not exist yet."""
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
