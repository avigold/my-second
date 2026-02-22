"""Job registry: in-memory store + SQLite persistence for web UI jobs."""

from __future__ import annotations

import json
import queue
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id           TEXT PRIMARY KEY,
    command      TEXT NOT NULL,
    params_json  TEXT NOT NULL,
    status       TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    out_path     TEXT,
    exit_code    INTEGER
);
"""


@dataclass
class Job:
    id: str
    command: str          # "fetch" or "search"
    params: dict          # form inputs that produced this job
    status: str           # "running" | "done" | "failed" | "cancelled"
    started_at: datetime
    finished_at: datetime | None = None
    out_path: str | None = None
    exit_code: int | None = None
    # Not persisted — live only while the process runs:
    log_lines: list[str] = field(default_factory=list, repr=False)
    queue: queue.Queue = field(default_factory=queue.Queue, repr=False)
    process: Any = field(default=None, repr=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "command": self.command,
            "params": self.params,
            "status": self.status,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "out_path": self.out_path,
            "exit_code": self.exit_code,
        }


class JobRegistry:
    """Thread-safe in-memory store with SQLite persistence."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
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
    ) -> Job:
        job = Job(
            id=str(uuid.uuid4()),
            command=command,
            params=params,
            status="running",
            started_at=datetime.now(tz=timezone.utc),
            out_path=out_path,
        )
        with self._lock:
            self._jobs[job.id] = job
        self._persist(job)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list_all(self) -> list[dict]:
        with self._lock:
            jobs = list(self._jobs.values())
        return [j.to_dict() for j in sorted(jobs, key=lambda j: j.started_at, reverse=True)]

    def delete(self, job_id: str) -> bool:
        """Remove a job from memory and the database. Returns True if found."""
        with self._lock:
            if job_id not in self._jobs:
                return False
            del self._jobs[job_id]
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        return True

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

    def _init_db(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(_SCHEMA)

    def _load_existing(self) -> None:
        """Load historical jobs from DB so they appear on the dashboard."""
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(
                "SELECT id, command, params_json, status, started_at, finished_at, out_path, exit_code FROM jobs"
            ).fetchall()
        for row in rows:
            job_id, command, params_json, status, started_at, finished_at, out_path, exit_code = row
            # Mark any jobs that were "running" when the server last died as cancelled.
            if status == "running":
                status = "cancelled"
            job = Job(
                id=job_id,
                command=command,
                params=json.loads(params_json),
                status=status,
                started_at=datetime.fromisoformat(started_at),
                finished_at=datetime.fromisoformat(finished_at) if finished_at else None,
                out_path=out_path,
                exit_code=exit_code,
            )
            self._jobs[job_id] = job

    def _persist(self, job: Job) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """
                INSERT INTO jobs (id, command, params_json, status, started_at, finished_at, out_path, exit_code)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status=excluded.status,
                    finished_at=excluded.finished_at,
                    out_path=excluded.out_path,
                    exit_code=excluded.exit_code
                """,
                (
                    job.id,
                    job.command,
                    json.dumps(job.params),
                    job.status,
                    job.started_at.isoformat(),
                    job.finished_at.isoformat() if job.finished_at else None,
                    job.out_path,
                    job.exit_code,
                ),
            )
