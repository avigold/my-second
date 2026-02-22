"""Download and index a player's Lichess game history for local opening-book construction.

After running ``mysecond fetch-player-games``, the shared SQLite cache contains
per-position opening statistics for the specified player, in exactly the same
format as the Lichess ``/player`` explorer endpoint.  Subsequent
``mysecond search`` runs read directly from the cache without making any
HTTP requests to the rate-limited ``/player`` endpoint.

How it works
------------
1. The Lichess games export API (``GET /api/games/user/{username}``) streams
   all games for a player in PGN format, optionally filtered by time control,
   colour, and date range.
2. python-chess parses each game.  We walk every game's moves up to
   ``max_plies`` half-moves, recording statistics only for positions where
   it is **the player's turn** (i.e. the colour we care about).
3. The resulting per-position dict is converted to Lichess-explorer-compatible
   JSON and written into the shared SQLite cache under the key
   ``lichess_player_{username}_{color}_{speeds}``.  This is the exact backend
   key that :class:`~mysecond.repertoire.PlayerExplorer` reads, so the cache
   transparently shadows the network endpoint.

Incremental updates
-------------------
Pass ``--since YYYY-MM-DD`` to fetch only games played since that date and
**merge** their counts into any existing cache entries.  Without ``--since``
a full rebuild is performed and existing entries are replaced.

Daily cron example::

    0 4 * * * cd /path/to/mysecond && .venv/bin/mysecond fetch-player-games \\
              --username GothamChess --color white --since yesterday
"""

from __future__ import annotations

import io
import json
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import chess
import chess.pgn
import requests

from .cache import Cache

_LICHESS_GAMES_URL = "https://lichess.org/api/games/user/{username}"
_DEFAULT_DB = Path("data/cache.sqlite")
_HEADERS = {
    "Accept": "application/x-chess-pgn",
    "User-Agent": "mysecond/0.1.0",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_player_games(
    username: str,
    color: str,                          # 'white' or 'black'
    cache: Cache,
    speeds: str = "blitz,rapid,classical",
    max_plies: int = 30,
    max_games: int = 10_000,
    since_ts: int | None = None,         # Unix milliseconds
    verbose: bool = True,
) -> int:
    """Download games and populate the cache with per-position statistics.

    Parameters
    ----------
    username:
        Lichess username.
    color:
        ``'white'`` or ``'black'`` — the colour to index.
    cache:
        Shared :class:`~mysecond.cache.Cache` instance.
    speeds:
        Comma-separated Lichess time controls (e.g. ``'blitz,rapid,classical'``).
    max_plies:
        Walk each game at most this many half-moves deep.
    max_games:
        Maximum games to download.
    since_ts:
        If set, only download games played after this Unix-millisecond timestamp
        and merge into existing cache entries (incremental update).
    verbose:
        Print progress messages.

    Returns
    -------
    int
        Number of unique positions indexed.
    """
    backend = _backend_key(username, color, speeds)

    if verbose:
        print(
            f"[fetch] Downloading {username}'s games as {color} "
            f"({speeds}) – up to {max_games} games …",
            flush=True,
        )

    pgn_text = _download_pgn(username, color, speeds, max_games, since_ts)

    if not pgn_text.strip():
        if verbose:
            print("[fetch] No games returned (check username / colour / speeds).")
        return 0

    if verbose:
        print("[fetch] Parsing games and building opening book …", flush=True)

    book = _build_book(pgn_text, color, max_plies, verbose=verbose)

    if not book:
        if verbose:
            print("[fetch] No positions extracted.")
        return 0

    if verbose:
        print(f"[fetch] Writing {len(book)} positions to cache …", flush=True)

    _store_book(book, cache, backend, merge=since_ts is not None)

    # Record the fetch timestamp so --since can be omitted in future runs.
    _write_fetch_meta(cache, backend)

    if verbose:
        print(f"[fetch] Done. {len(book)} positions cached for {username} ({color}).")

    return len(book)


def last_fetch_ts(username: str, color: str, speeds: str, cache: Cache) -> int | None:
    """Return the Unix-ms timestamp of the last successful fetch, or None."""
    backend = _backend_key(username, color, speeds)
    meta_key = f"_fetch_meta_{backend}"
    row = cache.get(meta_key, "meta")
    if row is None:
        return None
    return int(row.get("ts_ms", 0)) or None


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


def _download_pgn(
    username: str,
    color: str,
    speeds: str,
    max_games: int,
    since_ts: int | None,
) -> str:
    session = requests.Session()
    session.headers.update(_HEADERS)

    params: dict[str, Any] = {
        "perfType": speeds,
        "color": color,
        "max": max_games,
        "format": "pgn",
        "evals": "false",
        "opening": "false",
        "clocks": "false",
        "moves": "true",
    }
    if since_ts is not None:
        params["since"] = since_ts

    url = _LICHESS_GAMES_URL.format(username=username)
    try:
        resp = session.get(url, params=params, timeout=180)
        if resp.status_code == 404:
            raise RuntimeError(
                f"Lichess user '{username}' not found (404). "
                "Check the spelling — Lichess usernames are case-insensitive "
                "but must match exactly (e.g. 'GothamChess', not 'Rozman_Levy')."
            )
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as exc:
        raise RuntimeError(
            f"Failed to download games for {username}: {exc}"
        ) from exc
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Book construction
# ---------------------------------------------------------------------------


def _build_book(
    pgn_text: str,
    color: str,
    max_plies: int,
    verbose: bool = False,
) -> dict[str, dict[str, Any]]:
    """Parse PGN text and return per-position statistics.

    The resulting dict maps FEN → Lichess-explorer-compatible payload::

        {
            "white": <int>,   # white wins when player reached this position
            "draws": <int>,
            "black": <int>,   # black wins when player reached this position
            "moves": [
                {
                    "uci": "e2e4",
                    "white": <int>, "draws": <int>, "black": <int>,
                    "averageRating": 0,
                },
                ...
            ]
        }

    Only positions where it is *the player's turn* are recorded, so
    ``moves`` always reflects the player's own choices.
    """
    player_turn = chess.WHITE if color == "white" else chess.BLACK

    # book: fen → {white, draws, black, moves: {uci → {white, draws, black}}}
    book: dict[str, dict[str, Any]] = {}

    buf = io.StringIO(pgn_text)
    processed = 0
    skipped = 0

    while True:
        try:
            game = chess.pgn.read_game(buf)
        except Exception:  # noqa: BLE001
            skipped += 1
            continue
        if game is None:
            break

        result = game.headers.get("Result", "*")
        if result == "1-0":
            w, d, b = 1, 0, 0
        elif result == "0-1":
            w, d, b = 0, 0, 1
        elif result == "1/2-1/2":
            w, d, b = 0, 1, 0
        else:
            skipped += 1
            continue  # unfinished / aborted game

        board = game.board()
        ply = 0

        for move in game.mainline_moves():
            if ply >= max_plies:
                break

            # Record only the player's own moves at their turn.
            if board.turn == player_turn:
                fen = board.fen()
                uci = move.uci()

                if fen not in book:
                    book[fen] = {"white": 0, "draws": 0, "black": 0, "moves": {}}

                pos = book[fen]
                pos["white"] += w
                pos["draws"] += d
                pos["black"] += b

                if uci not in pos["moves"]:
                    pos["moves"][uci] = {"white": 0, "draws": 0, "black": 0}
                pos["moves"][uci]["white"] += w
                pos["moves"][uci]["draws"] += d
                pos["moves"][uci]["black"] += b

            board.push(move)
            ply += 1

        processed += 1
        if verbose and processed % 1000 == 0:
            print(
                f"  … {processed} games, {len(book)} positions so far",
                flush=True,
            )

    if verbose:
        print(
            f"[fetch] {processed} games parsed, {skipped} skipped, "
            f"{len(book)} unique positions.",
            flush=True,
        )

    return book


# ---------------------------------------------------------------------------
# Cache storage
# ---------------------------------------------------------------------------


def _store_book(
    book: dict[str, dict[str, Any]],
    cache: Cache,
    backend: str,
    merge: bool,
) -> None:
    """Write opening-book entries to the cache.

    In *merge* mode new counts are added to existing entries (for incremental
    updates).  In full mode existing entries are replaced.
    """
    for fen, pos in book.items():
        payload = _to_payload(pos)
        if merge:
            existing = cache.get(fen, backend)
            if existing is not None:
                payload = _merge_payloads(existing, payload)
        cache.set(fen, backend, payload)


def _to_payload(pos: dict[str, Any]) -> dict[str, Any]:
    """Convert internal book entry to Lichess-explorer-compatible JSON."""
    moves_list = [
        {
            "uci": uci,
            "white": counts["white"],
            "draws": counts["draws"],
            "black": counts["black"],
            "averageRating": 0,
        }
        for uci, counts in sorted(
            pos["moves"].items(),
            key=lambda kv: -(kv[1]["white"] + kv[1]["draws"] + kv[1]["black"]),
        )
    ]
    return {
        "white": pos["white"],
        "draws": pos["draws"],
        "black": pos["black"],
        "moves": moves_list,
    }


def _merge_payloads(
    existing: dict[str, Any],
    new: dict[str, Any],
) -> dict[str, Any]:
    """Add *new* game counts into an *existing* cache payload."""
    moves_map: dict[str, dict[str, int]] = {}
    for m in existing.get("moves", []):
        moves_map[m["uci"]] = {
            "white": m.get("white", 0),
            "draws": m.get("draws", 0),
            "black": m.get("black", 0),
        }
    for m in new.get("moves", []):
        if m["uci"] in moves_map:
            moves_map[m["uci"]]["white"] += m.get("white", 0)
            moves_map[m["uci"]]["draws"] += m.get("draws", 0)
            moves_map[m["uci"]]["black"] += m.get("black", 0)
        else:
            moves_map[m["uci"]] = {
                "white": m.get("white", 0),
                "draws": m.get("draws", 0),
                "black": m.get("black", 0),
            }

    return {
        "white": existing.get("white", 0) + new.get("white", 0),
        "draws": existing.get("draws", 0) + new.get("draws", 0),
        "black": existing.get("black", 0) + new.get("black", 0),
        "moves": [
            {
                "uci": uci,
                "white": counts["white"],
                "draws": counts["draws"],
                "black": counts["black"],
                "averageRating": 0,
            }
            for uci, counts in sorted(
                moves_map.items(),
                key=lambda kv: -(kv[1]["white"] + kv[1]["draws"] + kv[1]["black"]),
            )
        ],
    }


# ---------------------------------------------------------------------------
# Metadata helpers
# ---------------------------------------------------------------------------


def _backend_key(username: str, color: str, speeds: str) -> str:
    return f"lichess_player_{username.lower()}_{color}_{speeds}"


def _write_fetch_meta(cache: Cache, backend: str) -> None:
    """Record the current timestamp as the last successful fetch time."""
    ts_ms = int(time.time() * 1000)
    meta_key = f"_fetch_meta_{backend}"
    cache.set(meta_key, "meta", {"ts_ms": ts_ms})


def import_pgn_player(
    pgn_path: Path,
    username: str,
    color: str,
    cache: Cache,
    speeds: str = "blitz,rapid,classical",
    max_plies: int = 30,
    verbose: bool = True,
) -> int:
    """Index games from a local PGN file into the player cache.

    Useful when the player's Lichess account is inactive or you want
    to use OTB games obtained from chessgames.com, FIDE, TWIC, etc.
    The data is stored under the same cache key as ``fetch_player_games``
    so ``mysecond search --player <username>`` reads it transparently.
    """
    pgn_text = pgn_path.read_text(encoding="utf-8", errors="replace")
    if verbose:
        print(
            f"[import] Reading {pgn_path.name}  ({len(pgn_text):,} chars) "
            f"for {username} as {color} …",
            flush=True,
        )

    book = _build_book(pgn_text, color, max_plies, verbose=verbose)

    if not book:
        if verbose:
            print("[import] No positions extracted.")
        return 0

    backend = _backend_key(username, color, speeds)
    if verbose:
        print(
            f"[import] Writing {len(book):,} positions to cache …",
            flush=True,
        )

    _store_book(book, cache, backend, merge=False)
    _write_fetch_meta(cache, backend)

    if verbose:
        print(f"[import] Done. {len(book):,} positions cached for {username} ({color}).")

    return len(book)
