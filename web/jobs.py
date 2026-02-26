"""Job registry: in-memory store + PostgreSQL persistence for web UI jobs."""

from __future__ import annotations

import json
import queue
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
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

CREATE TABLE IF NOT EXISTS subscriptions (
    user_id                UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    stripe_customer_id     TEXT UNIQUE,
    stripe_subscription_id TEXT UNIQUE,
    plan                   TEXT NOT NULL DEFAULT 'free',
    status                 TEXT NOT NULL DEFAULT 'active',
    current_period_end     TIMESTAMPTZ,
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
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
    # Not persisted — live only while the process runs:
    log_lines: list[str] = field(default_factory=list, repr=False)
    queue: queue.Queue    = field(default_factory=queue.Queue, repr=False)
    process: Any          = field(default=None, repr=False)
    # Callback set by the job queue to actually launch the subprocess:
    _launch_fn: Any       = field(default=None, repr=False)

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
    ) -> dict:
        """Create or update a user by platform ID. Returns user dict."""
        with self._conn() as conn, conn.cursor() as cur:
            if lichess_id:
                cur.execute(
                    """
                    INSERT INTO users (lichess_id, username)
                    VALUES (%s, %s)
                    ON CONFLICT (lichess_id) DO UPDATE SET username = EXCLUDED.username
                    RETURNING id, username, role
                    """,
                    (lichess_id, username),
                )
                platform = "lichess"
            elif chesscom_id:
                cur.execute(
                    """
                    INSERT INTO users (chesscom_id, username)
                    VALUES (%s, %s)
                    ON CONFLICT (chesscom_id) DO UPDATE SET username = EXCLUDED.username
                    RETURNING id, username, role
                    """,
                    (chesscom_id, username),
                )
                platform = "chesscom"
            else:
                cur.execute(
                    """
                    INSERT INTO users (google_id, username)
                    VALUES (%s, %s)
                    ON CONFLICT (google_id) DO UPDATE SET username = EXCLUDED.username
                    RETURNING id, username, role
                    """,
                    (google_id, username),
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
                    MAX(j.started_at)           AS last_active
                FROM users u
                LEFT JOIN subscriptions s ON s.user_id = u.id
                LEFT JOIN jobs j ON j.user_id = u.id
                GROUP BY u.id, u.username, u.role, u.created_at,
                         u.lichess_id, u.chesscom_id, u.google_id,
                         s.plan, s.status
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
                       out_path, exit_code, log_text, user_id
                FROM jobs
                ORDER BY started_at DESC
                """
            )
            rows = cur.fetchall()

        for row in rows:
            job_id, command, params, status, started_at, finished_at, \
                out_path, exit_code, log_text, user_id = row

            if status == "running":
                status = "cancelled"

            job = Job(
                id=str(job_id),
                command=command,
                params=params,          # psycopg2 deserialises JSONB → dict automatically
                status=status,
                started_at=started_at,
                finished_at=finished_at,
                out_path=out_path,
                exit_code=exit_code,
                user_id=str(user_id) if user_id else None,
                log_lines=log_text.splitlines() if log_text else [],
            )
            self._jobs[job.id] = job

    def _fetch_from_db(self, job_id: str) -> Job | None:
        """Load a single job from the DB — used when it wasn't found in memory."""
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, command, params, status, started_at, finished_at,
                       out_path, exit_code, log_text, user_id
                FROM jobs WHERE id = %s
                """,
                (job_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        job_id_, command, params, status, started_at, finished_at, \
            out_path, exit_code, log_text, user_id = row
        # Can't stream a job owned by another worker — treat as cancelled.
        if status == "running":
            status = "cancelled"
        job = Job(
            id=str(job_id_),
            command=command,
            params=params,
            status=status,
            started_at=started_at,
            finished_at=finished_at,
            out_path=out_path,
            exit_code=exit_code,
            user_id=str(user_id) if user_id else None,
            log_lines=log_text.splitlines() if log_text else [],
        )
        with self._lock:
            self._jobs[job.id] = job  # cache it so subsequent requests are fast
        return job

    def _persist(self, job: Job) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO jobs (
                    id, user_id, command, params, status,
                    started_at, finished_at, out_path, exit_code, log_text
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    status      = EXCLUDED.status,
                    finished_at = EXCLUDED.finished_at,
                    out_path    = EXCLUDED.out_path,
                    exit_code   = EXCLUDED.exit_code,
                    log_text    = EXCLUDED.log_text
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
                import time
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
