"""Bot management: CRUD operations for the bots PostgreSQL table."""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from typing import Any

import psycopg2
import psycopg2.extras


class BotManager:
    """Thread-safe CRUD for the bots PostgreSQL table.

    Each method opens and closes its own connection — no persistent state,
    same pattern as JobRegistry.
    """

    def __init__(self, database_url: str) -> None:
        self._db_url = database_url

    @contextmanager
    def _conn(self):
        conn = psycopg2.connect(self._db_url)
        try:
            yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def create(
        self,
        user_id: str,
        opponent_username: str,
        platform: str,
        speeds: str,
        color: str,
        job_id: str,
    ) -> str:
        """Insert a new bot row and return its UUID string."""
        bot_id = str(uuid.uuid4())
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bots
                    (id, user_id, opponent_username, opponent_platform,
                     speeds, job_id, status)
                VALUES (%s, %s, %s, %s, %s, %s, 'training')
                """,
                (bot_id, user_id, opponent_username, platform, speeds, job_id),
            )
            conn.commit()
        return bot_id

    def set_status(
        self,
        bot_id: str,
        status: str,
        opponent_elo: int | None = None,
    ) -> None:
        """Update a bot's status and optionally its Elo rating."""
        with self._conn() as conn, conn.cursor() as cur:
            if opponent_elo is not None:
                cur.execute(
                    "UPDATE bots SET status = %s, opponent_elo = %s WHERE id = %s",
                    (status, opponent_elo, bot_id),
                )
            else:
                cur.execute(
                    "UPDATE bots SET status = %s WHERE id = %s",
                    (status, bot_id),
                )
            conn.commit()

    def delete(self, bot_id: str, user_id: str) -> bool:
        """Delete a bot row. Returns True if a row was deleted."""
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM bots WHERE id = %s AND user_id = %s",
                (bot_id, user_id),
            )
            deleted = cur.rowcount > 0
            conn.commit()
        return deleted

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def list_for_user(self, user_id: str) -> list[dict[str, Any]]:
        """Return all bots owned by user_id, newest first."""
        with self._conn() as conn, conn.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor
        ) as cur:
            cur.execute(
                """
                SELECT id, user_id, opponent_username, opponent_platform,
                       speeds, opponent_elo, job_id, status, created_at
                FROM bots
                WHERE user_id = %s
                ORDER BY created_at DESC
                """,
                (user_id,),
            )
            rows = cur.fetchall()
        return [_serialise(r) for r in rows]

    def get(self, bot_id: str) -> dict[str, Any] | None:
        """Return a single bot row by id, or None if not found."""
        with self._conn() as conn, conn.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor
        ) as cur:
            cur.execute(
                """
                SELECT id, user_id, opponent_username, opponent_platform,
                       speeds, opponent_elo, job_id, status, created_at
                FROM bots
                WHERE id = %s
                """,
                (bot_id,),
            )
            row = cur.fetchone()
        return _serialise(row) if row else None


def _serialise(row) -> dict[str, Any]:
    """Convert a psycopg2 RealDictRow to a plain dict, converting datetimes."""
    d = dict(row)
    if d.get("created_at") is not None:
        d["created_at"] = d["created_at"].isoformat()
    return d
