"""Subprocess launcher for mysecond CLI commands."""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .jobs import Job, JobRegistry


def _mysecond_bin() -> str:
    """Return the path to the mysecond CLI script in the active venv."""
    return str(Path(sys.executable).parent / "mysecond")


def build_fetch_argv(params: dict) -> list[str]:
    """Build the argv list for ``mysecond fetch-player-games``."""
    cmd = [_mysecond_bin(), "fetch-player-games"]
    cmd += ["--username", params["username"]]
    cmd += ["--color", params["color"]]
    if params.get("speeds"):
        cmd += ["--speeds", params["speeds"]]
    if params.get("max_games"):
        cmd += ["--max-games", str(params["max_games"])]
    if params.get("max_plies"):
        cmd += ["--max-plies", str(params["max_plies"])]
    if params.get("platform"):
        cmd += ["--platform", params["platform"]]
    if params.get("since_date"):
        cmd += ["--since", params["since_date"]]
    return cmd


def build_search_argv(params: dict, out_path: str) -> list[str]:
    """Build the argv list for ``mysecond search``."""
    cmd = [_mysecond_bin(), "search"]
    cmd += ["--side", params["side"]]
    cmd += ["--out", out_path]

    if params.get("fen"):
        cmd += ["--fen", params["fen"]]
    if params.get("plies"):
        cmd += ["--plies", str(params["plies"])]
    if params.get("beam"):
        cmd += ["--beam", str(params["beam"])]
    if params.get("min_book_games"):
        cmd += ["--min-book-games", str(params["min_book_games"])]
    if params.get("novelty_threshold") is not None:
        cmd += ["--novelty-threshold", str(params["novelty_threshold"])]
    if params.get("opponent_responses"):
        cmd += ["--opponent-responses", str(params["opponent_responses"])]
    if params.get("depths"):
        cmd += ["--depths", params["depths"]]
    if params.get("time_ms"):
        cmd += ["--time-ms", str(params["time_ms"])]
    if params.get("min_eval") is not None:
        cmd += ["--min-eval", str(params["min_eval"])]
    if params.get("continuation_plies"):
        cmd += ["--continuations", str(params["continuation_plies"])]
    if params.get("workers"):
        cmd += ["--workers", str(params["workers"])]
    if params.get("max_positions"):
        cmd += ["--max-positions", str(params["max_positions"])]
    if params.get("max_candidates"):
        cmd += ["--max-candidates", str(params["max_candidates"])]
    if params.get("player"):
        cmd += ["--player", params["player"]]
    if params.get("opponent"):
        cmd += ["--opponent", params["opponent"]]
    if params.get("min_player_games"):
        cmd += ["--min-player-games", str(params["min_player_games"])]
    if params.get("min_opponent_games"):
        cmd += ["--min-opponent-games", str(params["min_opponent_games"])]
    if params.get("player_platform"):
        cmd += ["--player-platform", params["player_platform"]]
    if params.get("opponent_platform"):
        cmd += ["--opponent-platform", params["opponent_platform"]]
    if params.get("player_speeds"):
        cmd += ["--player-speeds", params["player_speeds"]]
    if params.get("opponent_speeds"):
        cmd += ["--opponent-speeds", params["opponent_speeds"]]
    return cmd


def launch_job(job: "Job", argv: list[str], cwd: Path, registry: "JobRegistry") -> None:
    """Start a subprocess and stream its stdout into job.queue.

    Merges stderr into stdout so all output appears in the log stream.
    Runs the reader in a daemon thread so it doesn't block the server.
    """
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        cwd=str(cwd),
    )
    job.process = proc

    def _reader() -> None:
        assert proc.stdout is not None
        for raw_line in proc.stdout:
            line = raw_line.rstrip("\n")
            job.log_lines.append(line)
            job.queue.put(line)
        proc.wait()
        exit_code = proc.returncode
        status = "done" if exit_code == 0 else "failed"
        job.queue.put(None)  # sentinel — SSE generator will close
        registry.update_status(job.id, status, exit_code=exit_code)

    t = threading.Thread(target=_reader, daemon=True)
    t.start()


def build_repertoire_argv(params: dict, out_path: str) -> list[str]:
    """Build the argv list for ``mysecond extract-repertoire``."""
    cmd = [_mysecond_bin(), "extract-repertoire"]
    cmd += ["--username", params["username"]]
    cmd += ["--color", params["color"]]
    cmd += ["--out", out_path]
    if params.get("platform"):
        cmd += ["--platform", params["platform"]]
    if params.get("speeds"):
        cmd += ["--speeds", params["speeds"]]
    if params.get("min_games"):
        cmd += ["--min-games", str(params["min_games"])]
    if params.get("max_plies"):
        cmd += ["--max-plies", str(params["max_plies"])]
    return cmd


def build_habits_argv(params: dict, out_path: str) -> list[str]:
    """Build the argv list for ``mysecond analyze-habits``."""
    cmd = [_mysecond_bin(), "analyze-habits"]
    cmd += ["--username", params["username"]]
    cmd += ["--color", params["color"]]
    cmd += ["--out", out_path]
    if params.get("speeds"):
        cmd += ["--speeds", params["speeds"]]
    if params.get("min_games"):
        cmd += ["--min-games", str(params["min_games"])]
    if params.get("max_positions"):
        cmd += ["--max-positions", str(params["max_positions"])]
    if params.get("min_eval_gap"):
        cmd += ["--min-eval-gap", str(params["min_eval_gap"])]
    if params.get("depth"):
        cmd += ["--depth", str(params["depth"])]
    return cmd


def build_import_argv(params: dict, pgn_path: str) -> list[str]:
    """Build the argv list for ``mysecond import-pgn-player``."""
    cmd = [_mysecond_bin(), "import-pgn-player"]
    cmd += ["--pgn", pgn_path]
    cmd += ["--username", params["username"]]
    cmd += ["--color", params["color"]]
    if params.get("max_plies"):
        cmd += ["--max-plies", str(params["max_plies"])]
    return cmd
