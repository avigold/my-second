"""Tests for web/jobs.py — JobRegistry changes for the Redis-backed worker.

Focused on the six methods changed during the Redis worker refactor:
  create()                  → status must be "queued" (not "running")
  get()                     → re-fetches from DB for active jobs
  has_running_job()         → queries DB, not in-memory state
  get_log_and_status_from_db() → new method for SSE replay
  mark_cancelled()          → DB-first (worker sees it immediately)
  admin_stats()             → running_jobs from DB, not in-memory

All PostgreSQL I/O is mocked — no real DB connection needed.
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "web"))
sys.path.insert(0, str(_ROOT / "src"))

from jobs import Job, JobRegistry  # noqa: E402


# ---------------------------------------------------------------------------
# Mock-building helpers
# ---------------------------------------------------------------------------


def _make_cur(fetchone=None, fetchall=None):
    """Build a mock cursor that works as a ``with conn.cursor() as cur:`` context.

    ``cur.__enter__.return_value = cur`` ensures that
    ``with conn.cursor() as local_cur:`` binds ``local_cur`` to this same mock,
    not to a freshly-created child MagicMock.
    """
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone.return_value = fetchone
    cur.fetchall.return_value = fetchall if fetchall is not None else []
    cur.rowcount = 0
    return cur


def _make_pool(cur: MagicMock) -> MagicMock:
    conn = MagicMock()
    conn.cursor.return_value = cur
    pool = MagicMock()
    pool.getconn.return_value = conn
    return pool


# ---------------------------------------------------------------------------
# Fixture: JobRegistry with fully mocked DB
# ---------------------------------------------------------------------------


@pytest.fixture()
def cur():
    return _make_cur()


@pytest.fixture()
def pool(cur):
    return _make_pool(cur)


@pytest.fixture()
def registry(pool, cur):
    """A JobRegistry instance whose DB pool is replaced with the mock pool."""
    with patch("psycopg2.pool.ThreadedConnectionPool", return_value=pool), \
         patch.object(JobRegistry, "_init_db"), \
         patch.object(JobRegistry, "_load_existing"):
        reg = JobRegistry("postgresql://test/test")
    # Wire up the mock pool directly so _conn() uses it.
    reg._pool = pool
    return reg


# ---------------------------------------------------------------------------
# create() — status must be "queued"
# ---------------------------------------------------------------------------


class TestCreate:
    def test_status_is_queued(self, registry):
        job = registry.create("search", {"side": "white"}, user_id="user-1")

        assert job.status == "queued"

    def test_not_running_at_creation(self, registry):
        """Previously create() set status='running'; now it must be 'queued'."""
        job = registry.create("habits", {"username": "foo"})

        assert job.status != "running"

    def test_job_has_valid_uuid(self, registry):
        job = registry.create("fetch", {})

        uuid.UUID(job.id)  # raises if invalid

    def test_job_stored_in_memory(self, registry):
        job = registry.create("habits", {"username": "foo"})

        assert registry._jobs.get(job.id) is job

    def test_job_persisted_to_db(self, registry, cur):
        registry.create("repertoire", {"username": "foo"}, user_id="uid")

        # _persist() calls INSERT ... ON CONFLICT ... DO UPDATE
        assert cur.execute.called

    def test_out_path_none_by_default(self, registry):
        job = registry.create("search", {})

        assert job.out_path is None

    def test_out_path_stored(self, registry):
        job = registry.create("fetch", {}, out_path="/data/out.pgn")

        assert job.out_path == "/data/out.pgn"

    def test_user_id_stored(self, registry):
        job = registry.create("search", {}, user_id="user-uuid-abc")

        assert job.user_id == "user-uuid-abc"

    def test_command_stored(self, registry):
        job = registry.create("strategise", {"player": "foo", "opponent": "bar"})

        assert job.command == "strategise"

    def test_params_stored(self, registry):
        params = {"side": "black", "plies": 12}
        job = registry.create("search", params)

        assert job.params == params

    def test_multiple_creates_have_different_ids(self, registry):
        j1 = registry.create("fetch", {})
        j2 = registry.create("fetch", {})

        assert j1.id != j2.id


# ---------------------------------------------------------------------------
# get() — re-fetches from DB for active jobs
# ---------------------------------------------------------------------------


def _db_row(job_id="test-job", command="search", params=None, status="running",
            started_at=None, finished_at=None, out_path="/out.pgn",
            exit_code=None, log_text=None, user_id="user-1", pid=None):
    """Build a fake DB row tuple matching _fetch_from_db's SELECT column order."""
    return (
        job_id, command, params or {}, status,
        started_at or datetime.now(tz=timezone.utc),
        finished_at, out_path, exit_code, log_text, user_id, pid,
    )


class TestGet:
    def test_returns_none_for_unknown_job(self, registry, cur):
        cur.fetchone.return_value = None

        result = registry.get("nonexistent-id")

        assert result is None

    def test_returns_in_memory_done_job_without_db_hit(self, registry, cur):
        job = Job(
            id="done-job", command="search", params={}, status="done",
            started_at=datetime.now(tz=timezone.utc),
        )
        registry._jobs["done-job"] = job

        result = registry.get("done-job")

        assert result is job
        # _fetch_from_db should NOT have been called (no DB query for done jobs)
        cur.execute.assert_not_called()

    def test_returns_in_memory_failed_job_without_db_hit(self, registry, cur):
        job = Job(
            id="failed-job", command="habits", params={}, status="failed",
            started_at=datetime.now(tz=timezone.utc),
        )
        registry._jobs["failed-job"] = job

        result = registry.get("failed-job")

        assert result is job
        cur.execute.assert_not_called()

    def test_refetches_for_queued_job(self, registry, cur):
        """Queued in-memory job → hits DB for fresh status."""
        job = Job(
            id="queued-job", command="search", params={}, status="queued",
            started_at=datetime.now(tz=timezone.utc),
        )
        registry._jobs["queued-job"] = job
        # DB returns the job, now with status=running (worker picked it up)
        cur.fetchone.return_value = _db_row("queued-job", status="running")

        result = registry.get("queued-job")

        assert result is not None
        assert result.status == "running"

    def test_refetches_for_running_job(self, registry, cur):
        """Running in-memory job → hits DB for fresh status."""
        job = Job(
            id="running-job", command="habits", params={}, status="running",
            started_at=datetime.now(tz=timezone.utc),
        )
        registry._jobs["running-job"] = job
        cur.fetchone.return_value = _db_row(
            "running-job", command="habits", status="done",
            finished_at=datetime.now(tz=timezone.utc), exit_code=0,
        )

        result = registry.get("running-job")

        assert result.status == "done"

    def test_falls_back_to_memory_if_db_has_no_row(self, registry, cur):
        """If DB returns nothing for an active job, fall back to in-memory."""
        job = Job(
            id="mem-only", command="fetch", params={}, status="queued",
            started_at=datetime.now(tz=timezone.utc),
        )
        registry._jobs["mem-only"] = job
        cur.fetchone.return_value = None  # DB gap (e.g. transaction delay)

        result = registry.get("mem-only")

        assert result is job

    def test_cancelled_in_memory_not_refetched(self, registry, cur):
        job = Job(
            id="cancelled-job", command="search", params={}, status="cancelled",
            started_at=datetime.now(tz=timezone.utc),
        )
        registry._jobs["cancelled-job"] = job

        result = registry.get("cancelled-job")

        assert result is job
        cur.execute.assert_not_called()

    def test_fetches_from_db_when_not_in_memory(self, registry, cur):
        """Job not in memory at all → fetched from DB."""
        cur.fetchone.return_value = _db_row("db-only-job", status="done")

        result = registry.get("db-only-job")

        assert result is not None
        assert result.id == "db-only-job"
        assert result.status == "done"


# ---------------------------------------------------------------------------
# has_running_job() — must query DB, not in-memory
# ---------------------------------------------------------------------------


class TestHasRunningJob:
    def test_returns_true_when_db_has_running_job(self, registry, cur):
        cur.fetchone.return_value = (1,)  # COUNT = 1

        assert registry.has_running_job("user-abc") is True

    def test_returns_false_when_db_has_no_active_jobs(self, registry, cur):
        cur.fetchone.return_value = (0,)

        assert registry.has_running_job("user-abc") is False

    def test_sql_checks_both_running_and_queued(self, registry, cur):
        cur.fetchone.return_value = (0,)

        registry.has_running_job("user-abc")

        sql = cur.execute.call_args[0][0]
        assert "running" in sql
        assert "queued" in sql

    def test_does_not_rely_on_in_memory_state(self, registry, cur):
        """Even with no in-memory jobs, DB is queried and result trusted."""
        assert not registry._jobs  # no in-memory jobs
        cur.fetchone.return_value = (1,)  # DB says there IS a running job

        # Worker on another gunicorn process owns the job — must still detect it
        assert registry.has_running_job("user-xyz") is True

    def test_in_memory_running_job_ignored_if_db_says_zero(self, registry, cur):
        """If worker already finished the job, DB is the source of truth."""
        job = Job(
            id="stale-running", command="habits", params={}, status="running",
            started_at=datetime.now(tz=timezone.utc),
        )
        registry._jobs["stale-running"] = job
        cur.fetchone.return_value = (0,)  # DB: already done

        assert registry.has_running_job("user-1") is False

    def test_passes_user_id_to_db_query(self, registry, cur):
        cur.fetchone.return_value = (0,)

        registry.has_running_job("specific-user-id")

        args = cur.execute.call_args[0][1]
        assert "specific-user-id" in args


# ---------------------------------------------------------------------------
# get_log_and_status_from_db()
# ---------------------------------------------------------------------------


class TestGetLogAndStatusFromDb:
    def test_returns_split_lines_and_status(self, registry, cur):
        cur.fetchone.return_value = ("line1\nline2\nline3", "running")

        lines, status = registry.get_log_and_status_from_db("job-abc")

        assert lines == ["line1", "line2", "line3"]
        assert status == "running"

    def test_empty_log_text_returns_empty_list(self, registry, cur):
        cur.fetchone.return_value = (None, "queued")

        lines, status = registry.get_log_and_status_from_db("job-new")

        assert lines == []
        assert status == "queued"

    def test_job_not_found_returns_empty_and_failed(self, registry, cur):
        cur.fetchone.return_value = None

        lines, status = registry.get_log_and_status_from_db("nonexistent")

        assert lines == []
        assert status == "failed"

    def test_single_line_log(self, registry, cur):
        cur.fetchone.return_value = ("just one line", "done")

        lines, status = registry.get_log_and_status_from_db("job-single")

        assert lines == ["just one line"]

    def test_trailing_newline_not_added(self, registry, cur):
        cur.fetchone.return_value = ("line1\nline2", "done")

        lines, _ = registry.get_log_and_status_from_db("job-x")

        assert len(lines) == 2
        assert lines[-1] == "line2"

    def test_100_lines_all_returned(self, registry, cur):
        big_log = "\n".join(f"line {i}" for i in range(100))
        cur.fetchone.return_value = (big_log, "done")

        lines, _ = registry.get_log_and_status_from_db("job-big")

        assert len(lines) == 100
        assert lines[0] == "line 0"
        assert lines[99] == "line 99"

    @pytest.mark.parametrize("status", ["queued", "running", "done", "failed", "cancelled"])
    def test_all_statuses_returned_correctly(self, registry, cur, status):
        cur.fetchone.return_value = ("log", status)

        _, result_status = registry.get_log_and_status_from_db("job-x")

        assert result_status == status


# ---------------------------------------------------------------------------
# mark_cancelled() — DB-first so the worker process sees it immediately
# ---------------------------------------------------------------------------


class TestMarkCancelled:
    def test_returns_true_when_cancelled_successfully(self, registry, cur):
        cur.fetchone.return_value = ("job-abc",)  # RETURNING id

        result = registry.mark_cancelled("job-abc")

        assert result is True

    def test_returns_false_when_job_already_done(self, registry, cur):
        cur.fetchone.return_value = None  # UPDATE matched 0 rows

        result = registry.mark_cancelled("job-already-done")

        assert result is False

    def test_returns_false_when_job_already_cancelled(self, registry, cur):
        cur.fetchone.return_value = None

        result = registry.mark_cancelled("job-already-cancelled")

        assert result is False

    def test_db_updated_before_in_memory_state(self, registry, cur):
        """CRITICAL: DB write must happen before in-memory update so the worker
        sees the cancellation signal the very next time it checks status."""
        job = Job(
            id="job-to-cancel", command="search", params={}, status="running",
            started_at=datetime.now(tz=timezone.utc),
        )
        registry._jobs["job-to-cancel"] = job

        status_at_db_update_time = []

        def track_execute(sql, *args, **kwargs):
            if "UPDATE jobs SET status='cancelled'" in str(sql):
                # Capture in-memory job.status at the exact moment the DB UPDATE fires.
                # It must still be "running" here — the in-memory update comes after.
                status_at_db_update_time.append(job.status)
            # Return None (the default for a mock) — no real DB call needed.

        cur.execute.side_effect = track_execute
        cur.fetchone.return_value = ("job-to-cancel",)

        registry.mark_cancelled("job-to-cancel")

        # At the time of DB write, in-memory was still "running"
        assert status_at_db_update_time == ["running"]
        # After the call, in-memory is updated
        assert job.status == "cancelled"

    def test_in_memory_status_updated_to_cancelled(self, registry, cur):
        job = Job(
            id="job-mem", command="habits", params={}, status="running",
            started_at=datetime.now(tz=timezone.utc),
        )
        registry._jobs["job-mem"] = job
        cur.fetchone.return_value = ("job-mem",)

        registry.mark_cancelled("job-mem")

        assert job.status == "cancelled"

    def test_cancel_queued_job_works(self, registry, cur):
        """Queued jobs (not yet picked up by worker) must also be cancellable."""
        job = Job(
            id="job-queued", command="search", params={}, status="queued",
            started_at=datetime.now(tz=timezone.utc),
        )
        registry._jobs["job-queued"] = job
        cur.fetchone.return_value = ("job-queued",)

        result = registry.mark_cancelled("job-queued")

        assert result is True
        assert job.status == "cancelled"

    def test_sql_only_matches_running_or_queued(self, registry, cur):
        """The WHERE clause must prevent cancelling already-done/failed jobs."""
        cur.fetchone.return_value = None

        registry.mark_cancelled("job-abc")

        sql = cur.execute.call_args[0][0]
        assert "running" in sql
        assert "queued" in sql
        # NOT: UPDATE ... WHERE id=%s (no status check) — that would cancel done jobs

    def test_commits_to_db(self, registry, cur):
        cur.fetchone.return_value = ("job-abc",)

        registry.mark_cancelled("job-abc")

        pool = registry._pool
        pool.getconn().commit.assert_called()

    def test_job_not_in_memory_but_in_db(self, registry, cur):
        """Worker's job (not in Flask's memory) should still be cancellable via DB."""
        cur.fetchone.return_value = ("worker-job-id",)

        result = registry.mark_cancelled("worker-job-id")

        assert result is True
        # No crash even though job is not in _jobs dict

    def test_cancelled_job_not_in_memory_no_keyerror(self, registry, cur):
        """If job isn't in memory dict, no KeyError should be raised."""
        cur.fetchone.return_value = ("job-not-in-mem",)

        # Should not raise KeyError
        registry.mark_cancelled("job-not-in-mem")


# ---------------------------------------------------------------------------
# admin_stats() — running_jobs must come from DB, not in-memory
# ---------------------------------------------------------------------------


class TestAdminStats:
    def test_returns_all_expected_keys(self, registry, cur):
        cur.fetchone.side_effect = [
            (42,),  # total_users
            (7,),   # pro_subscribers
            (15,),  # jobs_today
            (3,),   # running_jobs
        ]

        stats = registry.admin_stats()

        assert "total_users" in stats
        assert "pro_subscribers" in stats
        assert "jobs_today" in stats
        assert "running_jobs" in stats

    def test_values_match_db_counts(self, registry, cur):
        cur.fetchone.side_effect = [(100,), (25,), (50,), (8,)]

        stats = registry.admin_stats()

        assert stats["total_users"] == 100
        assert stats["pro_subscribers"] == 25
        assert stats["jobs_today"] == 50
        assert stats["running_jobs"] == 8

    def test_running_jobs_queries_db_not_memory(self, registry, cur):
        """Worker updates DB but not Flask's memory — must trust DB."""
        # Add in-memory running job
        job = Job(
            id="stale-mem-job", command="search", params={}, status="running",
            started_at=datetime.now(tz=timezone.utc),
        )
        registry._jobs["stale-mem-job"] = job

        # DB says 0 running jobs (worker already finished it)
        cur.fetchone.side_effect = [(10,), (2,), (5,), (0,)]

        stats = registry.admin_stats()

        # Must trust DB count (0), not in-memory count (1)
        assert stats["running_jobs"] == 0

    def test_running_jobs_sql_includes_queued_status(self, registry, cur):
        """Queued jobs (waiting for worker) should also count as 'active'."""
        cur.fetchone.side_effect = [(0,), (0,), (0,), (0,)]

        registry.admin_stats()

        # Find the SQL query that counts running jobs
        all_sql = [c[0][0] for c in cur.execute.call_args_list]
        running_query = next(
            (q for q in all_sql if "running" in q and "queued" in q), None
        )
        assert running_query is not None, (
            "Expected a COUNT query that includes both 'running' and 'queued' statuses"
        )

    def test_zero_counts_work(self, registry, cur):
        cur.fetchone.side_effect = [(0,), (0,), (0,), (0,)]

        stats = registry.admin_stats()

        assert stats["total_users"] == 0
        assert stats["running_jobs"] == 0
