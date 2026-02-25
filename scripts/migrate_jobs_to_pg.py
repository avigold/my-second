#!/usr/bin/env python3
"""Migrate jobs from data/jobs.sqlite → PostgreSQL.

Usage:
    python scripts/migrate_jobs_to_pg.py

Reads DATABASE_URL from the environment (or .env).
Safe to run multiple times — uses ON CONFLICT DO NOTHING.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

# Load .env if present.
_env = Path(__file__).resolve().parent.parent / ".env"
if _env.exists():
    for line in _env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            if k.strip() not in os.environ:
                os.environ[k.strip()] = v.strip()

import psycopg2
import psycopg2.extras

SQLITE_PATH = Path(__file__).resolve().parent.parent / "data" / "jobs.sqlite"
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://mysecond:mysecond@localhost:5433/mysecond",
)


def main() -> None:
    if not SQLITE_PATH.exists():
        print(f"No SQLite DB found at {SQLITE_PATH} — nothing to migrate.")
        return

    print(f"Reading from {SQLITE_PATH} …")
    with sqlite3.connect(SQLITE_PATH) as src:
        rows = src.execute(
            """
            SELECT id, command, params_json, status, started_at,
                   finished_at, out_path, exit_code, log_text
            FROM jobs
            ORDER BY started_at
            """
        ).fetchall()

    print(f"Found {len(rows)} jobs in SQLite.")

    # Import DDL from jobs.py (single source of truth).
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "web"))
    from jobs import _DDL

    conn = psycopg2.connect(DATABASE_URL)
    inserted = skipped = 0
    try:
        with conn, conn.cursor() as cur:
            cur.execute(_DDL)
            conn.commit()
        with conn, conn.cursor() as cur:
            for row in rows:
                job_id, command, params_json, status, started_at, \
                    finished_at, out_path, exit_code, log_text = row

                # Jobs that were mid-flight when the old server died → cancelled.
                if status == "running":
                    status = "cancelled"

                try:
                    params = json.loads(params_json)
                except (json.JSONDecodeError, TypeError):
                    params = {}

                cur.execute(
                    """
                    INSERT INTO jobs (
                        id, command, params, status,
                        started_at, finished_at, out_path, exit_code, log_text
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    (
                        job_id,
                        command,
                        psycopg2.extras.Json(params),
                        status,
                        started_at,
                        finished_at,
                        out_path,
                        exit_code,
                        log_text,
                    ),
                )
                if cur.rowcount:
                    inserted += 1
                else:
                    skipped += 1

        print(f"Done — inserted {inserted}, skipped {skipped} (already present).")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
