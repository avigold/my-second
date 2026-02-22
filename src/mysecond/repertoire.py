"""Per-player opening repertoire extracted from the Lichess player explorer.

The Lichess ``/player`` endpoint returns opening statistics filtered to a
specific Lichess user with a specific colour.  We use this in two ways:

  1. At the *player's* turns – decide which in-book moves to walk deeper
     into.  Without a player filter the walk follows all theory; with one
     it stays focused on lines the player actually plays.

  2. At the *opponent's* turns – follow the opponent's real responses
     instead of the most-popular master moves, scoping the search to lines
     we will actually face.

Rate-limiting
-------------
The ``/player`` endpoint is more aggressively rate-limited than ``/masters``.
A minimum inter-request delay of 0.5 s is enforced inside the class.
All responses are persisted in the shared SQLite cache so repeated runs
avoid redundant HTTP calls.

Speed filter
------------
By default only ``rapid`` and ``classical`` games are included to keep the
repertoire relevant to serious over-the-board preparation.  Pass
``speeds="bullet,blitz,rapid,classical"`` to include all time controls.
"""

from __future__ import annotations

import time
from typing import Any

import requests

from .cache import Cache
from .models import ExplorerData, MoveStats

_LICHESS_PLAYER_URL = "https://explorer.lichess.ovh/player"
_MIN_REQUEST_INTERVAL = 0.5  # seconds between /player requests (rate limit)


class PlayerExplorer:
    """Opening-explorer data filtered to a single Lichess user.

    Parameters
    ----------
    username:
        Lichess username (case-insensitive on the server side).
    color:
        ``'white'`` or ``'black'`` – the colour we want statistics for.
    cache:
        Shared :class:`~mysecond.cache.Cache` instance.
    speeds:
        Comma-separated Lichess speed names to include.
        Default: ``'rapid,classical'``.
    """

    def __init__(
        self,
        username: str,
        color: str,
        cache: Cache,
        speeds: str = "rapid,classical",
    ) -> None:
        if color not in ("white", "black"):
            raise ValueError(f"color must be 'white' or 'black', got {color!r}")
        self._username = username
        self._color = color
        self._speeds = speeds
        # Cache key encodes all query dimensions so different configs don't collide.
        self._backend = f"lichess_player_{username.lower()}_{color}_{speeds}"
        self._cache = cache
        self._session = requests.Session()
        self._session.headers.update(
            {"Accept": "application/json", "User-Agent": "mysecond/0.1.0"}
        )
        self._last_ts: float = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_data(self, fen: str, local_only: bool = False) -> ExplorerData | None:
        """Return opening data for *fen* filtered to this player.

        Parameters
        ----------
        fen:
            Position FEN.
        local_only:
            When ``True``, return ``None`` on a cache miss instead of fetching
            from the network.  Use this in :func:`~mysecond.search.find_novelties`
            after running ``fetch-player-games``, so the theory walk never
            stalls on rate-limited HTTP calls for positions not in the local book.
        """
        cached = self._cache.get(fen, self._backend)
        if cached is not None:
            return _parse(cached)

        if local_only:
            return None  # no local data → caller uses fallback behaviour

        raw = self._fetch(fen)
        if raw is None:
            return None

        self._cache.set(fen, self._backend, raw)
        return _parse(raw)

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> "PlayerExplorer":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _fetch(self, fen: str, max_retries: int = 5) -> dict[str, Any] | None:
        # Respect the /player rate limit.
        elapsed = time.monotonic() - self._last_ts
        if elapsed < _MIN_REQUEST_INTERVAL:
            time.sleep(_MIN_REQUEST_INTERVAL - elapsed)

        params: dict[str, Any] = {
            "player": self._username,
            "color": self._color,
            "fen": fen,
            "recentGames": 0,  # omit recent-game snippets; we only need counts
        }
        if self._speeds:
            # The Lichess API expects repeated `speeds[]` params.
            params["speeds[]"] = self._speeds

        for attempt in range(max_retries):
            try:
                resp = self._session.get(
                    _LICHESS_PLAYER_URL,
                    params=params,
                    timeout=15,
                )
                self._last_ts = time.monotonic()
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code == 429:
                    time.sleep(2**attempt)
                    continue
                return None
            except requests.RequestException:
                if attempt < max_retries - 1:
                    time.sleep(2**attempt)
        return None


# ---------------------------------------------------------------------------
# Shared parser (same shape as LichessExplorer._parse)
# ---------------------------------------------------------------------------


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
