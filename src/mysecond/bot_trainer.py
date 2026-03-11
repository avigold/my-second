"""Train a bot that mimics a specific chess player's opening repertoire and habits.

How it works
------------
1. Fetch the opponent's games (for the specified colors) from Lichess/Chess.com
   into the local opening cache.
2. Run habit analysis to find positions where the opponent consistently plays
   a suboptimal move.
3. Fetch the opponent's Elo from the platform API.
4. Write a compact JSON "bot model" that the web server uses to select moves:
   - cache_backend_white / cache_backend_black: keys for opening-cache lookup
   - habits_white / habits_black: list of {fen, player_move_uci, games, total}
   - opponent_elo: integer rating (clamped to a reasonable Elo range)

The opening repertoire is NOT stored in the JSON — it is queried live from the
SQLite cache keyed by (fen, backend).  This keeps the model small and avoids
duplicating data that is already cached.
"""

from __future__ import annotations

import json
from pathlib import Path

import requests

from .cache import Cache
from .eval_cache import EvalCache
from .fetcher import _backend_key, fetch_player_games, fetch_player_games_chesscom
from .habits import analyze_habits

_LICHESS_USER_URL = "https://lichess.org/api/user/{username}"
_CHESSCOM_STATS_URL = "https://api.chess.com/pub/player/{username}/stats"
_ELO_MIN = 1100
_ELO_MAX = 3000
_SPEED_TO_CHESSCOM = {
    "blitz": "chess_blitz",
    "rapid": "chess_rapid",
    "bullet": "chess_bullet",
    "daily": "chess_daily",
    "classical": "chess_rapid",   # no classical on Chess.com — fall back to rapid
}


def train_bot(
    opponent_username: str,
    opponent_platform: str,
    speeds: str,
    colors: list[str],
    cache: Cache,
    engine_path: Path,
    out_path: Path,
    verbose: bool = True,
    eval_cache: EvalCache | None = None,
) -> None:
    """Train a bot that mimics the given opponent.

    Parameters
    ----------
    opponent_username:
        Chess platform username of the player to mimic.
    opponent_platform:
        ``'lichess'`` or ``'chesscom'``.
    speeds:
        Comma-separated time controls (e.g. ``'blitz,rapid'``).
    colors:
        Which sides to train — typically ``['white', 'black']``.
    cache:
        Shared opening-book cache.
    engine_path:
        Path to the Stockfish binary.
    out_path:
        JSON file to write the bot model to.
    verbose:
        Print progress messages.
    eval_cache:
        Optional eval cache to speed up habit analysis.
    """
    tag = f"[train-bot:{opponent_username}]"

    # ------------------------------------------------------------------
    # Step 1: Fetch games for each color.
    # ------------------------------------------------------------------
    for color in colors:
        if verbose:
            print(
                f"{tag} Fetching {color} games from {opponent_platform} ({speeds}) …",
                flush=True,
            )
        if opponent_platform == "chesscom":
            fetch_player_games_chesscom(
                username=opponent_username,
                color=color,
                cache=cache,
                speeds=speeds,
                verbose=verbose,
            )
        else:
            fetch_player_games(
                username=opponent_username,
                color=color,
                cache=cache,
                speeds=speeds,
                verbose=verbose,
            )

    # ------------------------------------------------------------------
    # Step 2: Analyse habits for each color.
    # ------------------------------------------------------------------
    habits_by_color: dict[str, list[dict]] = {}
    for color in colors:
        if verbose:
            print(f"{tag} Analysing {color} habits …", flush=True)
        habits = analyze_habits(
            username=opponent_username,
            color=color,
            cache=cache,
            engine_path=engine_path,
            speeds=speeds,
            platform=opponent_platform,
            verbose=verbose,
            eval_cache=eval_cache,
        )
        habits_by_color[color] = [
            {
                "fen": h.fen,
                "player_move_uci": h.player_move_uci,
                "games": h.player_move_games,
                "total": h.total_games,
            }
            for h in habits
        ]
        if verbose:
            print(
                f"{tag} {len(habits_by_color[color])} habit inaccuracies for {color}.",
                flush=True,
            )

    # ------------------------------------------------------------------
    # Step 3: Fetch Elo.
    # ------------------------------------------------------------------
    if verbose:
        print(f"{tag} Fetching Elo from {opponent_platform} …", flush=True)
    elo = _fetch_elo(opponent_username, opponent_platform, speeds)
    if verbose:
        print(f"{tag} Elo: {elo}", flush=True)

    # ------------------------------------------------------------------
    # Step 4: Build and write the bot model JSON.
    # ------------------------------------------------------------------
    model: dict = {
        "opponent_username": opponent_username,
        "opponent_platform": opponent_platform,
        "opponent_elo": elo,
        "speeds": speeds,
    }
    for color in colors:
        backend = _backend_key(
            opponent_username, color, speeds, platform=opponent_platform
        )
        model[f"cache_backend_{color}"] = backend
        model[f"habits_{color}"] = habits_by_color.get(color, [])

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(model, indent=2), encoding="utf-8")

    if verbose:
        print(f"{tag} Bot model written to {out_path}", flush=True)
        print(f"{tag} Done.", flush=True)


def _fetch_elo(username: str, platform: str, speeds: str) -> int | None:
    """Fetch the player's Elo from the platform API.

    Returns the highest rating across all specified speeds, or None on failure.
    Returned value is clamped to [1100, 3000].
    """
    speed_list = [s.strip().lower() for s in speeds.split(",") if s.strip()]
    headers = {"User-Agent": "mysecond/0.1.0"}
    try:
        if platform == "lichess":
            resp = requests.get(
                _LICHESS_USER_URL.format(username=username),
                headers=headers,
                timeout=10,
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            perfs = data.get("perfs", {})
            ratings = [
                perfs[s]["rating"]
                for s in speed_list
                if s in perfs and perfs[s].get("rating")
            ]
            if not ratings:
                # Fall back to any available time control.
                for alt in ("blitz", "rapid", "bullet", "classical"):
                    r = perfs.get(alt, {}).get("rating")
                    if r:
                        ratings = [r]
                        break
        else:
            resp = requests.get(
                _CHESSCOM_STATS_URL.format(username=username),
                headers=headers,
                timeout=10,
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            ratings = [
                data[_SPEED_TO_CHESSCOM[s]]["last"]["rating"]
                for s in speed_list
                if s in _SPEED_TO_CHESSCOM
                and _SPEED_TO_CHESSCOM[s] in data
                and data[_SPEED_TO_CHESSCOM[s]].get("last", {}).get("rating")
            ]
            if not ratings:
                for alt_key in ("chess_blitz", "chess_rapid", "chess_bullet"):
                    r = data.get(alt_key, {}).get("last", {}).get("rating")
                    if r:
                        ratings = [r]
                        break
    except requests.RequestException:
        return None

    if not ratings:
        return None
    return max(_ELO_MIN, min(_ELO_MAX, max(ratings)))
