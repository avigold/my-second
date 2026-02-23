"""Stockfish UCI wrapper built on python-chess SimpleEngine."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import chess
import chess.engine

# Use most available cores, leaving one free for the OS / web server.
_DEFAULT_THREADS = max(1, (os.cpu_count() or 1) - 1)


def find_stockfish() -> Path:
    """Return the path to the Stockfish binary.

    Resolution order:
    1. ``MYSECOND_STOCKFISH_PATH`` environment variable
    2. ``which stockfish`` on ``$PATH``
    """
    env_val = os.environ.get("MYSECOND_STOCKFISH_PATH")
    if env_val:
        candidate = Path(env_val)
        if candidate.is_file():
            return candidate
        raise FileNotFoundError(
            f"MYSECOND_STOCKFISH_PATH={env_val!r} does not point to an existing file"
        )

    which_result = shutil.which("stockfish")
    if which_result:
        return Path(which_result)

    raise FileNotFoundError(
        "stockfish not found in PATH. "
        "Install it (e.g. brew install stockfish) or set MYSECOND_STOCKFISH_PATH."
    )


class Engine:
    """Thin, context-manager-aware wrapper around chess.engine.SimpleEngine.

    Not thread-safe: each thread must own its own Engine instance.
    """

    def __init__(self, path: Path | None = None, threads: int = _DEFAULT_THREADS) -> None:
        self._path = path or find_stockfish()
        self._engine = chess.engine.SimpleEngine.popen_uci(str(self._path))
        if threads > 1:
            self._engine.configure({"Threads": threads})

    # ------------------------------------------------------------------
    # Public analysis API
    # ------------------------------------------------------------------

    def analyse_multipv(
        self,
        board: chess.Board,
        depth: int,
        multipv: int,
        time_ms: int | None = None,
    ) -> list[chess.engine.InfoDict]:
        """Analyse *board* with MultiPV; always returns a list of InfoDict."""
        limit = self._build_limit(depth, time_ms)
        result = self._engine.analyse(board, limit, multipv=multipv)
        if isinstance(result, list):
            return result
        return [result]

    def analyse_single(
        self,
        board: chess.Board,
        depth: int,
    ) -> chess.engine.InfoDict:
        """Analyse *board* at *depth*; returns one InfoDict."""
        result = self._engine.analyse(board, chess.engine.Limit(depth=depth))
        if isinstance(result, list):
            return result[0]
        return result  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _build_limit(depth: int, time_ms: int | None) -> chess.engine.Limit:
        if time_ms is not None:
            return chess.engine.Limit(depth=depth, time=time_ms / 1000.0)
        return chess.engine.Limit(depth=depth)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        self._engine.quit()

    def __enter__(self) -> "Engine":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
