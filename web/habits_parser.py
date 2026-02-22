"""Parse the annotated habits PGN output for the web browser."""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Any

import chess
import chess.pgn


def parse_habits(pgn_path: str) -> list[dict[str, Any]]:
    """Parse the habits PGN file and return a list of dicts for the React browser.

    Each dict contains all fields needed to display a position on the board
    and explain the inaccuracy to the user.
    """
    path = Path(pgn_path)
    if not path.exists():
        return []

    text = path.read_text(encoding="utf-8", errors="replace")
    buf = io.StringIO(text)
    results: list[dict[str, Any]] = []
    rank = 0

    while True:
        try:
            game = chess.pgn.read_game(buf)
        except Exception:
            continue
        if game is None:
            break

        rank += 1
        fen = game.headers.get("FEN", chess.STARTING_FEN)

        # Parse root comment for score / gap / freq.
        root_comment = game.comment or ""
        score = _parse_float(root_comment, r"score=([\d.]+)")
        eval_gap = _parse_float(root_comment, r"gap=([+-]?[\d.]+)")
        freq = _parse_int(root_comment, r"freq=(\d+)")

        # Main variation: player's habitual move.
        main_node = game.next()
        if main_node is None:
            continue

        player_move_uci = main_node.move.uci() if main_node.move else ""
        player_move_san = main_node.san() if main_node.move else ""
        player_comment = main_node.comment or ""

        # Extract player eval from comment.
        player_eval = _parse_float(player_comment, r"eval ([+-]?[\d.]+)")
        player_eval_cp = player_eval * 100 if player_eval is not None else 0.0

        # Alternative variation: engine's best move (first variation off root).
        best_node = None
        for variation in game.variations:
            if variation != main_node:
                best_node = variation
                break
        # Also check variations off the game root (chess.pgn stores them differently).
        if best_node is None and len(game.variations) > 1:
            best_node = game.variations[1]

        best_move_uci = ""
        best_move_san = ""
        best_eval_cp = 0.0
        if best_node is not None and best_node.move:
            best_move_uci = best_node.move.uci()
            best_move_san = best_node.san() if best_node.move else ""
            best_comment = best_node.comment or ""
            best_eval = _parse_float(best_comment, r"eval ([+-]?[\d.]+)")
            best_eval_cp = best_eval * 100 if best_eval is not None else 0.0

        # Compute orig/dest squares for chessground highlighting.
        player_orig, player_dest = _uci_to_squares(player_move_uci)
        best_orig, best_dest = _uci_to_squares(best_move_uci)

        results.append({
            "rank": rank,
            "fen": fen,
            "total_games": freq or 0,
            "player_move_uci": player_move_uci,
            "player_move_san": player_move_san,
            "player_move_orig": player_orig,
            "player_move_dest": player_dest,
            "best_move_uci": best_move_uci,
            "best_move_san": best_move_san,
            "best_move_orig": best_orig,
            "best_move_dest": best_dest,
            "eval_cp": best_eval_cp,
            "player_eval_cp": player_eval_cp,
            "eval_gap_cp": eval_gap or 0.0,
            "score": score or 0.0,
        })

    return results


def _parse_float(text: str, pattern: str) -> float | None:
    m = re.search(pattern, text)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return None


def _parse_int(text: str, pattern: str) -> int | None:
    m = re.search(pattern, text)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    return None


def _uci_to_squares(uci: str) -> tuple[str, str]:
    if len(uci) >= 4:
        return uci[:2], uci[2:4]
    return "", ""
