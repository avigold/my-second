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
from datetime import datetime, timezone
from pathlib import Path

import chess
import requests

from .cache import Cache
from .eval_cache import EvalCache
from .fetcher import _backend_key, fetch_player_games, fetch_player_games_chesscom
from .game_phases import analyze_game_phases
from .habits import analyze_habits
from .strategise import _build_opening_lines, _compute_style_profile

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
    white_book_out: Path | None = None,
    black_book_out: Path | None = None,
    profile_out: Path | None = None,
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

    # Known stages: fetch + habits for each color, then elo + write.
    # Use 100 sub-units per stage so we can show smooth sub-progress.
    n_stages = len(colors) * 2 + 2
    SCALE = 100
    total_scaled = n_stages * SCALE

    def _emit(scaled_n: int) -> None:
        print(f"[progress:train-bot] {scaled_n}/{total_scaled}", flush=True)

    def _stage_progress_fn(stage_idx: int):
        """Return a callback that maps sub-step (n, total) into the global scale."""
        base = stage_idx * SCALE
        def fn(n: int, total: int) -> None:
            sub = round(n / total * SCALE) if total > 0 else SCALE
            _emit(base + sub)
        return fn

    stage = 0
    _emit(0)

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
                show_progress=False,
                progress_fn=_stage_progress_fn(stage),
            )
        else:
            fetch_player_games(
                username=opponent_username,
                color=color,
                cache=cache,
                speeds=speeds,
                verbose=verbose,
            )
        stage += 1
        _emit(stage * SCALE)

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
            show_progress=False,
            progress_fn=_stage_progress_fn(stage),
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
        stage += 1
        _emit(stage * SCALE)

    # ------------------------------------------------------------------
    # Step 3: Fetch Elo.
    # ------------------------------------------------------------------
    if verbose:
        print(f"{tag} Fetching Elo from {opponent_platform} …", flush=True)
    elo = _fetch_elo(opponent_username, opponent_platform, speeds)
    if verbose:
        print(f"{tag} Elo: {elo}", flush=True)
    stage += 1
    _emit(stage * SCALE)

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

    # ------------------------------------------------------------------
    # Step 5 (optional): Export slim opening-book JSON files.
    # ------------------------------------------------------------------
    for color in colors:
        book_out = white_book_out if color == "white" else black_book_out
        if book_out is None:
            continue
        backend = _backend_key(opponent_username, color, speeds, platform=opponent_platform)
        entries = cache.scan_backend(backend)
        positions: dict = {}
        for fen, payload in entries:
            moves = [
                {"uci": m["uci"], "games": m.get("white", 0) + m.get("draws", 0) + m.get("black", 0)}
                for m in payload.get("moves", [])
                if m.get("white", 0) + m.get("draws", 0) + m.get("black", 0) >= 3
            ]
            if moves:
                positions[fen] = moves
        book_out.parent.mkdir(parents=True, exist_ok=True)
        book_out.write_text(
            json.dumps({"positions": positions}, separators=(",", ":")),
            encoding="utf-8",
        )
        if verbose:
            print(f"{tag} Opening book ({color}) written to {book_out} "
                  f"({len(positions)} positions)", flush=True)

    stage += 1
    _emit(stage * SCALE)

    if verbose:
        print(f"{tag} Bot model written to {out_path}", flush=True)

    # ------------------------------------------------------------------
    # Step 6 (optional): Export player profile JSON.
    # ------------------------------------------------------------------
    if profile_out is not None:
        if verbose:
            print(f"{tag} Computing player profile …", flush=True)

        # Style profiles from opening cache.
        style: dict = {}
        start_norm = " ".join(chess.STARTING_FEN.split()[:3]) + " -"
        for color in colors:
            backend = _backend_key(opponent_username, color, speeds, platform=opponent_platform)
            entries = cache.scan_backend(backend)
            cache_index: dict = {fen: payload for fen, payload in entries}
            profile_style = _compute_style_profile(cache_index, color)

            # First-move distribution.
            # White: read from the starting-position cache entry (White moves first).
            # Black: aggregate Black's responses across all legal White first moves,
            #        because the starting FEN (White to move) is absent from the
            #        Black cache — that cache only stores positions where Black moves.
            board0 = chess.Board()
            first_move_dist: list[dict] = []
            if color == "white":
                start_payload = cache_index.get(start_norm, {})
                root_moves = start_payload.get("moves", [])
                total_root = sum(
                    m.get("white", 0) + m.get("draws", 0) + m.get("black", 0)
                    for m in root_moves
                ) or 1
                for m in sorted(
                    root_moves,
                    key=lambda x: -(x.get("white", 0) + x.get("draws", 0) + x.get("black", 0)),
                )[:5]:
                    g = m.get("white", 0) + m.get("draws", 0) + m.get("black", 0)
                    try:
                        san = board0.san(chess.Move.from_uci(m["uci"]))
                    except Exception:
                        san = m["uci"]
                    first_move_dist.append({
                        "uci": m["uci"],
                        "san": san,
                        "games": g,
                        "pct": round(g / total_root, 3),
                    })
            else:
                # For Black: enumerate all legal White first moves, find matching
                # positions in the cache, and aggregate Black's responses.
                response_totals: dict[str, dict] = {}
                for white_move in board0.legal_moves:
                    b1 = board0.copy()
                    b1.push(white_move)
                    nfen1 = " ".join(b1.fen().split()[:3]) + " -"
                    payload1 = cache_index.get(nfen1, {})
                    if not payload1:
                        continue
                    for m in payload1.get("moves", []):
                        g = m.get("white", 0) + m.get("draws", 0) + m.get("black", 0)
                        if g == 0:
                            continue
                        try:
                            san = b1.san(chess.Move.from_uci(m["uci"]))
                        except Exception:
                            san = m["uci"]
                        if san not in response_totals:
                            response_totals[san] = {"uci": m["uci"], "san": san, "games": 0}
                        response_totals[san]["games"] += g
                sorted_responses = sorted(response_totals.values(), key=lambda x: -x["games"])[:5]
                total_resp = sum(x["games"] for x in sorted_responses) or 1
                for x in sorted_responses:
                    first_move_dist.append({**x, "pct": round(x["games"] / total_resp, 3)})
            profile_style["first_move_distribution"] = first_move_dist
            profile_style["top_openings"] = _build_opening_lines(cache_index, color, top_n=10)
            style[color] = profile_style

        # Total games indexed (sum at starting position, both colors).
        total_games = 0
        for color in colors:
            backend = _backend_key(opponent_username, color, speeds, platform=opponent_platform)
            entries = cache.scan_backend(backend)
            for fen, payload in entries:
                if fen == start_norm:
                    total_games += (
                        payload.get("white", 0)
                        + payload.get("draws", 0)
                        + payload.get("black", 0)
                    )
                    break

        # Game phase stats (downloads raw PGNs; use first color only to avoid double-counting).
        phase_stats = analyze_game_phases(
            username=opponent_username,
            color=colors[0],
            platform=opponent_platform,
            speeds=speeds,
            verbose=verbose,
        )

        # Top habits summary.
        habits_summary: dict = {"white_count": 0, "black_count": 0, "top_white": [], "top_black": []}
        for color in colors:
            key = f"habits_{color}"
            habits_list = habits_by_color.get(color, [])
            count_key = f"{color}_count"
            top_key = f"top_{color}"
            habits_summary[count_key] = len(habits_list)
            habits_summary[top_key] = habits_list[:3]

        avatar_url = _fetch_avatar_url(opponent_username, opponent_platform or "lichess")

        profile_data = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total_games": total_games,
            "phase_stats": phase_stats,
            "habits": habits_summary,
            "avatar_url": avatar_url,
        }
        for color in colors:
            profile_data[f"style_{color}"] = style.get(color, {})

        profile_out.parent.mkdir(parents=True, exist_ok=True)
        profile_out.write_text(json.dumps(profile_data, indent=2), encoding="utf-8")
        if verbose:
            print(f"{tag} Player profile written to {profile_out}", flush=True)

    if verbose:
        print(f"{tag} Done.", flush=True)


def _fetch_avatar_url(username: str, platform: str) -> str | None:
    """Fetch the player's avatar URL from their platform API."""
    headers = {"User-Agent": "mysecond/0.1.0"}
    try:
        if platform in ("chesscom", "chess.com"):
            resp = requests.get(
                f"https://api.chess.com/pub/player/{username.lower()}",
                headers=headers, timeout=5,
            )
            if resp.status_code == 200:
                return resp.json().get("avatar")
        elif platform == "lichess":
            resp = requests.get(
                f"https://lichess.org/api/user/{username.lower()}",
                headers=headers, timeout=5,
            )
            if resp.status_code == 200:
                return resp.json().get("profile", {}).get("imageUrl")
    except Exception:
        pass
    return None


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
