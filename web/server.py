"""Flask web server for the mysecond chess novelty finder."""

from __future__ import annotations

import json
import os
import queue
from pathlib import Path

import chess
from flask import (
    Flask,
    Response,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    stream_with_context,
)

from auth import (
    chesscom_auth_url,
    chesscom_enabled,
    chesscom_handle_callback,
    get_current_user,
    lichess_auth_url,
    lichess_enabled,
    lichess_handle_callback,
    login_required,
    set_session_user,
)

from habits_parser import parse_habits
from jobs import Job, JobRegistry
from pgn_parser import parse_novelties
from repertoire_parser import parse_repertoire
from runner import build_fetch_argv, build_habits_argv, build_import_argv, build_repertoire_argv, build_search_argv, build_strategise_argv, launch_job

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Project root: parent of this file's directory (web/).
REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"

# Auto-load .env so ANTHROPIC_API_KEY etc. are available without a manual export.
_env_file = REPO_ROOT / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            _k = _k.strip()
            if _k not in os.environ:          # don't override values already in env
                os.environ[_k] = _v.strip()
OUTPUT_DIR = DATA_DIR / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
UPLOADS_DIR = DATA_DIR / "uploads"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or os.urandom(24)

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://mysecond:mysecond@localhost:5433/mysecond",
)
registry = JobRegistry(DATABASE_URL)

DIST_DIR = REPO_ROOT / "web" / "static" / "dist"

# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------

_AUTH_EXEMPT_PATHS = {"/login", "/auth/logout"}
_AUTH_EXEMPT_PREFIXES = ("/auth/lichess", "/auth/chesscom", "/static/")


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
    return {"current_user": get_current_user()}


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------


@app.get("/login")
def login_page():
    if get_current_user():
        return redirect("/")
    return render_template(
        "login.html",
        lichess_ok=lichess_enabled(),
        chesscom_ok=chesscom_enabled(),
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
    return redirect("/")


@app.get("/auth/chesscom")
def auth_chesscom():
    return redirect(chesscom_auth_url())


@app.get("/auth/chesscom/callback")
def auth_chesscom_callback():
    user = chesscom_handle_callback(registry)
    if user is None:
        return "Authentication failed — please try again.", 400
    set_session_user(user)
    return redirect("/")


@app.get("/auth/logout")
def auth_logout():
    session.clear()
    return redirect("/login")


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
    return render_template("dashboard.html")


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
    return render_template("job.html", job=job.to_dict())


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

    user = get_current_user()
    job = registry.create("fetch", params, user_id=user["id"] if user else None)
    argv = build_fetch_argv(params)
    launch_job(job, argv, REPO_ROOT, registry)
    return jsonify({"job_id": job.id})


@app.post("/api/search")
def api_search():
    params = request.get_json(force=True)
    if not params.get("side"):
        return jsonify({"error": "side is required"}), 400

    # Build output path before launching so the server knows where to find it.
    out_path = str(OUTPUT_DIR / f"{params.get('side', 'white')}_ideas.pgn")
    # Use a job-specific name to avoid collisions.
    user = get_current_user()
    job = registry.create("search", params, out_path=out_path, user_id=user["id"] if user else None)
    out_path = str(OUTPUT_DIR / f"{job.id}.pgn")
    job.out_path = out_path  # update before subprocess starts

    argv = build_search_argv(params, out_path)
    launch_job(job, argv, REPO_ROOT, registry)
    return jsonify({"job_id": job.id})


@app.get("/api/dashboard")
def api_dashboard():
    """Aggregate job data for the dashboard visualisations."""
    user = get_current_user()
    if user and user.get("role") == "admin":
        all_jobs = registry.list_all()
    else:
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

    return jsonify({
        "stats":          stats,
        "top_habits":     top_habits,
        "top_novelties":  top_novelties,
        "recent_jobs":    all_jobs[:6],
    })


@app.get("/api/jobs")
def api_jobs():
    user = get_current_user()
    if user and user.get("role") == "admin":
        return jsonify(registry.list_all())
    return jsonify(registry.list_for_user(user["id"]))


@app.get("/api/jobs/<job_id>")
def api_job(job_id: str):
    job = registry.get(job_id)
    if job is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(job.to_dict())


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


@app.delete("/api/jobs/<job_id>")
def api_delete_job(job_id: str):
    job = registry.get(job_id)
    if job is None:
        return jsonify({"error": "not found"}), 404
    # Cancel if still running.
    if job.process is not None and job.status == "running":
        job.process.terminate()
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

    user = get_current_user()
    job = registry.create("repertoire", params, user_id=user["id"] if user else None)
    out_path = str(OUTPUT_DIR / f"{job.id}.pgn")
    job.out_path = out_path

    argv = build_repertoire_argv(params, out_path)
    launch_job(job, argv, REPO_ROOT, registry)
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


@app.get("/import-pgn")
def import_pgn_page():
    return render_template("import.html")


@app.post("/api/habits")
def api_habits():
    params = request.get_json(force=True)
    if not params.get("username") or not params.get("color"):
        return jsonify({"error": "username and color are required"}), 400

    user = get_current_user()
    job = registry.create("habits", params, user_id=user["id"] if user else None)
    out_path = str(OUTPUT_DIR / f"{job.id}.pgn")
    job.out_path = out_path

    argv = build_habits_argv(params, out_path)
    launch_job(job, argv, REPO_ROOT, registry)
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

    # Inject API key from environment if not supplied by the client
    if not params.get("api_key"):
        params["api_key"] = os.environ.get("ANTHROPIC_API_KEY") or None

    user = get_current_user()
    job = registry.create("strategise", params, user_id=user["id"] if user else None)
    out_path = str(OUTPUT_DIR / f"{job.id}.json")
    job.out_path = out_path

    argv = build_strategise_argv(params, out_path)
    launch_job(job, argv, REPO_ROOT, registry)
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
    job = registry.create("import", params, user_id=user["id"] if user else None)

    # Save the uploaded file under the job ID so the subprocess can read it.
    pgn_path = str(UPLOADS_DIR / f"{job.id}.pgn")
    pgn_file.save(pgn_path)

    argv = build_import_argv(params, pgn_path)
    launch_job(job, argv, REPO_ROOT, registry)
    return jsonify({"job_id": job.id})



# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(f"[mysecond web] Serving from {REPO_ROOT}")
    print("[mysecond web] Open http://localhost:5000 in your browser")
    app.run(host="127.0.0.1", port=5000, debug=True, threaded=True)
