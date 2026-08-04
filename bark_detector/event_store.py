"""SQLite-backed history of detection events, for the event log UI.

Uses only the standard library (sqlite3) to keep the dependency list small.
A single lock serializes writes since multiple SourceWorker threads share
one store.
"""

from __future__ import annotations

import os
import sqlite3
import threading

from .snippet import DetectionEvent

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    label TEXT NOT NULL,
    score REAL NOT NULL,
    timestamp REAL NOT NULL,
    duration REAL NOT NULL,
    file TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events (timestamp DESC);
"""


class EventStore:
    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def add(self, event: DetectionEvent) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO events (source, label, score, timestamp, duration, file) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    event.source,
                    event.label,
                    event.score,
                    event.timestamp,
                    event.duration,
                    event.file,
                ),
            )
            self._conn.commit()
            assert cur.lastrowid is not None
            return cur.lastrowid

    def list_recent(self, limit: int = 50, before: float | None = None) -> list[dict]:
        query = "SELECT * FROM events"
        params: list = []
        if before is not None:
            query += " WHERE timestamp < ?"
            params.append(before)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS c FROM events").fetchone()
        return row["c"]

    def list_all_ordered_by_age_asc(self) -> list[dict]:
        """Oldest first - used by the cleanup pass to evict age/space victims."""
        with self._lock:
            rows = self._conn.execute("SELECT * FROM events ORDER BY timestamp ASC").fetchall()
        return [dict(row) for row in rows]

    def delete_ids(self, ids: list[int]) -> None:
        if not ids:
            return
        with self._lock:
            placeholders = ",".join("?" for _ in ids)
            self._conn.execute(f"DELETE FROM events WHERE id IN ({placeholders})", ids)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
