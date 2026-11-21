"""Local dictation history, so you can recover text that went to the wrong window."""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from .config import HISTORY_PATH

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS dictations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,
    duration    REAL    NOT NULL,
    engine      TEXT    NOT NULL,
    cleaned     INTEGER NOT NULL DEFAULT 0,
    raw_text    TEXT    NOT NULL,
    final_text  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS dictations_ts ON dictations (ts DESC);
"""


@dataclass
class Entry:
    id: int
    ts: float
    duration: float
    engine: str
    cleaned: bool
    raw_text: str
    final_text: str


class History:
    def __init__(self, path: Path | None = None):
        self.path = path or HISTORY_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def add(
        self,
        raw_text: str,
        final_text: str,
        duration: float,
        engine: str,
        cleaned: bool,
    ) -> None:
        try:
            self._conn.execute(
                "INSERT INTO dictations (ts, duration, engine, cleaned, raw_text, final_text)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (time.time(), duration, engine, int(cleaned), raw_text, final_text),
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            log.warning("could not record history: %s", exc)

    def recent(self, limit: int = 20) -> list[Entry]:
        rows = self._conn.execute(
            "SELECT id, ts, duration, engine, cleaned, raw_text, final_text"
            " FROM dictations ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            Entry(r[0], r[1], r[2], r[3], bool(r[4]), r[5], r[6]) for r in rows
        ]

    def stats(self) -> dict[str, float]:
        row = self._conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(duration), 0), COALESCE(SUM(LENGTH(final_text)), 0)"
            " FROM dictations"
        ).fetchone()
        return {"count": row[0], "seconds": row[1], "characters": row[2]}

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass
