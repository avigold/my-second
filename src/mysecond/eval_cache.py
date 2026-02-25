"""Persistent cache for Stockfish position evaluations.

Keyed on FEN string. Stores the top N best moves with their centipawn scores
(from White's perspective). On a cache hit the engine is skipped entirely.

Thread-safe: each thread gets its own SQLite connection (via threading.local);
writes are serialised with a threading.Lock so concurrent workers don't
corrupt the on-disk WAL.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path


_DDL = """
CREATE TABLE IF NOT EXISTS eval_cache (
    fen        TEXT    PRIMARY KEY,
    depth      INTEGER NOT NULL,
    moves_json TEXT    NOT NULL,
    ts         REAL    NOT NULL
);
"""

# Maximum number of lines stored per position.  Covers the worst-case
# qualifying-move count in habits analysis (capped at 20 in habits.py).
MAX_MULTIPV = 20


class EvalCache:
    """Read/write cache for Stockfish MultiPV evaluations.

    Storage format per row
    ----------------------
    moves_json : JSON array of ``{"uci": str, "white_cp": int}`` objects,
                 sorted best-first, always from White's perspective.

    A cached result at depth D satisfies any request at depth <= D and
    multipv <= len(stored moves).
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        self._local = threading.local()
        self._init_db()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, fen: str, depth: int, multipv: int) -> list[dict] | None:
        """Return cached moves or None on a miss.

        Returns the top *multipv* ``{"uci", "white_cp"}`` dicts if the
        stored result was computed at *depth* >= requested.
        """
        try:
            row = self._conn().execute(
                "SELECT depth, moves_json FROM eval_cache WHERE fen = ?",
                (fen,),
            ).fetchone()
        except sqlite3.Error:
            return None

        if row is None:
            return None

        cached_depth, moves_json = row
        if cached_depth < depth:
            return None          # shallower than requested — must recompute

        try:
            moves = json.loads(moves_json)
        except (json.JSONDecodeError, TypeError):
            return None

        if len(moves) < multipv:
            return None          # not enough lines cached

        return moves[:multipv]

    def put(self, fen: str, depth: int, moves: list[dict]) -> None:
        """Store a list of ``{"uci", "white_cp"}`` dicts.

        Only overwrites an existing entry if *depth* >= the stored depth,
        ensuring we never replace a deeper result with a shallower one.
        """
        if not moves:
            return
        moves_json = json.dumps(moves[:MAX_MULTIPV])
        with self._write_lock:
            try:
                self._conn().execute(
                    """
                    INSERT INTO eval_cache (fen, depth, moves_json, ts)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(fen) DO UPDATE SET
                        depth      = CASE WHEN excluded.depth >= eval_cache.depth
                                          THEN excluded.depth      ELSE eval_cache.depth      END,
                        moves_json = CASE WHEN excluded.depth >= eval_cache.depth
                                          THEN excluded.moves_json ELSE eval_cache.moves_json END,
                        ts         = excluded.ts
                    """,
                    (fen, depth, moves_json, time.time()),
                )
                self._conn().commit()
            except sqlite3.Error:
                pass  # non-fatal: the position will just be re-evaluated next time

    def stats(self) -> dict:
        """Return basic cache statistics."""
        try:
            row = self._conn().execute(
                "SELECT COUNT(*), MAX(depth), MIN(ts) FROM eval_cache"
            ).fetchone()
            count, max_depth, min_ts = row or (0, None, None)
            return {"positions": count, "max_depth": max_depth}
        except sqlite3.Error:
            return {"positions": 0, "max_depth": None}

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        """Return (or create) a per-thread SQLite connection."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                str(self._db_path),
                timeout=10,
                check_same_thread=False,
            )
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    def _init_db(self) -> None:
        conn = self._conn()
        conn.execute(_DDL)
        conn.commit()
