"""SQLite-backed cache for opening-explorer API responses."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any


class Cache:
    """Persistent key-value store keyed by (fen, backend).

    Thread-safe for concurrent reads/writes from multiple threads
    (SQLite WAL mode + check_same_thread=False).
    """

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(db_path),
            check_same_thread=False,
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._create_table()

    def _create_table(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS explorer_cache (
                fen     TEXT NOT NULL,
                backend TEXT NOT NULL,
                payload TEXT NOT NULL,
                ts      REAL NOT NULL,
                PRIMARY KEY (fen, backend)
            )
            """
        )
        self._conn.commit()

    def get(self, fen: str, backend: str) -> dict[str, Any] | None:
        """Return cached payload or *None* on a cache miss."""
        row = self._conn.execute(
            "SELECT payload FROM explorer_cache WHERE fen = ? AND backend = ?",
            (fen, backend),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def set(self, fen: str, backend: str, data: dict[str, Any]) -> None:
        """Insert or replace a cache entry."""
        self._conn.execute(
            """
            INSERT OR REPLACE INTO explorer_cache (fen, backend, payload, ts)
            VALUES (?, ?, ?, ?)
            """,
            (fen, backend, json.dumps(data), time.time()),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Cache":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
