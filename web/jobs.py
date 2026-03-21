"""Job registry: in-memory store + PostgreSQL persistence for web UI jobs."""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.extras
import psycopg2.pool


_DDL = """
CREATE TABLE IF NOT EXISTS users (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    lichess_id  TEXT UNIQUE,
    chesscom_id TEXT UNIQUE,
    username    TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Idempotent: add columns that may not exist yet.
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='users' AND column_name='role'
  ) THEN
    ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='users' AND column_name='google_id'
  ) THEN
    ALTER TABLE users ADD COLUMN google_id TEXT UNIQUE;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='users' AND column_name='email'
  ) THEN
    ALTER TABLE users ADD COLUMN email TEXT;
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS jobs (
    id          UUID PRIMARY KEY,
    user_id     UUID REFERENCES users(id) ON DELETE SET NULL,
    command     TEXT NOT NULL,
    params      JSONB NOT NULL,
    status      TEXT NOT NULL,
    started_at  TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    out_path    TEXT,
    exit_code   INTEGER,
    log_text    TEXT
);

CREATE INDEX IF NOT EXISTS jobs_started_at_idx ON jobs (started_at DESC);
CREATE INDEX IF NOT EXISTS jobs_user_id_idx    ON jobs (user_id);

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='jobs' AND column_name='pid'
  ) THEN
    ALTER TABLE jobs ADD COLUMN pid INTEGER;
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS subscriptions (
    user_id                UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    stripe_customer_id     TEXT UNIQUE,
    stripe_subscription_id TEXT UNIQUE,
    plan                   TEXT NOT NULL DEFAULT 'free',
    status                 TEXT NOT NULL DEFAULT 'active',
    current_period_end     TIMESTAMPTZ,
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS bots (
    id                 TEXT PRIMARY KEY,
    user_id            UUID REFERENCES users(id) ON DELETE CASCADE,
    opponent_username  TEXT NOT NULL,
    opponent_platform  TEXT NOT NULL DEFAULT 'lichess',
    speeds             TEXT NOT NULL DEFAULT 'blitz,rapid,classical',
    opponent_elo       INTEGER,
    job_id             TEXT,
    status             TEXT NOT NULL DEFAULT 'training',
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS featured_players (
    slug             TEXT PRIMARY KEY,
    display_name     TEXT NOT NULL,
    platform         TEXT NOT NULL,
    username         TEXT NOT NULL,
    title            TEXT,
    description      TEXT,
    speeds           TEXT NOT NULL DEFAULT 'blitz,rapid',
    elo              INTEGER,
    white_book_path  TEXT,
    black_book_path  TEXT,
    bot_model_path   TEXT,
    profile_json_path TEXT,
    status           TEXT NOT NULL DEFAULT 'pending',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='featured_players' AND column_name='profile_json_path'
  ) THEN
    ALTER TABLE featured_players ADD COLUMN profile_json_path TEXT;
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='featured_players' AND column_name='photo_url'
  ) THEN
    ALTER TABLE featured_players ADD COLUMN photo_url TEXT;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='featured_players' AND column_name='photo_position'
  ) THEN
    ALTER TABLE featured_players ADD COLUMN photo_position INTEGER DEFAULT 25;
  END IF;
END $$;
"""


@dataclass
class Job:
    id: str
    command: str          # "fetch" | "search" | "habits" | "repertoire" | "strategise" | "import"
    params: dict          # form inputs that produced this job
    status: str           # "queued" | "running" | "done" | "failed" | "cancelled"
    started_at: datetime
    finished_at: datetime | None = None
    out_path: str | None = None
    exit_code: int | None = None
    user_id: str | None = None
    pid: int | None = None  # subprocess PID — used to reconnect after restart
    # Not persisted — live only while the process runs:
    log_lines: list[str] = field(default_factory=list, repr=False)
    queue: queue.Queue    = field(default_factory=queue.Queue, repr=False)
    process: Any          = field(default=None, repr=False)
    # Callback set by the job queue to actually launch the subprocess:
    _launch_fn: Any       = field(default=None, repr=False)
    # Callback called on completion (status, exit_code) — not persisted,
    # must be re-registered after restart for orphaned jobs:
    _completion_fn: Any   = field(default=None, repr=False)

    def to_dict(self) -> dict:
        return {
            "id":          self.id,
            "command":     self.command,
            "params":      self.params,
            "status":      self.status,
            "started_at":  self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "out_path":    self.out_path,
            "exit_code":   self.exit_code,
            "user_id":     self.user_id,
        }


class JobRegistry:
    """Thread-safe in-memory store with PostgreSQL persistence."""

    def __init__(self, database_url: str) -> None:
        self._pool = psycopg2.pool.ThreadedConnectionPool(2, 20, database_url)
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._init_db()
        self._load_existing()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create(
        self,
        command: str,
        params: dict,
        out_path: str | None = None,
        user_id: str | None = None,
    ) -> Job:
        job = Job(
            id=str(uuid.uuid4()),
            command=command,
            params=params,
            status="running",
            started_at=datetime.now(tz=timezone.utc),
            out_path=out_path,
            user_id=user_id,
        )
        with self._lock:
            self._jobs[job.id] = job
        self._persist(job)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is not None:
            return job
        # Fall back to DB — handles requests routed to a different gunicorn worker.
        return self._fetch_from_db(job_id)

    def list_all(self) -> list[dict]:
        with self._lock:
            jobs = list(self._jobs.values())
        return [j.to_dict() for j in sorted(jobs, key=lambda j: j.started_at, reverse=True)]

    def upsert_user(
        self,
        *,
        username: str,
        lichess_id: str | None = None,
        chesscom_id: str | None = None,
        google_id: str | None = None,
        email: str | None = None,
    ) -> dict:
        """Create or update a user by platform ID. Returns user dict."""
        with self._conn() as conn, conn.cursor() as cur:
            if lichess_id:
                cur.execute(
                    """
                    INSERT INTO users (lichess_id, username, email)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (lichess_id) DO UPDATE SET username = EXCLUDED.username,
                        email = COALESCE(EXCLUDED.email, users.email)
                    RETURNING id, username, role
                    """,
                    (lichess_id, username, email),
                )
                platform = "lichess"
            elif chesscom_id:
                cur.execute(
                    """
                    INSERT INTO users (chesscom_id, username, email)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (chesscom_id) DO UPDATE SET username = EXCLUDED.username,
                        email = COALESCE(EXCLUDED.email, users.email)
                    RETURNING id, username, role
                    """,
                    (chesscom_id, username, email),
                )
                platform = "chesscom"
            else:
                cur.execute(
                    """
                    INSERT INTO users (google_id, username, email)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (google_id) DO UPDATE SET username = EXCLUDED.username,
                        email = COALESCE(EXCLUDED.email, users.email)
                    RETURNING id, username, role
                    """,
                    (google_id, username, email),
                )
                platform = "google"
            row = cur.fetchone()
            conn.commit()
        return {
            "id":       str(row[0]),
            "username": row[1],
            "role":     row[2],
            "platform": platform,
        }

    def list_for_user(self, user_id: str) -> list[dict]:
        with self._lock:
            jobs = [j for j in self._jobs.values() if j.user_id == user_id]
        return [j.to_dict() for j in sorted(jobs, key=lambda j: j.started_at, reverse=True)]

    def has_running_job(self, user_id: str) -> bool:
        """Return True if the user already has a running or queued job."""
        with self._lock:
            return any(
                j.user_id == user_id and j.status in ("running", "queued")
                for j in self._jobs.values()
            )

    # ------------------------------------------------------------------
    # Admin methods
    # ------------------------------------------------------------------

    def admin_stats(self) -> dict:
        """Return aggregate stats for the admin dashboard."""
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM users")
            total_users = cur.fetchone()[0]
            cur.execute(
                "SELECT COUNT(*) FROM subscriptions WHERE plan = 'pro' AND status IN ('active', 'trialing')"
            )
            pro_subscribers = cur.fetchone()[0]
            cur.execute(
                "SELECT COUNT(*) FROM jobs WHERE started_at >= date_trunc('day', NOW())"
            )
            jobs_today = cur.fetchone()[0]
        with self._lock:
            running = sum(1 for j in self._jobs.values() if j.status in ("running", "queued"))
        return {
            "total_users":     total_users,
            "pro_subscribers": pro_subscribers,
            "jobs_today":      jobs_today,
            "running_jobs":    running,
        }

    def list_users_with_stats(self) -> list[dict]:
        """Return all users with job counts and subscription info."""
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    u.id, u.username, u.role, u.created_at,
                    CASE WHEN u.lichess_id  IS NOT NULL THEN 'lichess'
                         WHEN u.chesscom_id IS NOT NULL THEN 'chesscom'
                         WHEN u.google_id   IS NOT NULL THEN 'google'
                         ELSE 'unknown' END AS platform,
                    COALESCE(s.plan, 'free')   AS plan,
                    COALESCE(s.status, '')      AS sub_status,
                    COUNT(j.id)                 AS total_jobs,
                    MAX(j.started_at)           AS last_active,
                    u.email
                FROM users u
                LEFT JOIN subscriptions s ON s.user_id = u.id
                LEFT JOIN jobs j ON j.user_id = u.id
                GROUP BY u.id, u.username, u.role, u.created_at,
                         u.lichess_id, u.chesscom_id, u.google_id,
                         s.plan, s.status, u.email
                ORDER BY last_active DESC NULLS LAST
                """
            )
            rows = cur.fetchall()
        return [
            {
                "id":          str(r[0]),
                "username":    r[1],
                "role":        r[2],
                "created_at":  r[3].isoformat() if r[3] else None,
                "platform":    r[4],
                "plan":        r[5],
                "sub_status":  r[6],
                "total_jobs":  r[7],
                "last_active": r[8].isoformat() if r[8] else None,
                "email":       r[9],
            }
            for r in rows
        ]

    def set_user_role(self, user_id: str, role: str) -> bool:
        """Set the role for a user. Returns True if the user was found."""
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("UPDATE users SET role = %s WHERE id = %s", (role, user_id))
            updated = cur.rowcount
            conn.commit()
        return updated > 0

    def set_pid(self, job_id: str, pid: int) -> None:
        """Store the subprocess PID immediately after launch."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.pid = pid
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("UPDATE jobs SET pid = %s WHERE id = %s", (pid, job_id))
            conn.commit()

    def set_out_path(self, job_id: str, out_path: str) -> None:
        """Persist the output file path immediately so orphan recovery can find it."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.out_path = out_path
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("UPDATE jobs SET out_path = %s WHERE id = %s", (out_path, job_id))
            conn.commit()

    def flush_log(self, job_id: str, log_lines: list[str]) -> None:
        """Flush the current log lines to DB so they survive a server restart."""
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE jobs SET log_text = %s WHERE id = %s",
                ("\n".join(log_lines) if log_lines else None, job_id),
            )
            conn.commit()

    def get_log_lines(self, job_id: str) -> list[str]:
        """Fetch the current log lines for a job directly from DB.

        Used by the orphan watcher to forward lines to a cross-worker SSE
        stream that can't access the original reader thread's queue.
        """
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT log_text FROM jobs WHERE id = %s", (job_id,))
            row = cur.fetchone()
        return row[0].splitlines() if row and row[0] else []

    def set_completion_callback(self, job_id: str, fn) -> None:
        """Register (or replace) the completion callback for an existing job.

        Used at startup to re-attach callbacks to orphaned subprocesses that
        survived a gunicorn restart.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job._completion_fn = fn

    def resolve_orphan(self, job_id: str, status: str, exit_code: int) -> None:
        """Mark an orphaned job done/failed without touching log_text.

        The orphan watcher runs on a different worker than the one that
        launched the job.  That worker's reader thread may have already
        written the real log to DB.  Using a targeted UPDATE (no log_text
        column) prevents the stale in-memory log_lines from overwriting it.
        The WHERE status='running' guard ensures we lose no data if the
        reader thread on the other worker already resolved the job.
        """
        now = datetime.now(tz=timezone.utc)
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE jobs
                   SET status = %s, exit_code = %s, finished_at = %s
                 WHERE id = %s AND status = 'running'
                """,
                (status, exit_code, now, job_id),
            )
            conn.commit()
        # Sync in-memory state regardless of whether the UPDATE fired.
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job.status = status
                job.exit_code = exit_code
                job.finished_at = now

    def mark_cancelled(self, job_id: str) -> bool:
        """Mark a running or queued job as cancelled.
        Returns True if the job existed and was running or queued."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status not in ("running", "queued"):
                return False
            job.status = "cancelled"
        return True

    def delete(self, job_id: str) -> bool:
        """Remove a job from memory and the database. Returns True if found."""
        with self._lock:
            if job_id not in self._jobs:
                return False
            del self._jobs[job_id]
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM jobs WHERE id = %s", (job_id,))
            conn.commit()
        return True

    # ------------------------------------------------------------------
    # Subscription / plan methods
    # ------------------------------------------------------------------

    def get_user_plan(self, user_id: str) -> str:
        """Return 'pro' if user has an active Pro subscription, else 'free'.
        Admins always get 'pro' (caller must pass role separately)."""
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT plan, status FROM subscriptions WHERE user_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
        if row is None:
            return "free"
        plan, status = row
        if plan == "pro" and status in ("active", "trialing"):
            return "pro"
        return "free"

    def count_monthly_jobs(self, user_id: str, command: str) -> int:
        """Count done jobs for this user in the current calendar month."""
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM jobs
                WHERE user_id = %s
                  AND command = %s
                  AND status  = 'done'
                  AND started_at >= date_trunc('month', NOW())
                """,
                (user_id, command),
            )
            return cur.fetchone()[0]

    def upsert_subscription(
        self,
        *,
        user_id: str,
        stripe_customer_id: str,
        stripe_subscription_id: str | None,
        plan: str,
        status: str,
        current_period_end: datetime | None,
    ) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO subscriptions (
                    user_id, stripe_customer_id, stripe_subscription_id,
                    plan, status, current_period_end, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (user_id) DO UPDATE SET
                    stripe_customer_id     = EXCLUDED.stripe_customer_id,
                    stripe_subscription_id = COALESCE(EXCLUDED.stripe_subscription_id,
                                                      subscriptions.stripe_subscription_id),
                    plan                   = EXCLUDED.plan,
                    status                 = EXCLUDED.status,
                    current_period_end     = COALESCE(EXCLUDED.current_period_end,
                                                      subscriptions.current_period_end),
                    updated_at             = NOW()
                """,
                (user_id, stripe_customer_id, stripe_subscription_id,
                 plan, status, current_period_end),
            )
            conn.commit()

    def get_stripe_customer_id(self, user_id: str) -> str | None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT stripe_customer_id FROM subscriptions WHERE user_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
        return row[0] if row else None

    def get_user_id_by_stripe_customer(self, stripe_customer_id: str) -> str | None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT user_id FROM subscriptions WHERE stripe_customer_id = %s",
                (stripe_customer_id,),
            )
            row = cur.fetchone()
        return str(row[0]) if row else None

    def get_subscription(self, user_id: str) -> dict | None:
        """Return full subscription row as dict, or None."""
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT stripe_customer_id, stripe_subscription_id,
                       plan, status, current_period_end, updated_at
                FROM subscriptions WHERE user_id = %s
                """,
                (user_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return {
            "stripe_customer_id":     row[0],
            "stripe_subscription_id": row[1],
            "plan":                   row[2],
            "status":                 row[3],
            "current_period_end":     row[4].isoformat() if row[4] else None,
            "updated_at":             row[5].isoformat() if row[5] else None,
        }

    def update_status(
        self,
        job_id: str,
        status: str,
        exit_code: int | None = None,
        out_path: str | None = None,
    ) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.status = status
            job.finished_at = datetime.now(tz=timezone.utc)
            if exit_code is not None:
                job.exit_code = exit_code
            if out_path is not None:
                job.out_path = out_path
        self._persist(job)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @contextmanager
    def _conn(self):
        conn = self._pool.getconn()
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    def _init_db(self) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(_DDL)
            conn.commit()

    def _load_existing(self) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, command, params, status, started_at, finished_at,
                       out_path, exit_code, log_text, user_id, pid
                FROM jobs
                ORDER BY started_at DESC
                """
            )
            rows = cur.fetchall()

        orphans: list[Job] = []

        for row in rows:
            job_id, command, params, status, started_at, finished_at, \
                out_path, exit_code, log_text, user_id, pid = row

            if status == "running":
                # Check if the output file already exists (job finished before restart).
                if out_path and Path(out_path).exists():
                    status    = "done"
                    exit_code = 0
                elif pid and _pid_alive(pid):
                    # Subprocess is still running as an orphan — keep "running"
                    # and spawn a watcher thread to detect completion.
                    pass   # status stays "running"; added to orphans below
                else:
                    status = "cancelled"

            job = Job(
                id=str(job_id),
                command=command,
                params=params,
                status=status,
                started_at=started_at,
                finished_at=finished_at,
                out_path=out_path,
                exit_code=exit_code,
                pid=pid,
                user_id=str(user_id) if user_id else None,
                log_lines=log_text.splitlines() if log_text else [],
            )
            self._jobs[job.id] = job

            if status == "running":
                orphans.append(job)

        # Persist corrected statuses (done / cancelled) back to DB.
        for job in self._jobs.values():
            if job.status in ("done", "cancelled") and job.finished_at is None:
                job.finished_at = datetime.now(tz=timezone.utc)
                self._persist(job)

        # Spawn watcher threads for orphaned subprocesses.
        for job in orphans:
            threading.Thread(
                target=_watch_orphan, args=(job, self), daemon=True
            ).start()

    def _fetch_from_db(self, job_id: str) -> Job | None:
        """Load a single job from the DB — used when it wasn't found in memory."""
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, command, params, status, started_at, finished_at,
                       out_path, exit_code, log_text, user_id, pid
                FROM jobs WHERE id = %s
                """,
                (job_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        job_id_, command, params, status, started_at, finished_at, \
            out_path, exit_code, log_text, user_id, pid = row
        # Running job owned by another worker: check output file + PID before giving up.
        # Only mark cancelled if we know for certain the process is dead (PID was set
        # but the process no longer exists).  If PID is NULL the subprocess is very
        # new (started within the last few seconds) — keep "running" so the UI
        # doesn't flash "cancelled" while the launcher thread is still setting up.
        orphan = False
        if status == "running":
            if out_path and Path(out_path).exists():
                status    = "done"
                exit_code = 0
            elif pid and _pid_alive(pid):
                orphan = True  # still alive on another worker — keep "running"
            elif pid and not _pid_alive(pid):
                status = "cancelled"  # PID known but dead → definitely cancelled
            # else: pid is None → subprocess just started, PID not written yet → keep "running"
        job = Job(
            id=str(job_id_),
            command=command,
            params=params,
            status=status,
            started_at=started_at,
            finished_at=finished_at,
            out_path=out_path,
            exit_code=exit_code,
            pid=pid,
            user_id=str(user_id) if user_id else None,
            log_lines=log_text.splitlines() if log_text else [],
        )
        with self._lock:
            self._jobs[job.id] = job  # cache it so subsequent requests are fast
        if orphan:
            # Pre-populate the queue with any log lines already in the DB so
            # that a cross-worker SSE stream can replay them immediately.
            for line in job.log_lines:
                job.queue.put(line)
            threading.Thread(target=_watch_orphan, args=(job, self), daemon=True).start()
        return job

    def _persist(self, job: Job) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO jobs (
                    id, user_id, command, params, status,
                    started_at, finished_at, out_path, exit_code, log_text, pid
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    status      = EXCLUDED.status,
                    finished_at = EXCLUDED.finished_at,
                    out_path    = EXCLUDED.out_path,
                    exit_code   = EXCLUDED.exit_code,
                    log_text    = EXCLUDED.log_text,
                    pid         = COALESCE(EXCLUDED.pid, jobs.pid)
                """,
                (
                    job.id,
                    job.user_id,
                    job.command,
                    psycopg2.extras.Json(job.params),
                    job.status,
                    job.started_at,
                    job.finished_at,
                    job.out_path,
                    job.exit_code,
                    "\n".join(job.log_lines) if job.log_lines else None,
                    job.pid,
                ),
            )
            conn.commit()


# ---------------------------------------------------------------------------
# Global job queue
# ---------------------------------------------------------------------------

class JobQueue:
    """Limits concurrent heavy jobs to MAX_CONCURRENT across all users.

    Jobs that arrive when all slots are taken are marked "queued" and
    launched in FIFO order as slots free up.  Light jobs (fetch, import)
    bypass the queue entirely.
    """

    HEAVY = {"search", "habits", "strategise", "repertoire"}
    MAX_CONCURRENT = 4

    def __init__(self) -> None:
        self._sem = threading.Semaphore(self.MAX_CONCURRENT)
        self._lock = threading.Lock()
        self._waiting: list[str] = []   # job IDs in FIFO order

    def enqueue(self, job: "Job", registry: "JobRegistry") -> None:
        """Submit a job.  Launches immediately if a slot is free, else queues it."""
        if job.command not in self.HEAVY:
            if job._launch_fn:
                job._launch_fn()
            return

        acquired = self._sem.acquire(blocking=False)
        if acquired:
            self._start(job, registry)
        else:
            with self._lock:
                self._waiting.append(job.id)
            with registry._lock:
                job.status = "queued"

    def queue_position(self, job_id: str) -> int | None:
        """Return 1-based queue position, or None if not queued."""
        with self._lock:
            try:
                return self._waiting.index(job_id) + 1
            except ValueError:
                return None

    def _start(self, job: "Job", registry: "JobRegistry") -> None:
        """Mark job running and launch it; release the slot when it finishes."""
        with registry._lock:
            job.status = "running"

        original_launch = job._launch_fn

        def _wrapped() -> None:
            try:
                if original_launch:
                    original_launch()
                while job.status == "running":
                    time.sleep(1)
            finally:
                self._sem.release()
                self._promote_next(registry)

        threading.Thread(target=_wrapped, daemon=True).start()

    def _promote_next(self, registry: "JobRegistry") -> None:
        """Start the next queued job if one exists."""
        with self._lock:
            if not self._waiting:
                return
            next_id = self._waiting.pop(0)

        with registry._lock:
            job = registry._jobs.get(next_id)
        if job is None or job.status == "cancelled":
            self._sem.release()
            self._promote_next(registry)
            return

        if self._sem.acquire(blocking=False):
            self._start(job, registry)

    def remove(self, job_id: str) -> None:
        """Remove a cancelled job from the wait list (if present)."""
        with self._lock:
            try:
                self._waiting.remove(job_id)
            except ValueError:
                pass


# ---------------------------------------------------------------------------
# Orphan recovery helpers (module-level so they can be used by JobRegistry)
# ---------------------------------------------------------------------------

def _pid_alive(pid: int) -> bool:
    """Return True if a process with the given PID is currently running."""
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def _watch_orphan(job: "Job", registry: "JobRegistry") -> None:
    """Poll an orphaned subprocess until it completes or dies.

    Called in a daemon thread when a running job's subprocess lives on a
    different gunicorn worker.  Polls the DB for new log lines every few
    seconds and forwards them to the orphan job's queue so a cross-worker
    SSE stream sees live-ish progress.  When the output file appears (or
    the PID dies) it marks the job done or failed accordingly.
    """
    out = Path(job.out_path) if job.out_path else None
    pid = job.pid
    # Number of log lines already put into the queue (from _fetch_from_db).
    emitted = len(job.log_lines)

    def _drain_new_lines() -> None:
        """Fetch any log lines added since last check and emit them."""
        nonlocal emitted
        all_lines = registry.get_log_lines(str(job.id))
        for line in all_lines[emitted:]:
            job.queue.put(line)
            emitted += 1

    while True:
        # Forward any new log lines that the other worker flushed to DB.
        _drain_new_lines()

        # Output file exists → success.  We intentionally do NOT require
        # st_size > 0 because a fetch that finds 0 games writes an empty
        # PGN file and exits 0 — that is a legitimate success.
        if out and out.exists():
            # Wait briefly for the reader thread to flush its final log.
            time.sleep(2)
            _drain_new_lines()
            registry.resolve_orphan(job.id, "done", 0)
            job.queue.put(None)  # SSE sentinel
            if job._completion_fn:
                try:
                    job._completion_fn("done", 0)
                except Exception:
                    pass
            return

        if pid and not _pid_alive(pid):
            # Process is gone.  Wait briefly in case the file is still
            # being flushed to disk, then do one final file check before
            # giving up.
            time.sleep(2)
            _drain_new_lines()
            if out and out.exists():
                registry.resolve_orphan(job.id, "done", exit_code=0)
                job.queue.put(None)
                if job._completion_fn:
                    try:
                        job._completion_fn("done", 0)
                    except Exception:
                        pass
                return
            # Use resolve_orphan (not update_status) so we never overwrite
            # log_text.  The real reader thread on another worker may have
            # already persisted the actual subprocess output; we must not
            # clobber it with our stale in-memory log_lines.
            # WHERE status='running' in resolve_orphan means this is a no-op
            # if the other worker already resolved the job.
            registry.resolve_orphan(job.id, "failed", -1)
            job.queue.put(None)
            if job._completion_fn:
                try:
                    job._completion_fn("failed", -1)
                except Exception:
                    pass
            return

        time.sleep(3)
