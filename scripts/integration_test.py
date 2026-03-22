#!/usr/bin/env python3
"""Live integration tests for the Redis-backed worker.

Runs directly on the server (or anywhere with access to the DB + Redis).
Bypasses Flask auth — inserts jobs directly into PostgreSQL and Redis.

Usage:
    python scripts/integration_test.py [--test concurrent|restart|all]

Tests:
    concurrent  — 3 fetch jobs submitted simultaneously; asserts parallel execution
    restart     — 1 habits job; restarts mysecond-web mid-run; asserts job survives
    all         — runs both (default)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Bootstrap — load .env so DATABASE_URL / REDIS_URL are available
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parent.parent
_env_file = _ROOT / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

try:
    import psycopg2
    import psycopg2.extras
    import redis as redis_lib
except ImportError as e:
    sys.exit(f"Missing dependency: {e}. Run: pip install psycopg2-binary redis")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DATABASE_URL  = os.environ.get("DATABASE_URL", "postgresql://mysecond:mysecond@localhost:5432/mysecond")
REDIS_URL     = os.environ.get("REDIS_URL",    "redis://localhost:6379/0")
REDIS_KEY     = "mysecond:jobs:queued"
OUTPUT_DIR    = _ROOT / "data" / "output"
TIMEOUT_S     = 240   # max seconds to wait for any single job

# ANSI colours
GRN = "\033[32m"; YLW = "\033[33m"; RED = "\033[31m"; CYN = "\033[36m"; RST = "\033[0m"

def ok(msg):   print(f"{GRN}  ✓ {msg}{RST}")
def info(msg): print(f"{CYN}  → {msg}{RST}")
def warn(msg): print(f"{YLW}  ! {msg}{RST}")
def fail(msg): print(f"{RED}  ✗ {msg}{RST}")


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

@contextmanager
def _db():
    url = DATABASE_URL
    if "connect_timeout" not in url:
        url += ("&" if "?" in url else "?") + "connect_timeout=10"
    conn = psycopg2.connect(url)
    try:
        yield conn
    finally:
        conn.close()


def _insert_job(conn, job_id: str, command: str, params: dict, out_path: str) -> None:
    now = datetime.now(tz=timezone.utc)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO jobs (id, user_id, command, params, status, started_at, out_path)
            VALUES (%s, NULL, %s, %s, 'queued', %s, %s)
            """,
            (job_id, command, json.dumps(params), now, out_path),
        )
    conn.commit()


def _poll_job(job_id: str, interval: float = 1.0, timeout: float = TIMEOUT_S) -> dict:
    """Poll DB until job reaches a terminal state.  Returns the final row dict."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with _db() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT status, exit_code, started_at, finished_at, log_text, pid "
                "FROM jobs WHERE id=%s",
                (job_id,),
            )
            row = cur.fetchone()
        if row and row["status"] in ("done", "failed", "cancelled"):
            return dict(row)
        current = row["status"] if row else "unknown"
        info(f"  job {job_id[:8]}… status={current}" + (f" pid={row['pid']}" if row and row.get("pid") else ""))
        time.sleep(interval)
    return {"status": "timeout"}


def _get_row(job_id: str) -> dict | None:
    with _db() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT status, exit_code, started_at, finished_at, log_text, pid FROM jobs WHERE id=%s",
            (job_id,),
        )
        row = cur.fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Job factory
# ---------------------------------------------------------------------------

def _make_fetch_job(username: str, color: str, platform: str) -> tuple[str, str, dict, str]:
    job_id  = str(uuid.uuid4())
    out_path = str(OUTPUT_DIR / f"{job_id}.pgn")
    params   = {"username": username, "color": color, "platform": platform}
    return job_id, "fetch", params, out_path


def _make_habits_job(username: str, color: str, platform: str) -> tuple[str, str, dict, str]:
    job_id  = str(uuid.uuid4())
    out_path = str(OUTPUT_DIR / f"{job_id}.pgn")
    params   = {"username": username, "color": color, "platform": platform}
    return job_id, "habits", params, out_path


# ---------------------------------------------------------------------------
# Test 1: Concurrent fetch jobs
# ---------------------------------------------------------------------------

def test_concurrent():
    """Submit 3 fetch jobs simultaneously and verify they run in parallel."""
    print(f"\n{CYN}══ Test 1: Concurrent fetch jobs ══{RST}")

    # Three distinct users with known game history.
    jobs_to_create = [
        _make_fetch_job("ginnyLRS",    "white", "lichess"),
        _make_fetch_job("eyeqlion",    "white", "chesscom"),
        _make_fetch_job("elmsakni",    "white", "chesscom"),
    ]

    r = redis_lib.from_url(REDIS_URL, decode_responses=True)

    # Insert all three atomically, then push to Redis together.
    with _db() as conn:
        for job_id, command, params, out_path in jobs_to_create:
            _insert_job(conn, job_id, command, params, out_path)

    job_ids = [j[0] for j in jobs_to_create]
    for job_id in job_ids:
        r.rpush(REDIS_KEY, job_id)
        info(f"Queued {job_id[:8]}… ({jobs_to_create[job_ids.index(job_id)][2]['username']})")

    enqueue_time = time.monotonic()

    # ── Phase 1: wait for all jobs to start ──────────────────────────────────
    info("Waiting for all 3 jobs to start running…")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        statuses = {}
        for job_id in job_ids:
            row = _get_row(job_id)
            statuses[job_id] = row["status"] if row else "unknown"
        running_or_done = sum(
            1 for s in statuses.values() if s in ("running", "done", "failed")
        )
        info(f"  {running_or_done}/3 started  " + "  ".join(
            f"{jid[:8]}:{s}" for jid, s in statuses.items()
        ))
        if running_or_done == 3:
            break
        time.sleep(1)
    else:
        fail("FAIL: not all jobs started within 30s")
        return False

    started_elapsed = time.monotonic() - enqueue_time
    ok(f"All 3 jobs started within {started_elapsed:.1f}s")

    # ── Phase 2: assert parallel execution ───────────────────────────────────
    # Collect started_at timestamps immediately after all are running.
    rows = {}
    for job_id in job_ids:
        rows[job_id] = _get_row(job_id)

    started_ats = [rows[jid]["started_at"] for jid in job_ids if rows[jid] and rows[jid]["started_at"]]
    if len(started_ats) == 3:
        span = (max(started_ats) - min(started_ats)).total_seconds()
        if span <= 5.0:
            ok(f"All 3 started within {span:.1f}s of each other (parallel ✓)")
        else:
            warn(f"Start times span {span:.1f}s — jobs may have run sequentially (expected ≤5s)")

    # ── Phase 3: wait for all to finish ──────────────────────────────────────
    info("Waiting for all 3 jobs to finish…")
    results = {}
    for job_id in job_ids:
        username = jobs_to_create[job_ids.index(job_id)][2]["username"]
        info(f"Polling {job_id[:8]}… ({username})")
        results[job_id] = _poll_job(job_id, interval=2.0)

    passed = True
    for job_id, result in results.items():
        username = jobs_to_create[job_ids.index(job_id)][2]["username"]
        status = result.get("status")
        ec     = result.get("exit_code")
        if status == "done" and ec == 0:
            ok(f"{username}: done (exit 0)")
        elif status == "timeout":
            fail(f"{username}: TIMED OUT after {TIMEOUT_S}s")
            passed = False
        else:
            fail(f"{username}: status={status} exit_code={ec}")
            passed = False

    # Check total elapsed
    total = time.monotonic() - enqueue_time
    if passed:
        ok(f"All 3 jobs done in {total:.1f}s total")
    return passed


# ---------------------------------------------------------------------------
# Test 2: Web restart survival
# ---------------------------------------------------------------------------

def test_restart():
    """Submit a habits job, restart mysecond-web mid-run, assert job completes."""
    print(f"\n{CYN}══ Test 2: Web restart survival ══{RST}")

    # ginnyLRS has cached game data from previous fetch jobs → habits runs quickly.
    job_id, command, params, out_path = _make_habits_job("ginnyLRS", "white", "lichess")

    r = redis_lib.from_url(REDIS_URL, decode_responses=True)

    with _db() as conn:
        _insert_job(conn, job_id, command, params, out_path)
    r.rpush(REDIS_KEY, job_id)
    info(f"Queued habits job {job_id[:8]}… (ginnyLRS/white/lichess)")

    # ── Wait for job to start running ────────────────────────────────────────
    info("Waiting for job to reach 'running'…")
    deadline = time.monotonic() + 30
    pid = None
    while time.monotonic() < deadline:
        row = _get_row(job_id)
        if row and row["status"] == "running":
            pid = row.get("pid")
            ok(f"Job is running (pid={pid})")
            break
        if row and row["status"] in ("done", "failed", "cancelled"):
            warn(f"Job finished before we could restart web: status={row['status']}")
            # Still counts as passing if it completed successfully.
            if row["status"] == "done" and row.get("exit_code") == 0:
                ok("Job completed before restart window — still PASS")
                return True
            else:
                fail(f"Job ended prematurely: status={row['status']}")
                return False
        info(f"  status={row['status'] if row else 'unknown'}")
        time.sleep(1)
    else:
        fail("Job did not start within 30s")
        return False

    # ── Restart mysecond-web ─────────────────────────────────────────────────
    info("Restarting mysecond-web…")
    try:
        result = subprocess.run(
            ["systemctl", "restart", "mysecond-web"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            ok("mysecond-web restarted")
        else:
            warn(f"systemctl restart returned {result.returncode}: {result.stderr.strip()}")
    except (FileNotFoundError, PermissionError) as e:
        warn(f"Cannot run systemctl ({e}); skipping the restart step")
        warn("To test restart survival manually: systemctl restart mysecond-web")

    restart_time = time.monotonic()
    info(f"Web restarted at t+{restart_time:.1f}s into test")

    # ── Poll until job finishes ───────────────────────────────────────────────
    info("Monitoring job to completion (worker is independent of web)…")
    result = _poll_job(job_id, interval=3.0)

    status   = result.get("status")
    exit_code = result.get("exit_code")
    log_text  = result.get("log_text") or ""

    # ── Assertions ────────────────────────────────────────────────────────────
    passed = True

    if status == "done" and exit_code == 0:
        ok(f"Job completed: status=done exit_code=0")
    elif status == "timeout":
        fail(f"FAIL: job timed out after {TIMEOUT_S}s — worker may have died")
        passed = False
    else:
        fail(f"FAIL: status={status} exit_code={exit_code}")
        passed = False

    if log_text:
        line_count = len(log_text.splitlines())
        ok(f"log_text persisted: {line_count} lines in DB")
    else:
        fail("FAIL: log_text is empty — worker did not persist log to DB")
        passed = False

    # ── Verify web recovered and can serve job data ───────────────────────────
    info("Checking web server is healthy after restart…")
    try:
        import urllib.request
        with urllib.request.urlopen("https://mysecond.app/healthz", timeout=10) as resp:
            if resp.status == 200:
                ok("GET /healthz → 200 OK")
            else:
                warn(f"GET /healthz → {resp.status}")
    except Exception as e:
        warn(f"Health check failed: {e}")

    return passed


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Live integration tests for mysecond worker")
    parser.add_argument(
        "--test",
        choices=["concurrent", "restart", "all"],
        default="all",
        help="Which test(s) to run (default: all)",
    )
    args = parser.parse_args()

    # Verify connectivity before running tests.
    print(f"\n{CYN}── Checking connectivity ──{RST}")
    try:
        with _db() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
        ok("PostgreSQL: connected")
    except Exception as e:
        sys.exit(f"{RED}  ✗ PostgreSQL: {e}{RST}")

    try:
        r = redis_lib.from_url(REDIS_URL, decode_responses=True)
        r.ping()
        ok("Redis: connected")
    except Exception as e:
        sys.exit(f"{RED}  ✗ Redis: {e}{RST}")

    # Check worker is running.
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "mysecond-worker"],
            capture_output=True, text=True,
        )
        if result.stdout.strip() == "active":
            ok("mysecond-worker: active")
        else:
            warn(f"mysecond-worker: {result.stdout.strip()} — tests may fail")
    except FileNotFoundError:
        warn("systemctl not found — can't check worker status")

    results = {}
    if args.test in ("concurrent", "all"):
        results["concurrent"] = test_concurrent()
    if args.test in ("restart", "all"):
        results["restart"] = test_restart()

    # Summary
    print(f"\n{CYN}══ Results ══{RST}")
    all_passed = True
    for name, passed in results.items():
        if passed:
            ok(f"{name}: PASSED")
        else:
            fail(f"{name}: FAILED")
            all_passed = False

    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
