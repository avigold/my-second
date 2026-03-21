"""Featured player management: CRUD for the featured_players PostgreSQL table."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import psycopg2
import psycopg2.extras


class FeaturedPlayerManager:
    """CRUD for the featured_players table. Same pattern as BotManager."""

    def __init__(self, database_url: str) -> None:
        self._db_url = database_url

    @contextmanager
    def _conn(self):
        conn = psycopg2.connect(self._db_url)
        try:
            yield conn
        finally:
            conn.close()

    def create(
        self,
        slug: str,
        display_name: str,
        platform: str,
        username: str,
        title: str | None,
        speeds: str,
        description: str | None,
        photo_url: str | None = None,
    ) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO featured_players
                    (slug, display_name, platform, username, title, speeds, description, photo_url)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (slug) DO NOTHING
                """,
                (slug, display_name, platform, username, title, speeds, description, photo_url),
            )
            conn.commit()

    def set_photo_url(self, slug: str, photo_url: str) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE featured_players SET photo_url = %s WHERE slug = %s",
                (photo_url, slug),
            )
            conn.commit()

    def list_all(self) -> list[dict[str, Any]]:
        with self._conn() as conn, conn.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor
        ) as cur:
            cur.execute(
                "SELECT * FROM featured_players ORDER BY created_at ASC"
            )
            rows = cur.fetchall()
        return [_serialise(r) for r in rows]

    def get(self, slug: str) -> dict[str, Any] | None:
        with self._conn() as conn, conn.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor
        ) as cur:
            cur.execute(
                "SELECT * FROM featured_players WHERE slug = %s",
                (slug,),
            )
            row = cur.fetchone()
        return _serialise(row) if row else None

    def set_ready(
        self,
        slug: str,
        elo: int | None,
        white_book_path: str,
        black_book_path: str,
        bot_model_path: str,
        profile_json_path: str | None = None,
    ) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE featured_players
                   SET status = 'ready', elo = %s,
                       white_book_path = %s, black_book_path = %s,
                       bot_model_path = %s, profile_json_path = %s
                 WHERE slug = %s
                """,
                (elo, white_book_path, black_book_path, bot_model_path, profile_json_path, slug),
            )
            conn.commit()

    def set_description(self, slug: str, description: str, force: bool = False) -> None:
        """Set description. If force=False (default), only sets when not already populated."""
        with self._conn() as conn, conn.cursor() as cur:
            if force:
                cur.execute(
                    "UPDATE featured_players SET description = %s WHERE slug = %s",
                    (description, slug),
                )
            else:
                cur.execute(
                    """UPDATE featured_players SET description = %s
                       WHERE slug = %s AND (description IS NULL OR description = '')""",
                    (description, slug),
                )
            conn.commit()

    def update_meta(
        self,
        slug: str,
        display_name: str,
        title: str | None,
        description: str | None,
        photo_url: str | None,
        photo_position: int | None = None,
    ) -> bool:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE featured_players
                      SET display_name = %s, title = %s,
                          description = %s, photo_url = %s,
                          photo_position = COALESCE(%s, photo_position, 25)
                    WHERE slug = %s""",
                (display_name, title, description, photo_url, photo_position, slug),
            )
            updated = cur.rowcount > 0
            conn.commit()
        return updated

    def update_training_params(
        self,
        slug: str,
        platform: str,
        username: str,
        speeds: str,
    ) -> bool:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE featured_players
                      SET platform = %s, username = %s, speeds = %s
                    WHERE slug = %s""",
                (platform, username, speeds, slug),
            )
            updated = cur.rowcount > 0
            conn.commit()
        return updated

    def set_failed(self, slug: str) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE featured_players SET status = 'failed' WHERE slug = %s",
                (slug,),
            )
            conn.commit()

    def set_status(self, slug: str, status: str) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE featured_players SET status = %s WHERE slug = %s",
                (status, slug),
            )
            conn.commit()

    def delete(self, slug: str) -> bool:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM featured_players WHERE slug = %s", (slug,))
            deleted = cur.rowcount > 0
            conn.commit()
        return deleted


def _serialise(row) -> dict[str, Any]:
    d = dict(row)
    if d.get("created_at") is not None:
        d["created_at"] = d["created_at"].isoformat()
    return d
