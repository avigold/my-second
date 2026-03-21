"""Flask web server for the mysecond chess novelty finder."""

from __future__ import annotations

import json
import os
import queue
import re
import signal
import threading
import urllib.request
import urllib.error
from pathlib import Path

import chess
from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    stream_with_context,
)

# ---------------------------------------------------------------------------
# Load .env before importing local modules so env vars are available at their
# module-level import time (e.g. GOOGLE_CLIENT_ID in auth.py).
# ---------------------------------------------------------------------------
_env_file = Path(__file__).resolve().parent.parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            _k = _k.strip()
            os.environ[_k.strip()] = _v.strip()

from auth import (
    chesscom_auth_url,
    chesscom_enabled,
    chesscom_handle_callback,
    get_current_user,
    google_auth_url,
    google_enabled,
    google_handle_callback,
    lichess_auth_url,
    lichess_enabled,
    lichess_handle_callback,
    login_required,
    set_session_user,
)

import backup as backup_module
from bots import BotManager
from featured_players import FeaturedPlayerManager
from habits_parser import parse_habits
from jobs import Job, JobRegistry
import maia_engine
from pgn_parser import parse_novelties
from repertoire_parser import parse_repertoire
from runner import build_fetch_argv, build_habits_argv, build_import_argv, build_repertoire_argv, build_search_argv, build_strategise_argv, build_train_bot_argv, build_featured_player_argv, launch_job, make_launch_fn
from jobs import JobQueue

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Project root: parent of this file's directory (web/).
REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"

OUTPUT_DIR = DATA_DIR / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
UPLOADS_DIR = DATA_DIR / "uploads"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or os.urandom(24)

# ---------------------------------------------------------------------------
# Wikipedia photo helper
# ---------------------------------------------------------------------------
def _fetch_wiki_photo(display_name: str) -> str | None:
    """Fetch a high-quality press photo from Wikipedia for the given name.

    Uses the REST summary endpoint, then upscales the thumbnail URL to 600 px
    wide so we get a crisp image without pulling the full original.
    Returns the photo URL, or None if not found.
    """
    name_encoded = display_name.replace(" ", "_")
    url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{name_encoded}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "mysecond/0.1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        thumb = data.get("thumbnail", {}).get("source")
        if thumb:
            # Replace whatever size prefix Wikipedia gave us with 1200px
            return re.sub(r"/\d+px-", "/1200px-", thumb)
    except Exception:
        pass
    return None

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://mysecond:mysecond@localhost:5432/mysecond",
)
# Ensure DB connection attempts fail fast rather than hanging indefinitely.
if "connect_timeout" not in DATABASE_URL:
    DATABASE_URL += ("&" if "?" in DATABASE_URL else "?") + "connect_timeout=10"

registry = JobRegistry(DATABASE_URL)
job_queue = JobQueue()
bot_manager = BotManager(DATABASE_URL)
featured_player_manager = FeaturedPlayerManager(DATABASE_URL)

PLAYERS_DIR = DATA_DIR / "players"
PLAYERS_DIR.mkdir(parents=True, exist_ok=True)


def _restore_featured_player_callbacks() -> None:
    """Re-attach completion callbacks to orphaned train-bot jobs after restart.

    _load_existing() starts _watch_orphan threads for running jobs, but those
    threads don't have a _completion_fn.  Without it, featured_player_manager
    .set_ready() is never called when the subprocess finishes.  This function
    iterates over all in-memory jobs, finds running train-bot jobs with a
    featured_slug, and registers the same callback that the launch route would
    have set.
    """
    with registry._lock:
        jobs_snapshot = list(registry._jobs.values())

    for job in jobs_snapshot:
        if job.command != "train-bot":
            continue
        slug = job.params.get("featured_slug")
        if not slug:
            continue

        player = featured_player_manager.get(slug)
        if player is None:
            continue

        out_path        = job.out_path or str(OUTPUT_DIR / f"{job.id}.json")
        white_book_path = str(PLAYERS_DIR / f"{slug}-white.json")
        black_book_path = str(PLAYERS_DIR / f"{slug}-black.json")
        profile_path    = str(PLAYERS_DIR / f"{slug}-profile.json")

        def _make_callback(_slug, _out, _white, _black, _profile, _player):
            def _on_complete(status: str, exit_code: int) -> None:
                if status == "done":
                    elo = None
                    try:
                        with open(_out, encoding="utf-8") as f:
                            model = json.load(f)
                        elo = model.get("opponent_elo")
                    except Exception:
                        pass
                    profile_exists = Path(_profile).exists()
                    featured_player_manager.set_ready(
                        _slug, elo, _white, _black, _out,
                        _profile if profile_exists else None,
                    )
                    if profile_exists:
                        threading.Thread(
                            target=_generate_player_description,
                            args=(_slug, _player["display_name"], _player.get("title"), _profile),
                            daemon=True,
                        ).start()
                else:
                    featured_player_manager.set_failed(_slug)
            return _on_complete

        if job.status == "running":
            registry.set_completion_callback(
                job.id,
                _make_callback(slug, out_path, white_book_path, black_book_path, profile_path, player),
            )


_restore_featured_player_callbacks()

# Opening-book cache for bot move lookup (same SQLite file as the CLI uses).
from mysecond.cache import Cache as _OpeningCache
_opening_cache = _OpeningCache(DATA_DIR / "cache.sqlite")

DIST_DIR = REPO_ROOT / "web" / "static" / "dist"

# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------

_AUTH_EXEMPT_PATHS = {"/", "/login", "/auth/logout", "/pricing", "/healthz", "/sitemap.xml", "/players"}
_AUTH_EXEMPT_PREFIXES = (
    "/auth/lichess", "/auth/chesscom", "/auth/google", "/static/",
    "/api/stripe/webhook",   # called by Stripe servers, no user session
    "/players/",
    "/api/players/",
)


@app.before_request
def require_login():
    path = request.path
    if path in _AUTH_EXEMPT_PATHS:
        return
    if any(path.startswith(p) for p in _AUTH_EXEMPT_PREFIXES):
        return
    if not get_current_user():
        if path.startswith("/api/"):
            return jsonify({"error": "unauthenticated"}), 401
        return redirect("/login")


@app.context_processor
def inject_current_user():
    user = get_current_user()
    plan = _effective_plan(user) if user else None
    return {"current_user": user, "current_plan": plan}


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------


@app.get("/healthz")
def healthz():
    """Lightweight health check — no DB, no auth. Used by monitoring and nginx."""
    return "ok", 200


@app.get("/sitemap.xml")
def sitemap():
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
        '  <url><loc>https://mysecond.app/</loc><changefreq>weekly</changefreq><priority>1.0</priority></url>',
        '  <url><loc>https://mysecond.app/pricing</loc><changefreq>monthly</changefreq><priority>0.8</priority></url>',
        '  <url><loc>https://mysecond.app/players</loc><changefreq>weekly</changefreq><priority>0.9</priority></url>',
    ]
    for p in featured_player_manager.list_all():
        if p["status"] == "ready":
            lines.append(
                f'  <url><loc>https://mysecond.app/players/{p["slug"]}</loc>'
                f'<changefreq>monthly</changefreq><priority>0.8</priority></url>'
            )
    lines.append('</urlset>')
    return Response("\n".join(lines), mimetype="application/xml")


def _safe_next() -> str:
    """Return the ?next= param if it's a safe relative path, else '/'."""
    nxt = request.args.get("next", "")
    if nxt and nxt.startswith("/") and not nxt.startswith("//"):
        return nxt
    return "/"


@app.get("/login")
def login_page():
    if get_current_user():
        return redirect(_safe_next() or "/")
    # Persist intended destination so the OAuth callback can redirect there.
    nxt = _safe_next()
    if nxt != "/":
        session["next_url"] = nxt
    return render_template(
        "login.html",
        lichess_ok=lichess_enabled(),
        chesscom_ok=chesscom_enabled(),
        google_ok=google_enabled(),
    )


@app.get("/auth/lichess")
def auth_lichess():
    return redirect(lichess_auth_url())


@app.get("/auth/lichess/callback")
def auth_lichess_callback():
    user = lichess_handle_callback(registry)
    if user is None:
        return "Authentication failed — please try again.", 400
    set_session_user(user)
    return redirect(session.pop("next_url", "/"))


@app.get("/auth/chesscom")
def auth_chesscom():
    return redirect(chesscom_auth_url())


@app.get("/auth/chesscom/callback")
def auth_chesscom_callback():
    user = chesscom_handle_callback(registry)
    if user is None:
        return "Authentication failed — please try again.", 400
    set_session_user(user)
    return redirect(session.pop("next_url", "/"))


@app.get("/auth/google")
def auth_google():
    return redirect(google_auth_url())


@app.get("/auth/google/callback")
def auth_google_callback():
    user = google_handle_callback(registry)
    if user is None:
        return "Authentication failed — please try again.", 400
    set_session_user(user)
    return redirect(session.pop("next_url", "/"))


@app.get("/auth/logout")
def auth_logout():
    session.clear()
    return redirect("/")


def _vite_tags() -> tuple[str, str]:
    """Return (<link> CSS tag, <script> JS tag) for the Vite-built bundle.

    Globs the assets/ directory for the hashed filenames rather than
    requiring a manifest (manifest generation needs an extra Vite flag).
    """
    assets = DIST_DIR / "assets"
    if not assets.exists():
        return (
            "<!-- Vite build not found: run bash scripts/build_web.sh -->",
            "",
        )
    base = "/static/dist/assets/"
    css_files = sorted(assets.glob("*.css"))
    js_files  = sorted(assets.glob("*.js"))
    css_tag = "\n".join(f'<link rel="stylesheet" href="{base}{f.name}">' for f in css_files)
    js_tag  = "\n".join(f'<script type="module" src="{base}{f.name}"></script>' for f in js_files)
    return css_tag, js_tag


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------


@app.get("/")
def index():
    players = _get_featured_players(limit=4)
    if get_current_user():
        return render_template("dashboard.html", featured_players=players)
    return render_template("landing.html", featured_players=players)


@app.get("/jobs")
def jobs_page():
    return render_template("index.html")


@app.get("/fetch")
def fetch_page():
    return render_template("fetch.html")


@app.get("/search")
def search_page():
    return render_template("search.html")


@app.get("/jobs/<job_id>")
def job_page(job_id: str):
    job = registry.get(job_id)
    if job is None:
        return "Job not found", 404
    bot = bot_manager.get_by_job_id(job_id) if job.command == "train-bot" else None
    return render_template("job.html", job=job.to_dict(), bot=bot)


@app.get("/jobs/<job_id>/novelties")
def novelty_browser_page(job_id: str):
    job = registry.get(job_id)
    if job is None:
        return "Job not found", 404
    side = job.params.get("side", "white")

    # Read Vite manifest to get hashed asset filenames.
    css_tag, js_tag = _vite_tags()
    return render_template(
        "novelty_browser.html",
        job_id=job_id,
        side=side,
        css_tag=css_tag,
        js_tag=js_tag,
    )


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------


@app.post("/api/fetch")
def api_fetch():
    params = request.get_json(force=True)
    if not params.get("username") or not params.get("color"):
        return jsonify({"error": "username and color are required"}), 400
    params["username"] = params["username"].strip()

    user = get_current_user()
    if err := _check_user_job_limit(user): return err
    if err := _check_plan_limit(user, "fetch"): return err
    job = registry.create("fetch", params, out_path=None, user_id=user["id"] if user else None)
    # Set out_path to a UUID-named PGN so games can be browsed after fetch.
    pgn_out = str(OUTPUT_DIR / f"{job.id}.pgn")
    registry.set_out_path(job.id, pgn_out)  # persist immediately — orphan recovery needs this
    params["pgn_out"] = pgn_out     # picked up by build_fetch_argv → --out
    argv = build_fetch_argv(params)
    launch_job(job, argv, REPO_ROOT, registry)
    return jsonify({"job_id": job.id})


@app.post("/api/search")
def api_search():
    params = request.get_json(force=True)
    if not params.get("side"):
        return jsonify({"error": "side is required"}), 400

    user = get_current_user()
    if err := _check_user_job_limit(user): return err
    if err := _check_plan_limit(user, "search"): return err
    job = registry.create("search", params, user_id=user["id"] if user else None)
    out_path = str(OUTPUT_DIR / f"{job.id}.pgn")
    job.out_path = out_path
    registry.set_out_path(job.id, out_path)  # persist immediately — orphan recovery needs this

    argv = build_search_argv(params, out_path)
    job._launch_fn = make_launch_fn(job, argv, REPO_ROOT, registry)
    job_queue.enqueue(job, registry)
    return jsonify({"job_id": job.id})


@app.get("/api/dashboard")
def api_dashboard():
    """Aggregate job data for the dashboard visualisations."""
    user = get_current_user()
    all_jobs = registry.list_for_user(user["id"]) if user else []
    done_jobs = [j for j in all_jobs if j["status"] == "done"]

    # Job counts by command type
    job_counts: dict[str, int] = {}
    for j in done_jobs:
        job_counts[j["command"]] = job_counts.get(j["command"], 0) + 1

    # Unique users mentioned across all jobs
    users: set[str] = set()
    for j in all_jobs:
        p = j.get("params") or {}
        if p.get("username"):
            users.add(p["username"])

    stats = {
        "total_jobs":  len(all_jobs),
        "done_jobs":   len(done_jobs),
        "job_counts":  job_counts,
        "user_count":  len(users),
        "users":       sorted(users)[:10],
    }

    # Top habits + total count across all completed habits jobs
    top_habits: list = []
    total_habits = 0
    habits_jobs = [
        j for j in all_jobs
        if j["command"] == "habits" and j["status"] == "done" and j.get("out_path")
    ]
    for hj in habits_jobs:
        try:
            items = parse_habits(hj["out_path"])
            total_habits += len(items)
            if not top_habits:
                for h in items[:5]:
                    h["job_id"]   = hj["id"]
                    h["username"] = (hj.get("params") or {}).get("username", "")
                    h["color"]    = (hj.get("params") or {}).get("color", "white")
                top_habits = items[:5]
        except Exception:
            pass

    stats["total_habits"] = total_habits

    # Top novelties + total count across all completed search jobs
    top_novelties: list = []
    total_novelties = 0
    search_jobs = [
        j for j in all_jobs
        if j["command"] == "search" and j["status"] == "done" and j.get("out_path")
    ]
    for sj in search_jobs:
        try:
            root_fen = (sj.get("params") or {}).get("fen", chess.STARTING_FEN)
            side     = (sj.get("params") or {}).get("side", "white")
            items    = parse_novelties(sj["out_path"], root_fen, side)
            total_novelties += len(items)
            if not top_novelties:
                for n in items[:5]:
                    n["job_id"] = sj["id"]
                top_novelties = items[:5]
        except Exception:
            pass

    stats["total_novelties"] = total_novelties

    ready_bots = [
        b for b in bot_manager.list_for_user(user["id"])
        if b["status"] == "ready"
    ] if user else []

    return jsonify({
        "stats":          stats,
        "top_habits":     top_habits,
        "top_novelties":  top_novelties,
        "recent_jobs":    all_jobs[:6],
        "ready_bots":     ready_bots[:3],
    })


@app.get("/api/jobs")
def api_jobs():
    user = get_current_user()
    if not user:
        return jsonify([])
    return jsonify(registry.list_for_user(user["id"]))


@app.get("/api/usage")
def api_usage():
    """Return the current user's monthly usage and plan for metering on form pages."""
    user = get_current_user()
    if not user:
        return jsonify({"plan": "free", "usage": {}})
    plan = _effective_plan(user)
    usage = {
        cmd: {
            "used":  registry.count_monthly_jobs(user["id"], cmd),
            "limit": None if plan == "pro" else _FREE_LIMITS.get(cmd),
        }
        for cmd in _FREE_LIMITS
    }
    return jsonify({"plan": plan, "usage": usage})


@app.get("/api/validate-user")
def api_validate_user():
    """Check whether a username exists on Lichess or Chess.com.

    Returns {valid: true, username: "<canonical>"} on success or
    {valid: false, error: "<message>"} on failure.  Used by forms to
    give instant feedback before launching a job.
    """
    username = request.args.get("username", "").strip()
    platform = request.args.get("platform", "lichess").strip()
    if not username:
        return jsonify({"valid": False, "error": "Username is required"})

    try:
        if platform == "lichess":
            url = f"https://lichess.org/api/user/{username}"
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=6) as resp:
                data = json.loads(resp.read())
            return jsonify({"valid": True, "username": data.get("username", username)})

        elif platform == "chesscom":
            url = f"https://api.chess.com/pub/player/{username}"
            req = urllib.request.Request(
                url, headers={"User-Agent": "mysecond.app chess analysis"}
            )
            with urllib.request.urlopen(req, timeout=6) as resp:
                data = json.loads(resp.read())
            return jsonify({"valid": True, "username": data.get("username", username)})

        else:
            return jsonify({"valid": False, "error": "Unknown platform"})

    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            platform_label = "Lichess" if platform == "lichess" else "Chess.com"
            return jsonify({"valid": False, "error": f"'{username}' not found on {platform_label}"})
        return jsonify({"valid": False, "error": "Could not reach the chess platform — try again"})
    except Exception:
        return jsonify({"valid": False, "error": "Could not reach the chess platform — try again"})


@app.get("/api/jobs/<job_id>")
def api_job(job_id: str):
    job = registry.get(job_id)
    if job is None:
        return jsonify({"error": "not found"}), 404
    d = job.to_dict()
    if d["status"] == "queued":
        d["queue_position"] = job_queue.queue_position(job_id)
    return jsonify(d)


@app.get("/api/jobs/<job_id>/novelties")
def api_novelties(job_id: str):
    job = registry.get(job_id)
    if job is None:
        return jsonify({"error": "not found"}), 404
    if not job.out_path or not Path(job.out_path).exists():
        return jsonify([])
    root_fen = job.params.get("fen", chess.STARTING_FEN)
    side = job.params.get("side", "white")
    return jsonify(parse_novelties(job.out_path, root_fen, side))


@app.get("/api/jobs/<job_id>/stream")
def api_stream(job_id: str):
    """SSE endpoint that streams job stdout line-by-line."""
    job = registry.get(job_id)
    if job is None:
        return "Job not found", 404

    def _generate():
        # If the job is queued, tell the client to poll and wait.
        if job.status == "queued":
            yield "event: queued\ndata: \n\n"
            return
        # If the job is already finished, replay the stored lines then close.
        if job.status != "running":
            for line in job.log_lines:
                yield f"data: {json.dumps({'line': line})}\n\n"
            yield "event: done\ndata: \n\n"
            return

        # Live streaming while the job runs.
        while True:
            try:
                item = job.queue.get(timeout=5.0)
                if item is None:
                    # Sentinel: job finished.
                    yield "event: done\ndata: \n\n"
                    break
                yield f"data: {json.dumps({'line': item})}\n\n"
            except queue.Empty:
                # Keepalive comment to prevent proxy / browser timeout.
                yield ": keepalive\n\n"

    return Response(
        stream_with_context(_generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/jobs/<job_id>/download")
def api_download(job_id: str):
    job = registry.get(job_id)
    if job is None:
        return "Job not found", 404
    if not job.out_path or not Path(job.out_path).exists():
        return "Output file not available", 404
    return send_file(
        job.out_path,
        as_attachment=True,
        download_name=Path(job.out_path).name,
        mimetype="application/x-chess-pgn",
    )


def _check_user_job_limit(user: dict | None):
    """Return a 409 response if the user already has a running or queued job, else None."""
    if user and registry.has_running_job(user["id"]):
        return jsonify({
            "error": "You already have a job running or queued. "
                     "Wait for it to finish or cancel it before starting a new one."
        }), 409
    return None


def _kill_job_process(job) -> None:
    """Send SIGTERM to the entire process group so child processes
    (e.g. Stockfish workers) are also terminated, not just the CLI process."""
    if job.process is None:
        return
    try:
        os.killpg(os.getpgid(job.process.pid), signal.SIGTERM)
    except (ProcessLookupError, OSError):
        pass  # already exited


# ---------------------------------------------------------------------------
# Plan / freemium helpers
# ---------------------------------------------------------------------------

_FREE_LIMITS: dict[str, int] = {"fetch": 5, "import": 5, "search": 3, "habits": 3, "repertoire": 3, "strategise": 1, "train-bot": 1}
_PRO_ONLY: set[str] = set()


_TITLED_ROLES = {"GM", "WGM", "IM", "WIM", "FM", "WFM", "CM", "WCM", "NM"}


def _effective_plan(user: dict) -> str:
    """Admins and titled players always get pro access; otherwise look up subscriptions."""
    if user.get("role") in {"admin"} | _TITLED_ROLES:
        return "pro"
    return registry.get_user_plan(user["id"])


def _check_plan_limit(user: dict | None, command: str):
    """Return a 402 response if the user has hit their plan limit, else None."""
    if user is None:
        return None  # auth gate already handled unauthenticated users
    plan = _effective_plan(user)
    if command in _PRO_ONLY and plan != "pro":
        return jsonify({
            "error": "Strategise is a Pro feature.",
            "upgrade_url": "/pricing",
        }), 402
    if command in _FREE_LIMITS and plan != "pro":
        limit = _FREE_LIMITS[command]
        used  = registry.count_monthly_jobs(user["id"], command)
        if used >= limit:
            return jsonify({
                "error": (
                    f"Free plan: {used}/{limit} {command} analyses used this month. "
                    "Upgrade to Pro for unlimited access."
                ),
                "upgrade_url": "/pricing",
            }), 402
    return None


@app.post("/api/jobs/<job_id>/cancel")
def api_cancel_job(job_id: str):
    user = get_current_user()
    job = registry.get(job_id)
    if job is None:
        return jsonify({"error": "not found"}), 404
    if user and user.get("role") != "admin" and job.user_id != user["id"]:
        return jsonify({"error": "forbidden"}), 403
    if job.status not in ("running", "queued"):
        return jsonify({"error": "job not running or queued"}), 400
    # Mark cancelled before killing so the reader thread preserves the status.
    registry.mark_cancelled(job_id)
    job_queue.remove(job_id)   # no-op if not in queue; removes if queued
    _kill_job_process(job)     # no-op if no process yet (queued)
    return jsonify({"status": "cancelled"})


@app.delete("/api/jobs/<job_id>")
def api_delete_job(job_id: str):
    job = registry.get(job_id)
    if job is None:
        return jsonify({"error": "not found"}), 404
    # Kill the whole process group if still running; dequeue if queued.
    if job.status in ("running", "queued"):
        registry.mark_cancelled(job_id)
        job_queue.remove(job_id)
        _kill_job_process(job)
    # Remove output file if present.
    if job.out_path:
        try:
            Path(job.out_path).unlink(missing_ok=True)
        except OSError:
            pass
    # Remove uploaded PGN for import jobs.
    upload_path = UPLOADS_DIR / f"{job_id}.pgn"
    if upload_path.exists():
        try:
            upload_path.unlink()
        except OSError:
            pass
    registry.delete(job_id)
    return jsonify({"status": "deleted"})


@app.get("/repertoire")
def repertoire_page():
    return render_template("repertoire.html")


@app.post("/api/repertoire")
def api_repertoire():
    params = request.get_json(force=True)
    if not params.get("username") or not params.get("color"):
        return jsonify({"error": "username and color are required"}), 400
    params["username"] = params["username"].strip()

    user = get_current_user()
    if err := _check_user_job_limit(user): return err
    if err := _check_plan_limit(user, "repertoire"): return err
    job = registry.create("repertoire", params, user_id=user["id"] if user else None)
    out_path = str(OUTPUT_DIR / f"{job.id}.pgn")
    job.out_path = out_path
    registry.set_out_path(job.id, out_path)  # persist immediately — orphan recovery needs this

    argv = build_repertoire_argv(params, out_path)
    job._launch_fn = make_launch_fn(job, argv, REPO_ROOT, registry)
    job_queue.enqueue(job, registry)
    return jsonify({"job_id": job.id})


@app.get("/api/jobs/<job_id>/repertoire")
def api_repertoire_data(job_id: str):
    job = registry.get(job_id)
    if job is None:
        return jsonify({"error": "not found"}), 404
    if not job.out_path or not Path(job.out_path).exists():
        return jsonify({})
    color = job.params.get("color", "white")
    return jsonify(parse_repertoire(job.out_path, color))


@app.get("/jobs/<job_id>/repertoire-browser")
def repertoire_browser_page(job_id: str):
    job = registry.get(job_id)
    if job is None:
        return "Job not found", 404
    side = job.params.get("color", "white")
    css_tag, js_tag = _vite_tags()
    return render_template(
        "repertoire_browser.html",
        job_id=job_id,
        side=side,
        css_tag=css_tag,
        js_tag=js_tag,
    )


@app.get("/habits")
def habits_page():
    return render_template("habits.html")


@app.get("/jobs/<job_id>/habits-practice")
def habits_practice_page(job_id: str):
    job = registry.get(job_id)
    if job is None:
        return "Job not found", 404
    side = job.params.get("color", "white")
    css_tag, js_tag = _vite_tags()
    return render_template(
        "habits_practice.html",
        job_id=job_id,
        side=side,
        css_tag=css_tag,
        js_tag=js_tag,
    )


@app.get("/jobs/<job_id>/habits-browser")
def habits_browser_page(job_id: str):
    job = registry.get(job_id)
    if job is None:
        return "Job not found", 404
    side = job.params.get("color", "white")
    css_tag, js_tag = _vite_tags()
    return render_template(
        "habits_browser.html",
        job_id=job_id,
        side=side,
        css_tag=css_tag,
        js_tag=js_tag,
    )


@app.get("/strategise")
def strategise_page():
    return render_template("strategise.html")


@app.get("/jobs/<job_id>/strategise-report")
def strategise_report_page(job_id: str):
    job = registry.get(job_id)
    if job is None:
        return "Job not found", 404
    side = job.params.get("player_color", "white")
    css_tag, js_tag = _vite_tags()
    return render_template(
        "strategise_report.html",
        job_id=job_id,
        side=side,
        css_tag=css_tag,
        js_tag=js_tag,
    )


@app.get("/jobs/<job_id>/games")
def game_analysis_page(job_id: str):
    job = registry.get(job_id)
    if job is None or job.command not in ("fetch", "import"):
        return "Not found", 404
    side = job.params.get("color", "white")
    css_tag, js_tag = _vite_tags()
    return render_template(
        "game_analysis.html",
        job=job.to_dict(),
        job_id=job_id,
        side=side,
        css_tag=css_tag,
        js_tag=js_tag,
    )


@app.get("/pricing")
def pricing_page():
    return render_template("pricing.html")


# ---------------------------------------------------------------------------
def _get_featured_players(limit=None):
    """Return enriched featured player dicts (status=ready). Used by landing + dashboard."""
    players_raw = [p for p in featured_player_manager.list_all() if p["status"] == "ready"]
    if limit:
        players_raw = players_raw[:limit]
    players = []
    for p in players_raw:
        pdata = dict(p)
        pdata["avatar_url"] = None
        if not pdata.get("photo_url"):
            profile_path = pdata.get("profile_json_path")
            if profile_path:
                try:
                    profile_file = Path(profile_path)
                    profile_data = json.loads(profile_file.read_text())
                    pdata["avatar_url"] = profile_data.get("avatar_url")
                    cached = profile_data.get("photo_url")
                    if cached:
                        pdata["photo_url"] = cached
                    else:
                        wiki = _fetch_wiki_photo(pdata["display_name"])
                        if wiki:
                            pdata["photo_url"] = wiki
                            profile_data["photo_url"] = wiki
                            profile_file.write_text(json.dumps(profile_data, indent=2))
                except Exception:
                    pass
        players.append(pdata)
    return players


# Public featured-player routes (no auth required)
# ---------------------------------------------------------------------------


@app.get("/players")
def players_page():
    players = _get_featured_players()
    return render_template("players.html", players=players, logged_in=bool(get_current_user()))


@app.get("/players/<slug>")
def player_profile_page(slug: str):
    player = featured_player_manager.get(slug)
    if player is None or player["status"] != "ready":
        return "Player not found", 404
    css_tag, js_tag = _vite_tags()
    return render_template(
        "player_profile.html",
        player=player,
        logged_in=bool(get_current_user()),
        css_tag=css_tag,
        js_tag=js_tag,
    )


@app.get("/api/players")
def api_players_list():
    players = [p for p in featured_player_manager.list_all() if p["status"] == "ready"]
    return jsonify(players)


@app.get("/api/players/<slug>/book/<color>")
def api_player_book(slug: str, color: str):
    if color not in ("white", "black"):
        return jsonify({"error": "color must be white or black"}), 400
    player = featured_player_manager.get(slug)
    if player is None or player["status"] != "ready":
        return jsonify({"error": "player not found"}), 404
    book_path = player.get(f"{color}_book_path")
    if not book_path or not Path(book_path).exists():
        return jsonify({"positions": {}})
    return send_file(book_path, mimetype="application/json")


@app.get("/api/players/<slug>/repertoire/<color>")
def api_player_repertoire(slug: str, color: str):
    """Convert a player's opening book into a navigable repertoire tree."""
    if color not in ("white", "black"):
        return jsonify({"error": "color must be white or black"}), 400
    player = featured_player_manager.get(slug)
    if player is None or player["status"] != "ready":
        return jsonify({"error": "player not found"}), 404
    book_path = player.get(f"{color}_book_path")
    if not book_path or not Path(book_path).exists():
        return jsonify({"error": "book not found"}), 404

    with open(book_path, encoding="utf-8") as f:
        book = json.load(f)
    positions = book.get("positions", {})

    import chess as _chess
    from collections import deque as _deque

    player_color = _chess.WHITE if color == "white" else _chess.BLACK

    def _nfen(b):
        p = b.fen().split()
        return f"{p[0]} {p[1]} {p[2]} -"

    # BFS build tree
    PLAYER_MAX  = 4   # top N player moves to expand
    OPP_MAX     = 3   # top N opponent moves to expand
    MAX_NODES   = 600
    MAX_DEPTH   = 16

    start_board = _chess.Board()
    root_nfen   = _nfen(start_board)

    root_node = {
        "id": "0", "parent_id": None,
        "fen": start_board.fen(),
        "move_san": None, "move_uci": None, "move_orig": None, "move_dest": None,
        "comment": "", "freq": None,
        "depth": 0, "is_player_move": False,
        "children": [],
    }

    bfs_queue = _deque([(start_board.copy(), root_node, 0)])
    total_nodes = 1

    while bfs_queue and total_nodes < MAX_NODES:
        board, parent_node, depth = bfs_queue.popleft()
        if depth >= MAX_DEPTH:
            continue

        nfen           = _nfen(board)
        is_player_turn = board.turn == player_color
        book_moves     = positions.get(nfen, [])

        if book_moves:
            # Use top-N moves from the book.
            top_n     = PLAYER_MAX if is_player_turn else OPP_MAX
            top_moves = [{"uci": m["uci"], "games": m.get("games", 0)}
                         for m in sorted(book_moves, key=lambda m: -m.get("games", 0))[:top_n]]
            # Total games at this position across ALL moves (for frequency %).
            position_total = sum(m.get("games", 0) for m in book_moves)
        elif not is_player_turn:
            # Opponent's turn and position not in book (e.g. starting FEN for
            # Black's repertoire).  Probe all legal moves and keep those whose
            # resulting position IS in the book, sorted by book game count.
            candidates = []
            for opp_move in board.legal_moves:
                b2 = board.copy()
                b2.push(opp_move)
                sub = positions.get(_nfen(b2), [])
                if sub:
                    total = sum(s.get("games", 0) for s in sub)
                    candidates.append({"uci": opp_move.uci(), "games": total})
            top_moves = sorted(candidates, key=lambda m: -m["games"])[:OPP_MAX]
            position_total = sum(m["games"] for m in candidates)
        else:
            continue  # player's turn but no book data — dead end

        for i, m in enumerate(top_moves):
            uci = m.get("uci", "")
            if not uci:
                continue
            try:
                move  = _chess.Move.from_uci(uci)
                san   = board.san(move)
                after = board.copy()
                after.push(move)
            except Exception:
                continue

            games    = m.get("games", 0)
            child_id = f"{parent_node['id']}.{i}"
            pct = round(games / position_total * 100) if position_total else 0
            child_node = {
                "id": child_id, "parent_id": parent_node["id"],
                "fen": after.fen(),
                "move_san": san, "move_uci": uci,
                "move_orig": uci[:2], "move_dest": uci[2:4],
                "comment": f"{games:,} games" if games else "",
                "freq": {"games": games, "total": position_total, "pct": pct,
                         "wins": None, "draws": None, "losses": None} if is_player_turn else None,
                "depth": depth + 1,
                "is_player_move": is_player_turn,
                "children": [],
            }
            parent_node["children"].append(child_node)
            total_nodes += 1
            bfs_queue.append((after, child_node, depth + 1))

    return jsonify({"root": root_node, "color": color, "tree_stats": {
        "total_positions": total_nodes,
        "player_moves": 0, "opponent_moves": 0,
        "leaf_count": 0, "max_depth": MAX_DEPTH,
        "quality_by_depth": {},
    }})


@app.post("/api/players/<slug>/engine-move")
def api_player_engine_move(slug: str):
    """Return Maia's move for the given FEN (out-of-book positions)."""
    player = featured_player_manager.get(slug)
    if player is None or player["status"] != "ready":
        return jsonify({"error": "player not found"}), 404
    data = request.get_json(force=True)
    fen = (data.get("fen") or "").strip()
    if not fen:
        return jsonify({"error": "fen is required"}), 400
    try:
        elo = player.get("elo") or 1500
        uci = maia_engine.get_move(fen, elo)
        if uci is None:
            return jsonify({"error": "engine returned no move"}), 500
        return jsonify({"uci": uci, "source": "engine"})
    except Exception as exc:
        return jsonify({"error": f"Engine error: {exc}"}), 500


@app.get("/api/players/<slug>/profile")
def api_player_profile(slug: str):
    player = featured_player_manager.get(slug)
    if player is None or player["status"] != "ready":
        return jsonify({"error": "player not found"}), 404
    profile_path = player.get("profile_json_path")
    if not profile_path or not Path(profile_path).exists():
        return jsonify({"error": "profile not yet generated"}), 404
    return send_file(profile_path, mimetype="application/json")


@app.get("/account")
def account_page():
    user = get_current_user()
    plan  = _effective_plan(user)
    sub   = registry.get_subscription(user["id"])
    usage = {
        cmd: {
            "used":  registry.count_monthly_jobs(user["id"], cmd),
            "limit": None if plan == "pro" else _FREE_LIMITS.get(cmd),
        }
        for cmd in ("search", "habits", "repertoire", "strategise")
    }
    stripe_ok  = bool(os.environ.get("STRIPE_SECRET_KEY"))
    return render_template(
        "account.html",
        user=user,
        plan=plan,
        sub=sub,
        usage=usage,
        stripe_ok=stripe_ok,
    )


# ---------------------------------------------------------------------------
# Stripe endpoints
# ---------------------------------------------------------------------------


def _stripe_client():
    import stripe as _s
    _s.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
    return _s


@app.post("/api/stripe/create-checkout-session")
def stripe_create_checkout():
    sk = os.environ.get("STRIPE_SECRET_KEY")
    price_id = os.environ.get("STRIPE_PRICE_ID")
    if not sk or not price_id:
        return jsonify({"error": "Stripe not configured"}), 501

    user = get_current_user()
    s = _stripe_client()

    # Retrieve or create Stripe customer.
    customer_id = registry.get_stripe_customer_id(user["id"])
    if not customer_id:
        customer = s.Customer.create(
            metadata={"user_id": user["id"], "username": user["username"]},
        )
        customer_id = customer.id
        # Persist immediately so webhooks can map customer → user.
        registry.upsert_subscription(
            user_id=user["id"],
            stripe_customer_id=customer_id,
            stripe_subscription_id=None,
            plan="free",
            status="pending",
            current_period_end=None,
        )

    base = request.host_url.rstrip("/")
    session = s.checkout.Session.create(
        customer=customer_id,
        client_reference_id=user["id"],
        payment_method_types=["card"],
        line_items=[{"price": price_id, "quantity": 1}],
        mode="subscription",
        success_url=f"{base}/account?stripe=success",
        cancel_url=f"{base}/account",
    )
    return jsonify({"url": session.url})


@app.post("/api/stripe/create-portal-session")
def stripe_create_portal():
    sk = os.environ.get("STRIPE_SECRET_KEY")
    if not sk:
        return jsonify({"error": "Stripe not configured"}), 501

    user = get_current_user()
    customer_id = registry.get_stripe_customer_id(user["id"])
    if not customer_id:
        return jsonify({"error": "No billing record found"}), 404

    s = _stripe_client()
    base = request.host_url.rstrip("/")
    try:
        portal = s.billing_portal.Session.create(
            customer=customer_id,
            return_url=f"{base}/account",
        )
    except s.error.InvalidRequestError as e:
        return jsonify({"error": str(e.user_message or e)}), 400
    except s.error.StripeError as e:
        return jsonify({"error": str(e.user_message or e)}), 502
    return jsonify({"url": portal.url})


@app.post("/api/stripe/webhook")
def stripe_webhook():
    sk = os.environ.get("STRIPE_SECRET_KEY")
    webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET")
    if not sk:
        return "Stripe not configured", 501

    s = _stripe_client()
    payload    = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")

    try:
        event = s.Webhook.construct_event(payload, sig_header, webhook_secret)
    except ValueError:
        return "Invalid payload", 400
    except s.error.SignatureVerificationError:
        return "Invalid signature", 400

    _handle_stripe_event(event)
    return jsonify({"received": True})


def _handle_stripe_event(event: dict) -> None:
    from datetime import timezone as _tz
    etype = event["type"]
    obj   = event["data"]["object"]

    if etype == "checkout.session.completed":
        user_id     = obj.get("client_reference_id")
        customer_id = obj.get("customer")
        sub_id      = obj.get("subscription")
        if user_id and customer_id:
            registry.upsert_subscription(
                user_id=user_id,
                stripe_customer_id=customer_id,
                stripe_subscription_id=sub_id,
                plan="pro",
                status="active",
                current_period_end=None,
            )

    elif etype in (
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    ):
        sub         = obj
        customer_id = sub["customer"]
        sub_id      = sub["id"]
        status      = sub["status"]
        plan        = "pro" if (
            status in ("active", "trialing")
            and etype != "customer.subscription.deleted"
        ) else "free"
        period_ts   = sub.get("current_period_end")
        period_end  = (
            datetime.fromtimestamp(period_ts, tz=_tz.utc) if period_ts else None
        )
        user_id = registry.get_user_id_by_stripe_customer(customer_id)
        if user_id:
            registry.upsert_subscription(
                user_id=user_id,
                stripe_customer_id=customer_id,
                stripe_subscription_id=sub_id,
                plan=plan,
                status=status,
                current_period_end=period_end,
            )

    elif etype == "invoice.payment_failed":
        customer_id = obj["customer"]
        sub_id      = obj.get("subscription")
        user_id     = registry.get_user_id_by_stripe_customer(customer_id)
        if user_id:
            registry.upsert_subscription(
                user_id=user_id,
                stripe_customer_id=customer_id,
                stripe_subscription_id=sub_id,
                plan="free",
                status="past_due",
                current_period_end=None,
            )


@app.get("/import-pgn")
def import_pgn_page():
    return render_template("import.html")


@app.post("/api/habits")
def api_habits():
    params = request.get_json(force=True)
    if not params.get("username") or not params.get("color"):
        return jsonify({"error": "username and color are required"}), 400
    params["username"] = params["username"].strip()

    user = get_current_user()
    if err := _check_user_job_limit(user): return err
    if err := _check_plan_limit(user, "habits"): return err
    job = registry.create("habits", params, user_id=user["id"] if user else None)
    out_path = str(OUTPUT_DIR / f"{job.id}.pgn")
    job.out_path = out_path
    registry.set_out_path(job.id, out_path)  # persist immediately — orphan recovery needs this

    argv = build_habits_argv(params, out_path)
    job._launch_fn = make_launch_fn(job, argv, REPO_ROOT, registry)
    job_queue.enqueue(job, registry)
    return jsonify({"job_id": job.id})


@app.get("/api/jobs/<job_id>/habits")
def api_habits_data(job_id: str):
    job = registry.get(job_id)
    if job is None:
        return jsonify({"error": "not found"}), 404
    if not job.out_path or not Path(job.out_path).exists():
        return jsonify([])
    return jsonify(parse_habits(job.out_path))


@app.post("/api/strategise")
def api_strategise():
    params = request.get_json(force=True)
    if not params.get("player") or not params.get("player_color") or not params.get("opponent"):
        return jsonify({"error": "player, player_color, and opponent are required"}), 400
    params["player"]   = params["player"].strip()
    params["opponent"] = params["opponent"].strip()

    # Never store the API key in params — the CLI subprocess reads it from
    # ANTHROPIC_API_KEY which it inherits from the gunicorn process environment.
    params.pop("api_key", None)

    user = get_current_user()
    if err := _check_user_job_limit(user): return err
    if err := _check_plan_limit(user, "strategise"): return err
    job = registry.create("strategise", params, user_id=user["id"] if user else None)
    out_path = str(OUTPUT_DIR / f"{job.id}.json")
    job.out_path = out_path
    registry.set_out_path(job.id, out_path)  # persist immediately — orphan recovery needs this

    argv = build_strategise_argv(params, out_path)
    job._launch_fn = make_launch_fn(job, argv, REPO_ROOT, registry)
    job_queue.enqueue(job, registry)
    return jsonify({"job_id": job.id})


@app.get("/api/jobs/<job_id>/strategise")
def api_strategise_data(job_id: str):
    job = registry.get(job_id)
    if job is None:
        return jsonify({"error": "not found"}), 404
    if not job.out_path or not Path(job.out_path).exists():
        return jsonify({})
    import json as _json
    with open(job.out_path, encoding="utf-8") as f:
        return jsonify(_json.load(f))


@app.get("/api/jobs/<job_id>/pgn-games")
def api_pgn_games_list(job_id: str):
    """Return a paginated, filtered list of games from the job's PGN file."""
    import io as _io
    job = registry.get(job_id)
    if job is None:
        return jsonify({"error": "not found"}), 404
    if not job.out_path or not Path(job.out_path).exists():
        return jsonify({"error": "PGN file not available"}), 404

    page      = max(1, int(request.args.get("page", 1)))
    per_page  = min(50, max(1, int(request.args.get("per_page", 20))))
    q         = request.args.get("q", "").strip().lower()
    result_f  = request.args.get("result", "all")   # all|win|draw|loss

    player_username = job.params.get("username", "").lower()
    job_color       = job.params.get("color", "white")   # fallback only

    pgn_text = Path(job.out_path).read_text(encoding="utf-8", errors="replace")
    buf = _io.StringIO(pgn_text)

    all_games = []
    idx = 0
    while True:
        headers = chess.pgn.read_headers(buf)
        if headers is None:
            break
        white  = headers.get("White", "?")
        black  = headers.get("Black", "?")
        result = headers.get("Result", "*")

        # Determine the player's actual colour in this game by username match.
        if player_username and player_username == white.lower():
            player_color = "white"
        elif player_username and player_username == black.lower():
            player_color = "black"
        else:
            player_color = job_color

        # Derive player result from the player's actual colour.
        if result == "1/2-1/2":
            player_result = "draw"
        elif (result == "1-0" and player_color == "white") or \
             (result == "0-1" and player_color == "black"):
            player_result = "win"
        elif result in ("1-0", "0-1"):
            player_result = "loss"
        else:
            player_result = "unknown"

        opponent = black if player_color == "white" else white

        all_games.append({
            "index":         idx,
            "white":         white,
            "black":         black,
            "result":        result,
            "player_result": player_result,
            "opponent":      opponent,
            "date":          headers.get("Date", ""),
            "time_control":  headers.get("TimeControl", ""),
            "eco":           headers.get("ECO", ""),
            "opening":       headers.get("Opening", headers.get("Variant", "")),
        })
        idx += 1

    # Apply filters.
    filtered = all_games
    if q:
        filtered = [g for g in filtered if q in g["opponent"].lower()]
    if result_f in ("win", "draw", "loss"):
        filtered = [g for g in filtered if g["player_result"] == result_f]

    total  = len(filtered)
    start  = (page - 1) * per_page
    paged  = filtered[start : start + per_page]

    return jsonify({
        "total":        total,
        "page":         page,
        "per_page":     per_page,
        "player_color": player_color,
        "games":        paged,
    })


@app.get("/api/jobs/<job_id>/pgn-games/<int:index>")
def api_pgn_game_detail(job_id: str, index: int):
    """Return full move list for game #index in the job's PGN file."""
    import io as _io
    job = registry.get(job_id)
    if job is None:
        return jsonify({"error": "not found"}), 404
    if not job.out_path or not Path(job.out_path).exists():
        return jsonify({"error": "PGN file not available"}), 404

    username = (job.params.get("username") or "").lower()
    pgn_text = Path(job.out_path).read_text(encoding="utf-8", errors="replace")
    buf = _io.StringIO(pgn_text)

    # Skip to game #index.
    for _ in range(index):
        if chess.pgn.read_headers(buf) is None:
            return jsonify({"error": "index out of range"}), 404

    game = chess.pgn.read_game(buf)
    if game is None:
        return jsonify({"error": "index out of range"}), 404

    headers = {k: v for k, v in game.headers.items()}

    # Determine the player's actual colour in this specific game by matching
    # the username against the White/Black header (case-insensitive).
    white_name = headers.get("White", "").lower()
    black_name = headers.get("Black", "").lower()
    if username and username == white_name:
        player_color = "white"
    elif username and username == black_name:
        player_color = "black"
    else:
        player_color = job.params.get("color", "white")

    # Walk mainline and collect per-ply data.
    board = game.board()
    moves = [{"fen": board.fen(), "san": None, "uci": None,
               "move_number": 0, "color": None}]  # starting position

    for node in game.mainline():
        move       = node.move
        san        = board.san(move)
        uci        = move.uci()
        move_num   = board.fullmove_number
        color      = "white" if board.turn == chess.WHITE else "black"
        board.push(move)
        moves.append({
            "fen":         board.fen(),
            "san":         san,
            "uci":         uci,
            "move_number": move_num,
            "color":       color,
        })

    return jsonify({
        "headers":      headers,
        "player_color": player_color,
        "moves":        moves,
    })


@app.get("/bots")
def bots_page():
    user = get_current_user()
    bots = bot_manager.list_for_user(user["id"]) if user else []
    # Attach training job status for "training" bots.
    for b in bots:
        if b["status"] == "training" and b.get("job_id"):
            job = registry.get(b["job_id"])
            b["job_status"] = job.status if job else "unknown"
        else:
            b["job_status"] = b["status"]
    return render_template("bots.html", bots=bots)


@app.get("/bots/<bot_id>/practice")
def bot_practice_page(bot_id: str):
    bot = bot_manager.get(bot_id)
    if bot is None:
        return "Bot not found", 404
    user = get_current_user()
    if user and bot["user_id"] != user["id"] and user.get("role") != "admin":
        return "Forbidden", 403
    css_tag, js_tag = _vite_tags()
    return render_template(
        "bot_practice.html",
        bot=bot,
        css_tag=css_tag,
        js_tag=js_tag,
    )


@app.get("/api/bots")
def api_bots_list():
    user = get_current_user()
    return jsonify(bot_manager.list_for_user(user["id"]) if user else [])


@app.get("/api/bots/<bot_id>")
def api_bot_detail(bot_id: str):
    bot = bot_manager.get(bot_id)
    if bot is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(bot)


@app.post("/api/bots")
def api_create_bot():
    params = request.get_json(force=True)
    opponent_username = (params.get("opponent_username") or "").strip()
    if not opponent_username:
        return jsonify({"error": "opponent_username is required"}), 400

    platform = params.get("platform", "lichess")
    speeds = params.get("speeds", "blitz,rapid,classical")
    color = params.get("color", "both")

    user = get_current_user()
    if err := _check_user_job_limit(user):
        return err
    if err := _check_plan_limit(user, "train-bot"):
        return err

    # Create the training job.
    job_params = {
        "opponent_username": opponent_username,
        "platform": platform,
        "speeds": speeds,
        "color": color,
    }
    job = registry.create("train-bot", job_params, user_id=user["id"] if user else None)
    out_path = str(OUTPUT_DIR / f"{job.id}.json")
    job.out_path = out_path
    registry.set_out_path(job.id, out_path)

    # Create the bot row (status = training).
    bot_id = bot_manager.create(
        user_id=user["id"] if user else None,
        opponent_username=opponent_username,
        platform=platform,
        speeds=speeds,
        color=color,
        job_id=job.id,
    )

    # Completion callback: update bot status when training finishes.
    def _on_complete(status: str, exit_code: int) -> None:
        if status == "done":
            # Try to read opponent_elo from the written model JSON.
            elo = None
            try:
                import json as _json
                with open(out_path, encoding="utf-8") as f:
                    model = _json.load(f)
                elo = model.get("opponent_elo")
            except Exception:
                pass
            bot_manager.set_status(bot_id, "ready", opponent_elo=elo)
        else:
            bot_manager.set_status(bot_id, "failed")

    argv = build_train_bot_argv(job_params, out_path)
    launch_job(job, argv, REPO_ROOT, registry, completion_callback=_on_complete)
    return jsonify({"bot_id": bot_id, "job_id": job.id})


@app.delete("/api/bots/<bot_id>")
def api_delete_bot(bot_id: str):
    user = get_current_user()
    if not user:
        return jsonify({"error": "login required"}), 401
    deleted = bot_manager.delete(bot_id, user["id"])
    if not deleted:
        return jsonify({"error": "bot not found"}), 404
    _bot_model_cache.pop(bot_id, None)
    return jsonify({"ok": True})


# In-memory cache for loaded bot models (bot_id → dict).
_bot_model_cache: dict[str, dict] = {}


def _load_bot_model(bot_id: str, job_id: str) -> dict | None:
    """Load and cache a bot model JSON from disk."""
    if bot_id in _bot_model_cache:
        return _bot_model_cache[bot_id]
    job = registry.get(job_id)
    if job is None or not job.out_path or not Path(job.out_path).exists():
        return None
    import json as _json
    with open(job.out_path, encoding="utf-8") as f:
        model = _json.load(f)
    _bot_model_cache[bot_id] = model
    return model


@app.post("/api/bots/<bot_id>/move")
def api_bot_move(bot_id: str):
    """Return the bot's move for the given FEN and color.

    Body: {"fen": "<FEN>", "color": "white"|"black"}
    Response: {"uci": "<UCI>", "source": "opening"|"habit"|"engine"}
    """
    import random

    data = request.get_json(force=True)
    fen = (data.get("fen") or "").strip()
    color = (data.get("color") or "white").strip()

    if not fen:
        return jsonify({"error": "fen is required"}), 400
    if color not in ("white", "black"):
        return jsonify({"error": "color must be white or black"}), 400

    bot = bot_manager.get(bot_id)
    if bot is None:
        return jsonify({"error": "bot not found"}), 404
    if bot["status"] != "ready":
        return jsonify({"error": "bot is not ready"}), 409

    model = _load_bot_model(bot_id, bot["job_id"])
    if model is None:
        return jsonify({"error": "bot model not available"}), 503

    # Build habits lookup: fen → {player_move_uci, games, total}
    habits_list = model.get(f"habits_{color}", [])
    habits_by_fen = {h["fen"]: h for h in habits_list}

    # ------------------------------------------------------------------
    # 1. Check opening cache
    # ------------------------------------------------------------------
    # Normalize the FEN by stripping the en passant field: chess.js v1+ omits
    # it when no pawn can capture, while Python's chess always includes it.
    # Cache keys are stored without ep (see fetcher._fen_cache_key).
    def _norm_lookup(f: str) -> str:
        parts = f.split(" ")
        return " ".join(parts[:3]) + " -"

    lookup_fen = _norm_lookup(fen)

    backend_key = model.get(f"cache_backend_{color}")
    if backend_key:
        cached = _opening_cache.get(lookup_fen, backend_key)

        if cached and cached.get("moves"):
            moves = cached["moves"]

            # Weighted-random move from the opening cache.
            # NOTE: do NOT inject habits here — the cache already reflects the
            # player's historical frequency (including rare/suboptimal choices).
            # Injecting on top would double-count those moves.
            #
            # Only consider moves with >= 10 games to avoid rare sidelines.
            # If nothing clears the bar, fall back to the single top move.
            _MIN_MOVE_GAMES = 10
            weighted_moves = [
                (m, m.get("white", 0) + m.get("draws", 0) + m.get("black", 0))
                for m in moves
            ]
            weighted_moves = [(m, w) for m, w in weighted_moves if w >= _MIN_MOVE_GAMES]
            if not weighted_moves:
                # All moves are below threshold — play the most common one.
                return jsonify({"uci": moves[0]["uci"], "source": "opening"})
            total_weight = sum(w for _, w in weighted_moves)
            r = random.uniform(0, total_weight)
            cumulative = 0.0
            for m, w in weighted_moves:
                cumulative += w
                if r <= cumulative:
                    return jsonify({"uci": m["uci"], "source": "opening"})
            return jsonify({"uci": weighted_moves[0][0]["uci"], "source": "opening"})

    # ------------------------------------------------------------------
    # 2. Post-opening habit injection
    # ------------------------------------------------------------------
    if lookup_fen in habits_by_fen:
        h = habits_by_fen[lookup_fen]
        prob = h["games"] / h["total"] if h["total"] > 0 else 0
        if random.random() < prob:
            return jsonify({"uci": h["player_move_uci"], "source": "habit"})

    # ------------------------------------------------------------------
    # 3. Maia engine (falls back to Stockfish if maia2 is not installed)
    # ------------------------------------------------------------------
    try:
        elo = model.get("opponent_elo") or 1500
        uci = maia_engine.get_move(fen, elo)
        if uci is None:
            return jsonify({"error": "engine returned no move"}), 500
        return jsonify({"uci": uci, "source": "engine"})
    except Exception as exc:
        return jsonify({"error": f"Engine error: {exc}"}), 500


@app.post("/api/import-pgn")
def api_import_pgn():
    username = request.form.get("username", "").strip()
    color = request.form.get("color", "white")
    max_plies = request.form.get("max_plies", "")
    pgn_file = request.files.get("pgn_file")

    if not username or not pgn_file or not pgn_file.filename:
        return jsonify({"error": "username and pgn_file are required"}), 400

    params = {
        "username": username,
        "color": color,
        "filename": pgn_file.filename,
    }
    if max_plies.isdigit():
        params["max_plies"] = int(max_plies)

    user = get_current_user()
    if err := _check_user_job_limit(user): return err
    if err := _check_plan_limit(user, "import"): return err
    job = registry.create("import", params, user_id=user["id"] if user else None)

    # Save the uploaded file under the job ID so the subprocess can read it.
    pgn_path = str(UPLOADS_DIR / f"{job.id}.pgn")
    pgn_file.save(pgn_path)

    argv = build_import_argv(params, pgn_path)
    launch_job(job, argv, REPO_ROOT, registry)
    return jsonify({"job_id": job.id})



# ---------------------------------------------------------------------------
# Admin routes
# ---------------------------------------------------------------------------

_ADMIN_VALID_ROLES = {"user", "admin"} | _TITLED_ROLES


def _require_admin():
    user = get_current_user()
    if not user or user.get("role") != "admin":
        abort(403)
    return user


@app.get("/admin")
def admin_page():
    _require_admin()
    return render_template("admin.html")


@app.get("/api/admin/stats")
def api_admin_stats():
    _require_admin()
    return jsonify(registry.admin_stats())


@app.get("/api/admin/users")
def api_admin_users():
    _require_admin()
    return jsonify(registry.list_users_with_stats())


@app.get("/api/admin/jobs")
def api_admin_jobs():
    _require_admin()
    return jsonify(registry.list_all()[:200])


@app.post("/api/admin/users/<user_id>/role")
def api_admin_set_role(user_id: str):
    _require_admin()
    data = request.get_json(force=True)
    role = (data.get("role") or "").strip()
    if role not in _ADMIN_VALID_ROLES:
        return jsonify({"error": f"Invalid role '{role}'"}), 400
    found = registry.set_user_role(user_id, role)
    if not found:
        return jsonify({"error": "User not found"}), 404
    return jsonify({"status": "ok", "role": role})


# ---------------------------------------------------------------------------
# Admin: featured players CRUD
# ---------------------------------------------------------------------------


@app.get("/api/admin/players")
def api_admin_players_list():
    _require_admin()
    return jsonify(featured_player_manager.list_all())


def _generate_player_description(slug: str, display_name: str, title: str | None, profile_path: str, force: bool = False) -> None:
    """Generate an AI description for a featured player and store it in the DB."""
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

        first_w = sw.get("first_move_distribution", [])
        first_b = sb.get("first_move_distribution", [])
        fm_w = f"1.{first_w[0]['san']} ({first_w[0]['pct']*100:.0f}%)" if first_w else "varied"
        fm_b = f"1...{first_b[0]['san']} ({first_b[0]['pct']*100:.0f}%)" if first_b else "varied"
        end_conv = ph.get("endgame_conversion_rate")
        end_str = f"{end_conv:.0%}" if end_conv is not None else "unknown"

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
            featured_player_manager.set_description(slug, description, force=force)
    except Exception:
        pass  # Description generation is best-effort


@app.post("/api/admin/players")
def api_admin_players_create():
    _require_admin()
    data = request.get_json(force=True)
    slug         = (data.get("slug") or "").strip().lower()
    display_name = (data.get("display_name") or "").strip()
    platform     = (data.get("platform") or "lichess").strip()
    username     = (data.get("username") or "").strip()
    title        = (data.get("title") or "").strip() or None
    speeds       = (data.get("speeds") or "blitz,rapid").strip()
    description  = (data.get("description") or "").strip() or None
    photo_url    = (data.get("photo_url") or "").strip() or None

    if not slug or not display_name or not username:
        return jsonify({"error": "slug, display_name, and username are required"}), 400

    if not re.match(r'^[a-z0-9-]+$', slug):
        return jsonify({"error": "slug must be lowercase alphanumeric with hyphens only"}), 400

    if featured_player_manager.get(slug):
        return jsonify({"error": f"Slug '{slug}' already exists"}), 409

    featured_player_manager.create(slug, display_name, platform, username, title, speeds, description, photo_url)

    job_params = {
        "opponent_username": username,
        "platform": platform,
        "speeds": speeds,
        "color": "both",
    }
    job = registry.create("train-bot", {**job_params, "featured_slug": slug}, user_id=None)
    out_path        = str(OUTPUT_DIR / f"{job.id}.json")
    white_book_path = str(PLAYERS_DIR / f"{slug}-white.json")
    black_book_path = str(PLAYERS_DIR / f"{slug}-black.json")
    profile_path    = str(PLAYERS_DIR / f"{slug}-profile.json")
    job.out_path = out_path
    registry.set_out_path(job.id, out_path)

    def _on_complete(status: str, exit_code: int) -> None:
        if status == "done":
            elo = None
            try:
                with open(out_path, encoding="utf-8") as f:
                    model = json.load(f)
                elo = model.get("opponent_elo")
            except Exception:
                pass
            profile_path_exists = Path(profile_path).exists()
            featured_player_manager.set_ready(
                slug, elo, white_book_path, black_book_path, out_path,
                profile_path if profile_path_exists else None,
            )
            if profile_path_exists:
                threading.Thread(
                    target=_generate_player_description,
                    args=(slug, display_name, title, profile_path),
                    daemon=True,
                ).start()
        else:
            featured_player_manager.set_failed(slug)

    argv = build_featured_player_argv(job_params, out_path, white_book_path, black_book_path, profile_path)
    launch_job(job, argv, REPO_ROOT, registry, completion_callback=_on_complete)
    return jsonify({"slug": slug, "job_id": job.id}), 201


@app.delete("/api/admin/players/<slug>")
def api_admin_players_delete(slug: str):
    _require_admin()
    player = featured_player_manager.get(slug)
    if not player:
        return jsonify({"error": "not found"}), 404
    for path_key in ("white_book_path", "black_book_path"):
        p = player.get(path_key)
        if p:
            try:
                Path(p).unlink(missing_ok=True)
            except OSError:
                pass
    featured_player_manager.delete(slug)
    return jsonify({"ok": True})


@app.post("/api/admin/players/<slug>/photo")
def api_admin_players_upload_photo(slug: str):
    _require_admin()
    player = featured_player_manager.get(slug)
    if not player:
        return jsonify({"error": "not found"}), 404
    if "photo" not in request.files:
        return jsonify({"error": "no file"}), 400
    file = request.files["photo"]
    if not file.filename:
        return jsonify({"error": "no filename"}), 400
    ext = Path(file.filename).suffix.lower()
    if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
        return jsonify({"error": "unsupported format; use jpg, png, or webp"}), 400
    photos_dir = REPO_ROOT / "web" / "static" / "player-photos"
    photos_dir.mkdir(exist_ok=True)
    for old in photos_dir.glob(f"{slug}.*"):
        old.unlink(missing_ok=True)
    filename = f"{slug}{ext}"
    file.save(photos_dir / filename)
    photo_url = f"/static/player-photos/{filename}"
    featured_player_manager.update_meta(
        slug, player["display_name"], player["title"], player["description"], photo_url
    )
    profile_path = player.get("profile_json_path")
    if profile_path and Path(profile_path).exists():
        try:
            profile_data = json.loads(Path(profile_path).read_text())
            profile_data["photo_url"] = photo_url
            Path(profile_path).write_text(json.dumps(profile_data, indent=2))
        except Exception:
            pass
    return jsonify({"ok": True, "photo_url": photo_url})


@app.patch("/api/admin/players/<slug>")
def api_admin_players_update(slug: str):
    _require_admin()
    player = featured_player_manager.get(slug)
    if not player:
        return jsonify({"error": "not found"}), 404
    data = request.get_json(force=True)
    display_name   = (data.get("display_name") or "").strip()
    title          = (data.get("title") or "").strip() or None
    description    = (data.get("description") or "").strip() or None
    photo_position = data.get("photo_position")
    if photo_position is not None:
        try:
            photo_position = max(0, min(100, int(photo_position)))
        except (TypeError, ValueError):
            photo_position = None
    if not display_name:
        return jsonify({"error": "display_name is required"}), 400
    featured_player_manager.update_meta(
        slug, display_name, title, description, player["photo_url"], photo_position
    )
    return jsonify({"ok": True})


@app.post("/api/admin/players/<slug>/retrain")
def api_admin_players_retrain(slug: str):
    _require_admin()
    player = featured_player_manager.get(slug)
    if not player:
        return jsonify({"error": "not found"}), 404

    body = request.get_json(silent=True) or {}
    platform          = body.get("platform")          or player["platform"]
    username          = body.get("username")          or player["username"]
    speeds            = body.get("speeds")            or player["speeds"]
    regen_bot         = body.get("regen_bot",         True)
    regen_profile     = body.get("regen_profile",     True)
    regen_description = body.get("regen_description", True)

    # Persist any changed training params before starting the job.
    if platform != player["platform"] or username != player["username"] or speeds != player["speeds"]:
        featured_player_manager.update_training_params(slug, platform, username, speeds)

    profile_path    = str(PLAYERS_DIR / f"{slug}-profile.json")

    # Description-only mode: no training job needed.
    if not regen_bot:
        if regen_description:
            if not Path(profile_path).exists():
                return jsonify({"error": "No profile found; retrain the bot first to generate stats"}), 400
            threading.Thread(
                target=_generate_player_description,
                args=(slug, player["display_name"], player.get("title"), profile_path),
                kwargs={"force": True},
                daemon=True,
            ).start()
        return jsonify({"slug": slug, "description_regenerating": regen_description})

    featured_player_manager.set_status(slug, "pending")

    job_params = {
        "opponent_username": username,
        "platform": platform,
        "speeds": speeds,
        "color": "both",
    }
    job = registry.create("train-bot", {**job_params, "featured_slug": slug}, user_id=None)
    out_path        = str(OUTPUT_DIR / f"{job.id}.json")
    white_book_path = str(PLAYERS_DIR / f"{slug}-white.json")
    black_book_path = str(PLAYERS_DIR / f"{slug}-black.json")
    job.out_path = out_path
    registry.set_out_path(job.id, out_path)

    def _on_complete(status: str, exit_code: int) -> None:
        if status == "done":
            elo = None
            try:
                with open(out_path, encoding="utf-8") as f:
                    model = json.load(f)
                elo = model.get("opponent_elo")
            except Exception:
                pass
            profile_path_exists = Path(profile_path).exists()
            featured_player_manager.set_ready(
                slug, elo, white_book_path, black_book_path, out_path,
                profile_path if profile_path_exists else None,
            )
            if profile_path_exists and regen_description:
                threading.Thread(
                    target=_generate_player_description,
                    args=(slug, player["display_name"], player.get("title"), profile_path),
                    kwargs={"force": True},
                    daemon=True,
                ).start()
        else:
            featured_player_manager.set_failed(slug)

    argv = build_featured_player_argv(
        job_params, out_path, white_book_path, black_book_path, profile_path,
        include_profile=regen_profile,
    )
    launch_job(job, argv, REPO_ROOT, registry, completion_callback=_on_complete)
    return jsonify({"slug": slug, "job_id": job.id})


@app.post("/api/admin/players/reorder")
def api_admin_players_reorder():
    _require_admin()
    body = request.get_json(force=True)
    slugs = body.get("slugs")
    if not isinstance(slugs, list) or not slugs:
        return jsonify({"error": "slugs list required"}), 400
    featured_player_manager.reorder(slugs)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Admin backup routes
# ---------------------------------------------------------------------------


@app.post("/api/admin/backup")
def admin_create_backup():
    _require_admin()
    body = request.get_json(silent=True) or {}
    result = backup_module.create_backup(description=body.get("description", ""))
    return jsonify(result)


@app.get("/api/admin/backups")
def admin_list_backups():
    _require_admin()
    return jsonify(backup_module.list_backups())


@app.post("/api/admin/backups/<backup_id>/restore")
def admin_restore_backup(backup_id: str):
    _require_admin()
    try:
        result = backup_module.restore_backup(backup_id)
        return jsonify(result)
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 404


@app.delete("/api/admin/backups/<backup_id>")
def admin_delete_backup(backup_id: str):
    _require_admin()
    try:
        backup_module.delete_backup(backup_id)
        return jsonify({"ok": True})
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 404


@app.get("/api/admin/backups/<op_id>/status")
def admin_backup_status(op_id: str):
    _require_admin()
    return jsonify(backup_module.get_status(op_id))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(f"[mysecond web] Serving from {REPO_ROOT}")
    print("[mysecond web] Open http://localhost:5000 in your browser")
    app.run(host="127.0.0.1", port=5000, debug=True, threaded=True)
