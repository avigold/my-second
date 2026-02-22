"""Lichess masters opening-explorer client.

ALL network I/O lives in this module.  The explorer layer is intentionally
isolated so it can be swapped for another backend without touching the rest
of the codebase.

Database note
-------------
We query the Lichess *masters* endpoint, which indexes classical OTB games
from FIDE-rated players (≥ 2200 Elo) sourced from FIDE, national federations,
and major tournament organisers.  This is a good proxy for professional
preparation but does **not** cover every game in the ChessBase Mega Database.
For the purposes of novelty detection the distinction matters: a move showing
0 games here may still appear in private databases.  Results should be
cross-checked against Mega before committing a novelty to serious play.
"""

from __future__ import annotations

import time
from typing import Any

import requests

from .cache import Cache
from .models import ExplorerData, MoveStats

_LICHESS_MASTERS_URL = "https://explorer.lichess.ovh/masters"
_DEFAULT_BACKEND = "lichess_masters"
_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "mysecond/0.1.0",
}


class LichessExplorer:
    """Fetches full opening-explorer data (aggregate + per-move) for a position.

    Responses are cached in SQLite, keyed by ``(fen, backend)``.
    HTTP 429 responses are retried with exponential back-off.
    """

    def __init__(self, cache: Cache, backend: str = _DEFAULT_BACKEND) -> None:
        self._cache = cache
        self._backend = backend
        self._session = requests.Session()
        self._session.headers.update(_HEADERS)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_data(self, fen: str) -> ExplorerData | None:
        """Return full explorer data for *fen*, using the cache first."""
        cached = self._cache.get(fen, self._backend)
        if cached is not None:
            return self._parse(cached)

        raw = self._fetch(fen)
        if raw is None:
            return None

        self._cache.set(fen, self._backend, raw)
        return self._parse(raw)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _fetch(self, fen: str, max_retries: int = 5) -> dict[str, Any] | None:
        params: dict[str, str] = {"fen": fen}
        for attempt in range(max_retries):
            try:
                resp = self._session.get(
                    _LICHESS_MASTERS_URL,
                    params=params,
                    timeout=10,
                )
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code == 429:
                    time.sleep(2 ** attempt)
                    continue
                return None
            except requests.RequestException:
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
        return None

    @staticmethod
    def _parse(data: dict[str, Any]) -> ExplorerData:
        moves: list[MoveStats] = []
        for m in data.get("moves", []):
            moves.append(
                MoveStats(
                    uci=m.get("uci", ""),
                    white=int(m.get("white", 0)),
                    draws=int(m.get("draws", 0)),
                    black=int(m.get("black", 0)),
                    average_rating=int(m.get("averageRating", 0)),
                )
            )
        return ExplorerData(
            white=int(data.get("white", 0)),
            draws=int(data.get("draws", 0)),
            black=int(data.get("black", 0)),
            moves=moves,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> "LichessExplorer":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
