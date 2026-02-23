"""Analyse game-phase statistics from a player's raw game history.

Downloads a sample of complete games and computes:
- Average game length per time control (blitz/rapid/classical etc.)
- Draw / decisive rates
- Endgame reach rate and conversion rate
- Draw rates broken down by middlegame vs endgame endings
"""

from __future__ import annotations

import io
from typing import Any

import chess
import chess.pgn

from .fetcher import download_raw_pgn

# Non-pawn material values for phase detection.
_PIECE_VALUES: dict[int, int] = {
    chess.QUEEN:  9,
    chess.ROOK:   5,
    chess.BISHOP: 3,
    chess.KNIGHT: 3,
}

# Combined non-pawn material (both sides) at or below this → endgame.
_ENDGAME_THRESHOLD = 20


def _non_pawn_material(board: chess.Board) -> int:
    total = 0
    for piece_type, value in _PIECE_VALUES.items():
        total += len(board.pieces(piece_type, chess.WHITE)) * value
        total += len(board.pieces(piece_type, chess.BLACK)) * value
    return total


def _parse_result(result: str, color: str) -> str:
    """Return 'win', 'loss', or 'draw' from the player's perspective."""
    if result == "1/2-1/2":
        return "draw"
    if result == "1-0":
        return "win" if color == "white" else "loss"
    if result == "0-1":
        return "win" if color == "black" else "loss"
    return "unknown"


def _speed_from_headers(headers: Any) -> str:
    """Classify a game's time control as bullet/blitz/rapid/classical/unknown."""
    event = headers.get("Event", "").lower()
    for speed in ("bullet", "blitz", "rapid", "classical", "correspondence"):
        if speed in event:
            return speed
    # Fall back to TimeControl header (e.g. "300+3", "600", "900+10")
    tc = headers.get("TimeControl", "")
    if tc and tc not in ("-", "?", ""):
        try:
            base = int(tc.split("+")[0].split("/")[-1])
            if base < 180:
                return "bullet"
            if base < 480:
                return "blitz"
            if base < 1500:
                return "rapid"
            return "classical"
        except (ValueError, IndexError):
            pass
    return "unknown"


def analyze_game_phases(
    username: str,
    color: str,
    platform: str,
    speeds: str = "blitz,rapid,classical",
    max_games: int = 200,
    verbose: bool = True,
) -> dict[str, Any]:
    """Download up to *max_games* and compute per-phase statistics.

    Returns
    -------
    dict with keys:
        total_games, avg_length_by_speed, draw_rate, decisive_rate,
        endgame_reach_rate, endgame_conversion_rate,
        draw_rate_middlegame, draw_rate_endgame
    """
    tag = f"[phases:{username}]"

    if verbose:
        print(f"{tag} Downloading up to {max_games} games for phase analysis ({platform}) …", flush=True)
    print(f"[progress:phases:{username}] 0/{max_games}", flush=True)

    try:
        pgn_text = download_raw_pgn(username, color, platform, speeds, max_games)
    except Exception as exc:
        if verbose:
            print(f"{tag} Download failed: {exc}", flush=True)
        return _empty_phases()

    if not pgn_text.strip():
        if verbose:
            print(f"{tag} No games returned.", flush=True)
        return _empty_phases()

    buf = io.StringIO(pgn_text)
    games_processed = 0

    draws = wins = losses = 0

    # Phase counters
    endgame_reached     = 0
    endgame_win         = 0
    endgame_loss        = 0
    endgame_draw        = 0
    middlegame_draw     = 0
    middlegame_decisive = 0

    # Per-speed plies accumulator: speed → (total_plies, game_count)
    speed_plies: dict[str, list[int]] = {}

    if verbose:
        print(f"{tag} Parsing games and computing phase statistics …", flush=True)

    while True:
        try:
            game = chess.pgn.read_game(buf)
        except Exception:
            continue
        if game is None:
            break

        result  = game.headers.get("Result", "*")
        outcome = _parse_result(result, color)
        if outcome == "unknown":
            continue

        speed = _speed_from_headers(game.headers)

        board = game.board()
        ply   = 0
        reached_endgame = False

        for move in game.mainline_moves():
            board.push(move)
            ply += 1
            if not reached_endgame and _non_pawn_material(board) <= _ENDGAME_THRESHOLD:
                reached_endgame = True

        games_processed += 1
        speed_plies.setdefault(speed, []).append(ply)

        if outcome == "draw":
            draws += 1
        elif outcome == "win":
            wins += 1
        else:
            losses += 1

        if reached_endgame:
            endgame_reached += 1
            if outcome == "draw":
                endgame_draw += 1
            elif outcome == "win":
                endgame_win += 1
            else:
                endgame_loss += 1
        else:
            # Game ended before reaching endgame threshold — classify as middlegame
            if outcome == "draw":
                middlegame_draw += 1
            else:
                middlegame_decisive += 1

        if games_processed % 10 == 0:
            eg_pct = f"{endgame_reached / games_processed * 100:.0f}%" if games_processed else "0%"
            if verbose:
                print(
                    f"{tag} {games_processed} games — "
                    f"{wins}W / {draws}D / {losses}L — "
                    f"endgame reached: {eg_pct}",
                    flush=True,
                )
            print(f"[progress:phases:{username}] {games_processed}/{max_games}", flush=True)

    print(f"[progress:phases:{username}] {games_processed}/{max_games}", flush=True)

    if verbose:
        print(
            f"{tag} Complete: {games_processed} games analysed "
            f"({wins}W / {draws}D / {losses}L).",
            flush=True,
        )
        by_speed_summary = ", ".join(
            f"{s}: {len(ps)}g" for s, ps in sorted(speed_plies.items())
        )
        if by_speed_summary:
            print(f"{tag} Time controls: {by_speed_summary}", flush=True)

    if games_processed == 0:
        return _empty_phases()

    total        = games_processed
    draw_rate    = draws / total

    endgame_decisive   = endgame_win + endgame_loss
    endgame_conversion = (endgame_win / endgame_decisive) if endgame_decisive > 0 else 0.0
    endgame_draw_rate  = (endgame_draw / endgame_reached) if endgame_reached > 0 else 0.0

    middlegame_total     = middlegame_draw + middlegame_decisive
    middlegame_draw_rate = (middlegame_draw / middlegame_total) if middlegame_total > 0 else 0.0

    # Average game length (in full moves) broken down by time control.
    avg_length_by_speed = {
        s: round(sum(plies) / len(plies) / 2, 1)
        for s, plies in sorted(speed_plies.items())
        if plies
    }

    return {
        "total_games":             games_processed,
        "avg_length_by_speed":     avg_length_by_speed,
        "draw_rate":               round(draw_rate, 3),
        "decisive_rate":           round(1 - draw_rate, 3),
        "endgame_reach_rate":      round(endgame_reached / total, 3),
        "endgame_conversion_rate": round(endgame_conversion, 3),
        "draw_rate_middlegame":    round(middlegame_draw_rate, 3),
        "draw_rate_endgame":       round(endgame_draw_rate, 3),
    }


def _empty_phases() -> dict[str, Any]:
    return {
        "total_games":             0,
        "avg_length_by_speed":     {},
        "draw_rate":               0.0,
        "decisive_rate":           0.0,
        "endgame_reach_rate":      0.0,
        "endgame_conversion_rate": 0.0,
        "draw_rate_middlegame":    0.0,
        "draw_rate_endgame":       0.0,
    }
