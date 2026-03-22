"""Persistent background job worker — run as mysecond-worker systemd service.

Picks up jobs from Redis (BLPOP "mysecond:jobs:queued"), runs them as
subprocesses, and streams log lines back via Redis pub/sub so gunicorn SSE
connections can forward them to browsers.  Job status and log_text are
persisted to PostgreSQL after every line so the state survives a worker crash.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup — must come before local imports.
# ---------------------------------------------------------------------------
_WEB  = Path(__file__).resolve().parent
_ROOT = _WEB.parent
sys.path.insert(0, str(_WEB))
sys.path.insert(0, str(_ROOT / "src"))

# Load .env before importing anything that reads env vars.
_env_file = _ROOT / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ[_k.strip()] = _v.strip()

import psycopg2
import psycopg2.pool
import psycopg2.extras
import redis as redis_lib

from runner import (
    build_fetch_argv,
    build_habits_argv,
    build_import_argv,
    build_repertoire_argv,
    build_search_argv,
    build_strategise_argv,
    build_train_bot_argv,
    build_featured_player_argv,
)

# ---------------------------------------------------------------------------
# Config / constants
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [worker] %(levelname)s %(message)s",
)
log = logging.getLogger("worker")

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://mysecond:mysecond@localhost:5432/mysecond",
)
if "connect_timeout" not in DATABASE_URL:
    DATABASE_URL += ("&" if "?" in DATABASE_URL else "?") + "connect_timeout=10"

REDIS_URL        = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
REDIS_QUEUE_KEY  = "mysecond:jobs:queued"
REDIS_LOG_PREFIX = "mysecond:job:"
REDIS_LOG_SUFFIX = ":log"

# Maximum simultaneous heavy-analysis jobs (search/habits/repertoire/strategise).
# Light jobs (fetch, import, train-bot) are never gated by this semaphore.
MAX_CONCURRENT = 10
HEAVY          = {"search", "habits", "strategise", "repertoire"}

_DATA_DIR    = _ROOT / "data"
_OUTPUT_DIR  = _DATA_DIR / "output"
_UPLOADS_DIR = _DATA_DIR / "uploads"
_PLAYERS_DIR = _DATA_DIR / "players"

# ---------------------------------------------------------------------------
# Globals (set in main())
# ---------------------------------------------------------------------------

_pool:      psycopg2.pool.ThreadedConnectionPool | None = None
_redis:     redis_lib.Redis | None                      = None
_semaphore: threading.Semaphore | None                  = None
_shutdown   = threading.Event()

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


@contextmanager
def _conn():
    conn = _pool.getconn()
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.putconn(conn)


def _recover_stale() -> None:
    """At startup mark any 'running' jobs as 'failed' (they died with the previous worker instance)."""
    now = datetime.now(tz=timezone.utc)
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET status='failed', finished_at=%s WHERE status='running' RETURNING id",
            (now,),
        )
        rows = cur.fetchall()
        conn.commit()
    if rows:
        log.info("Recovered %d stale running jobs → failed", len(rows))


def _seed_queue() -> None:
    """Push all 'queued' job IDs to Redis after a worker restart so they get picked up."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM jobs WHERE status='queued' ORDER BY started_at ASC")
        rows = cur.fetchall()
    for (job_id,) in rows:
        # RPUSH + BLPOP = FIFO: oldest job runs first.
        _redis.rpush(REDIS_QUEUE_KEY, str(job_id))
    if rows:
        log.info("Seeded %d queued jobs into Redis", len(rows))


def _write_log(job_id: str, lines: list[str]) -> None:
    """Flush current log lines to DB so the state survives a crash."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE jobs SET log_text=%s WHERE id=%s",
                ("\n".join(lines) if lines else None, job_id),
            )
            conn.commit()
    except Exception as exc:
        log.warning("Log flush failed for %s: %s", job_id, exc)


def _set_job_pid(job_id: str, pid: int) -> None:
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("UPDATE jobs SET pid=%s WHERE id=%s", (pid, job_id))
            conn.commit()
    except Exception as exc:
        log.warning("PID update failed for %s: %s", job_id, exc)


def _finish_job(job_id: str, status: str, exit_code: int) -> None:
    now = datetime.now(tz=timezone.utc)
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE jobs SET status=%s, exit_code=%s, finished_at=%s WHERE id=%s",
                (status, exit_code, now, job_id),
            )
            conn.commit()
    except Exception as exc:
        log.warning("Status update failed for %s: %s", job_id, exc)


def _get_db_status(job_id: str) -> str | None:
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT status FROM jobs WHERE id=%s", (job_id,))
            row = cur.fetchone()
        return row[0] if row else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# argv builder
# ---------------------------------------------------------------------------


def _build_argv(job_id: str, command: str, params: dict, out_path: str | None) -> list[str]:
    if command == "fetch":
        # out_path doubles as pgn_out for fetch jobs.
        return build_fetch_argv({**params, "pgn_out": out_path})
    elif command == "search":
        return build_search_argv(params, out_path)
    elif command == "habits":
        return build_habits_argv(params, out_path)
    elif command == "repertoire":
        return build_repertoire_argv(params, out_path)
    elif command == "strategise":
        return build_strategise_argv(params, out_path)
    elif command == "import":
        pgn_path = str(_UPLOADS_DIR / f"{job_id}.pgn")
        return build_import_argv(params, pgn_path)
    elif command == "train-bot":
        slug = params.get("featured_slug")
        if slug:
            white   = str(_PLAYERS_DIR / f"{slug}-white.json")
            black   = str(_PLAYERS_DIR / f"{slug}-black.json")
            profile = str(_PLAYERS_DIR / f"{slug}-profile.json")
            return build_featured_player_argv(
                params, out_path, white, black, profile,
                include_profile=params.get("regen_profile", True),
            )
        return build_train_bot_argv(params, out_path)
    raise ValueError(f"Unknown command: {command!r}")


# ---------------------------------------------------------------------------
# Post-completion hooks
# ---------------------------------------------------------------------------


def _handle_bot_complete(job_id: str, out_path: str | None, status: str) -> None:
    """Update the bots table when a train-bot job finishes."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT id FROM bots WHERE job_id=%s", (job_id,))
            row = cur.fetchone()
        if row is None:
            return
        bot_id = row[0]

        elo = None
        if status == "done" and out_path:
            try:
                with open(out_path, encoding="utf-8") as f:
                    elo = json.load(f).get("opponent_elo")
            except Exception:
                pass

        new_status = "ready" if status == "done" else "failed"
        with _conn() as conn, conn.cursor() as cur:
            if elo is not None:
                cur.execute(
                    "UPDATE bots SET status=%s, opponent_elo=%s WHERE id=%s",
                    (new_status, elo, bot_id),
                )
            else:
                cur.execute("UPDATE bots SET status=%s WHERE id=%s", (new_status, bot_id))
            conn.commit()
    except Exception as exc:
        log.warning("Bot update failed for job %s: %s", job_id, exc)


def _generate_player_description(
    slug: str, display_name: str, title: str | None, profile_path: str, fpm, *, force: bool = False
) -> None:
    """Generate an AI description for a featured player and store it."""
    try:
        import anthropic as _anthropic
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            return
        with open(profile_path, encoding="utf-8") as f:
            profile = json.load(f)
        sw = profile.get("style_white", {})
        sb = profile.get("style_black", {})
        ph = profile.get("phase_stats", {})
        top_white = [o["move_sequence"] for o in sw.get("top_openings", [])[:3]]
        top_black = [o["move_sequence"] for o in sb.get("top_openings", [])[:3]]
        title_str = f"{title} " if title else ""
        first_w   = sw.get("first_move_distribution", [])
        first_b   = sb.get("first_move_distribution", [])
        fm_w = f"1.{first_w[0]['san']} ({first_w[0]['pct']*100:.0f}%)" if first_w else "varied"
        fm_b = f"1...{first_b[0]['san']} ({first_b[0]['pct']*100:.0f}%)" if first_b else "varied"
        end_conv = ph.get("endgame_conversion_rate")
        end_str  = f"{end_conv:.0%}" if end_conv is not None else "unknown"
        prompt = (
            f"Write a compelling 3-4 sentence profile of chess player {title_str}{display_name} "
            f"for a chess enthusiast audience.\n\n"
            f"Use your own knowledge of {display_name}'s playing style, reputation, and signature "
            f"openings as the foundation. Then weave in the statistical details below to add "
            f"specificity and ground the profile in real data. If the stats seem to contradict "
            f"their known reputation, trust your knowledge — the dataset may be incomplete.\n\n"
            f"Statistical data ({profile.get('total_games', 0):,} games indexed):\n"
            f"- As White: {sw.get('avg_win_rate', 0):.0%} win rate, {sw.get('draw_rate', 0):.0%} draws, "
            f"most common first move {fm_w}\n"
            f"- Top White openings: {', '.join(top_white) or 'varied'}\n"
            f"- As Black: {sb.get('avg_win_rate', 0):.0%} win rate, most common first response {fm_b}\n"
            f"- Top Black openings: {', '.join(top_black) or 'varied'}\n"
            f"- Endgame conversion: {end_str} when reaching endgame "
            f"(endgame reach {ph.get('endgame_reach_rate', 0):.0%})\n\n"
            f"Be specific and vivid — mention actual opening names and move sequences. "
            f"Write only the description text, no headers or preamble."
        )
        client = _anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        description = msg.content[0].text.strip()
        if description:
            fpm.set_description(slug, description, force=force)
    except Exception as exc:
        log.warning("Description generation failed for %s: %s", slug, exc)


def _handle_featured_player_complete(
    job_id: str, slug: str, params: dict, out_path: str | None, status: str
) -> None:
    """Update featured_players table and generate AI description after training."""
    from featured_players import FeaturedPlayerManager
    fpm = FeaturedPlayerManager(DATABASE_URL)

    if status != "done":
        fpm.set_failed(slug)
        return

    elo = None
    if out_path:
        try:
            with open(out_path, encoding="utf-8") as f:
                elo = json.load(f).get("opponent_elo")
        except Exception:
            pass

    white   = str(_PLAYERS_DIR / f"{slug}-white.json")
    black   = str(_PLAYERS_DIR / f"{slug}-black.json")
    profile = str(_PLAYERS_DIR / f"{slug}-profile.json")

    fpm.set_ready(slug, elo, white, black, out_path, profile if Path(profile).exists() else None)

    player = fpm.get(slug)
    if player and Path(profile).exists() and params.get("regen_description", True):
        threading.Thread(
            target=_generate_player_description,
            args=(slug, player["display_name"], player.get("title"), profile, fpm),
            kwargs={"force": params.get("force_description", False)},
            daemon=True,
        ).start()


def _post_complete(
    job_id: str, command: str, params: dict, out_path: str | None, status: str, exit_code: int
) -> None:
    if command != "train-bot":
        return
    try:
        slug = params.get("featured_slug")
        if slug:
            _handle_featured_player_complete(job_id, slug, params, out_path, status)
        else:
            _handle_bot_complete(job_id, out_path, status)
    except Exception as exc:
        log.exception("Post-completion hook failed for %s: %s", job_id, exc)


# ---------------------------------------------------------------------------
# Job runner
# ---------------------------------------------------------------------------


def _run_job(job_id: str) -> None:
    """Claim and execute a single job in the calling thread."""
    # Atomically claim: only proceeds if the job is still 'queued'.
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE jobs SET status='running'
            WHERE id=%s AND status='queued'
            RETURNING command, params, out_path
            """,
            (job_id,),
        )
        row = cur.fetchone()
        conn.commit()

    if row is None:
        log.info("Job %s: not claimable (already running, done, or cancelled) — skipping", job_id)
        return

    command, params, out_path = row

    try:
        argv = _build_argv(job_id, command, params, out_path)
    except ValueError as exc:
        log.error("Job %s: cannot build argv: %s", job_id, exc)
        _finish_job(job_id, "failed", 1)
        _redis.publish(
            f"{REDIS_LOG_PREFIX}{job_id}{REDIS_LOG_SUFFIX}",
            json.dumps({"done": True, "status": "failed"}),
        )
        return

    log.info("Job %s: starting %s", job_id, command)
    is_heavy = command in HEAVY
    if is_heavy:
        _semaphore.acquire()

    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=str(_ROOT),
            preexec_fn=os.setsid,
        )
        _set_job_pid(job_id, proc.pid)

        log_lines: list[str] = []
        line_num  = 0
        cancelled = False

        for raw_line in proc.stdout:
            line = raw_line.rstrip("\n")
            log_lines.append(line)

            # Publish to Redis for live SSE streaming.
            _redis.publish(
                f"{REDIS_LOG_PREFIX}{job_id}{REDIS_LOG_SUFFIX}",
                json.dumps({"n": line_num, "text": line}),
            )
            line_num += 1

            # Persist to DB for replay / crash recovery.
            if line_num == 1 or line_num % 20 == 0:
                _write_log(job_id, log_lines)

            # Check for cancellation signal from Flask.
            if line_num % 20 == 0 and _get_db_status(job_id) == "cancelled":
                log.info("Job %s: cancellation detected — killing process group", job_id)
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except (ProcessLookupError, OSError):
                    pass
                cancelled = True
                break

        proc.wait()
        exit_code = proc.returncode

        # Final log flush.
        _write_log(job_id, log_lines)

        # Determine final status (respect an external cancellation even if exit_code is 0).
        if cancelled or _get_db_status(job_id) == "cancelled":
            status = "cancelled"
        elif exit_code == 0:
            status = "done"
        else:
            status = "failed"

        _finish_job(job_id, status, exit_code)
        log.info("Job %s: %s (exit %s)", job_id, status, exit_code)

        # Publish done sentinel so waiting SSE connections can close.
        _redis.publish(
            f"{REDIS_LOG_PREFIX}{job_id}{REDIS_LOG_SUFFIX}",
            json.dumps({"done": True, "status": status}),
        )

        _post_complete(job_id, command, params, out_path, status, exit_code)

    except Exception as exc:
        log.exception("Job %s: unexpected error: %s", job_id, exc)
        _finish_job(job_id, "failed", 1)
        _redis.publish(
            f"{REDIS_LOG_PREFIX}{job_id}{REDIS_LOG_SUFFIX}",
            json.dumps({"done": True, "status": "failed"}),
        )
    finally:
        if is_heavy:
            _semaphore.release()


# ---------------------------------------------------------------------------
# Dispatch loop
# ---------------------------------------------------------------------------


def _dispatch_loop() -> None:
    log.info("Worker ready, polling for jobs...")
    while not _shutdown.is_set():
        try:
            result = _redis.blpop(REDIS_QUEUE_KEY, timeout=2)
        except Exception as exc:
            log.warning("Redis BLPOP error: %s", exc)
            time.sleep(1)
            continue

        if result is None:
            continue

        # decode_responses=True → result is already (str, str)
        _, job_id = result
        log.info("Dispatching job %s", job_id)
        t = threading.Thread(
            target=_run_job, args=(job_id,), daemon=True, name=f"job-{job_id[:8]}"
        )
        t.start()


def _shutdown_handler(signum, frame) -> None:
    log.info("Shutdown signal received — stopping dispatch loop, waiting for active jobs...")
    _shutdown.set()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    global _pool, _redis, _semaphore

    _pool      = psycopg2.pool.ThreadedConnectionPool(2, 20, DATABASE_URL)
    _redis     = redis_lib.from_url(REDIS_URL, decode_responses=True)
    _semaphore = threading.Semaphore(MAX_CONCURRENT)

    signal.signal(signal.SIGTERM, _shutdown_handler)
    signal.signal(signal.SIGINT,  _shutdown_handler)

    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    _UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    _PLAYERS_DIR.mkdir(parents=True, exist_ok=True)

    _recover_stale()
    _seed_queue()
    _dispatch_loop()

    # After the loop exits (SIGTERM received), give running threads up to 120s to finish.
    log.info("Waiting up to 120s for active jobs to finish...")
    time.sleep(120)
    log.info("Worker shut down.")


if __name__ == "__main__":
    main()
