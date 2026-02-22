"""Flask web server for the mysecond chess novelty finder."""

from __future__ import annotations

import json
import os
import queue
from pathlib import Path

from flask import (
    Flask,
    Response,
    jsonify,
    render_template,
    request,
    send_file,
    stream_with_context,
)

from jobs import Job, JobRegistry
from runner import build_fetch_argv, build_search_argv, launch_job

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Project root: parent of this file's directory (web/).
REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
OUTPUT_DIR = DATA_DIR / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.urandom(24)

registry = JobRegistry(DATA_DIR / "jobs.sqlite")


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------


@app.get("/")
def index():
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


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------


@app.post("/api/fetch")
def api_fetch():
    params = request.get_json(force=True)
    if not params.get("username") or not params.get("color"):
        return jsonify({"error": "username and color are required"}), 400

    job = registry.create("fetch", params)
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
    job = registry.create("search", params, out_path=out_path)
    out_path = str(OUTPUT_DIR / f"{job.id}.pgn")
    job.out_path = out_path  # update before subprocess starts

    argv = build_search_argv(params, out_path)
    launch_job(job, argv, REPO_ROOT, registry)
    return jsonify({"job_id": job.id})


@app.get("/api/jobs")
def api_jobs():
    return jsonify(registry.list_all())


@app.get("/api/jobs/<job_id>")
def api_job(job_id: str):
    job = registry.get(job_id)
    if job is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(job.to_dict())


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
def api_cancel(job_id: str):
    job = registry.get(job_id)
    if job is None:
        return jsonify({"error": "not found"}), 404
    if job.process is not None and job.status == "running":
        job.process.terminate()
        registry.update_status(job_id, "cancelled")
    return jsonify({"status": "cancelled"})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(f"[mysecond web] Serving from {REPO_ROOT}")
    print("[mysecond web] Open http://localhost:5000 in your browser")
    app.run(host="127.0.0.1", port=5000, debug=True, threaded=True)
