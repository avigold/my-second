"""Parse a repertoire PGN into a navigable tree for the React browser."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import chess
import chess.pgn


def parse_repertoire(pgn_path: str, color: str) -> dict[str, Any]:
    """Parse the repertoire PGN and return a nested tree suitable for the browser.

    Each node in the tree has:
        id            – unique path string (e.g. "0", "0.0", "0.0.1")
        parent_id     – parent node id, None for root
        fen           – FEN of the position after the move
        move_san      – SAN of the move leading here (None for root)
        move_uci      – UCI of the move (None for root)
        move_orig     – origin square e.g. "e2" (None for root)
        move_dest     – dest square e.g. "e4" (None for root)
        comment       – annotation string
        depth         – ply depth from root (0 = root)
        is_player_move – True if the player made the move to reach this node
        children      – list of child nodes (mainline first)

    Returns {"root": <nested node>, "color": color}.
    """
    path = Path(pgn_path)
    if not path.exists():
        return {}

    text = path.read_text(encoding="utf-8", errors="replace")
    buf = io.StringIO(text)
    try:
        game = chess.pgn.read_game(buf)
    except Exception:
        return {}

    if game is None:
        return {}

    player_color = chess.WHITE if color == "white" else chess.BLACK

    root_fen = game.headers.get("FEN", chess.STARTING_FEN)
    root_board = chess.Board(root_fen)

    root_node = {
        "id": "0",
        "parent_id": None,
        "fen": root_fen,
        "move_san": None,
        "move_uci": None,
        "move_orig": None,
        "move_dest": None,
        "comment": game.comment or "",
        "depth": 0,
        "is_player_move": False,
        "children": [],
    }

    _build_children(root_node, game, root_board, player_color, depth=0, id_prefix="0")

    return {"root": root_node, "color": color}


def _build_children(
    parent_dict: dict[str, Any],
    pgn_node: chess.pgn.GameNode,
    board: chess.Board,
    player_color: chess.Color,
    depth: int,
    id_prefix: str,
) -> None:
    """Recursively build child nodes from a PGN game node's variations."""
    for child_idx, variation in enumerate(pgn_node.variations):
        move = variation.move
        if move is None:
            continue

        child_id = f"{id_prefix}.{child_idx}"

        # The player who made this move is the side to move on `board`.
        is_player_move = board.turn == player_color

        try:
            san = board.san(move)
            uci = move.uci()
            orig = uci[:2]
            dest = uci[2:4]
        except Exception:
            continue

        new_board = board.copy()
        new_board.push(move)

        child_dict: dict[str, Any] = {
            "id": child_id,
            "parent_id": parent_dict["id"],
            "fen": new_board.fen(),
            "move_san": san,
            "move_uci": uci,
            "move_orig": orig,
            "move_dest": dest,
            "comment": variation.comment or "",
            "depth": depth + 1,
            "is_player_move": is_player_move,
            "children": [],
        }

        parent_dict["children"].append(child_dict)

        _build_children(child_dict, variation, new_board, player_color,
                        depth + 1, child_id)
