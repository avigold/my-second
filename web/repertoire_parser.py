"""Parse a repertoire PGN into a navigable tree for the React browser."""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Any

import chess
import chess.pgn


# ---------------------------------------------------------------------------
# Comment parsing
# ---------------------------------------------------------------------------

_FREQ_RE = re.compile(r'(\d+)/(\d+)\s+games\s+\((\d+)%\)')
_WDL_RE  = re.compile(r'W:(\d+)\s+D:(\d+)\s+L:(\d+)')


def _parse_freq(comment: str) -> dict[str, Any] | None:
    """Parse '42/67 games (63%) W:30 D:25 L:12' into a structured dict.

    Returns None if no frequency data is present.
    """
    m = _FREQ_RE.search(comment)
    if not m:
        return None
    result: dict[str, Any] = {
        "games":  int(m.group(1)),
        "total":  int(m.group(2)),
        "pct":    int(m.group(3)),
        "wins":   None,
        "draws":  None,
        "losses": None,
    }
    w = _WDL_RE.search(comment)
    if w:
        result["wins"]   = int(w.group(1))
        result["draws"]  = int(w.group(2))
        result["losses"] = int(w.group(3))
    return result


# ---------------------------------------------------------------------------
# Tree stats
# ---------------------------------------------------------------------------

def _compute_tree_stats(root: dict[str, Any]) -> dict[str, Any]:
    """Walk the tree and compute aggregate statistics."""
    total = player = opponent = leaves = 0
    max_depth = 0

    # quality_by_depth: depth -> {wins, draws, losses, games}
    # Only populated for player-move nodes that have WDL data.
    quality_acc: dict[int, dict[str, int]] = {}

    stack = [root]
    while stack:
        node = stack.pop()
        total += 1
        d = node["depth"]
        if d > max_depth:
            max_depth = d
        if node["is_player_move"]:
            player += 1
            freq = node.get("freq")
            if freq and freq.get("wins") is not None:
                if d not in quality_acc:
                    quality_acc[d] = {"wins": 0, "draws": 0, "losses": 0}
                quality_acc[d]["wins"]   += freq["wins"]
                quality_acc[d]["draws"]  += freq["draws"]
                quality_acc[d]["losses"] += freq["losses"]
        elif node["move_san"]:
            opponent += 1
        if not node["children"]:
            leaves += 1
        stack.extend(node["children"])

    # Convert accumulated WDL to a score (0–100) per depth.
    # score = (wins + 0.5 * draws) / total * 100
    quality_by_depth: dict[int, dict[str, Any]] = {}
    for depth, q in sorted(quality_acc.items()):
        wdl_total = q["wins"] + q["draws"] + q["losses"]
        if wdl_total > 0:
            score = (q["wins"] + q["draws"] * 0.5) / wdl_total * 100
            quality_by_depth[depth] = {
                "score":  round(score, 1),
                "wins":   q["wins"],
                "draws":  q["draws"],
                "losses": q["losses"],
                "total":  wdl_total,
            }

    return {
        "total_positions":  total,
        "player_moves":     player,
        "opponent_moves":   opponent,
        "leaf_count":       leaves,
        "max_depth":        max_depth,
        "quality_by_depth": quality_by_depth,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

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
        comment       – raw annotation string
        freq          – parsed frequency dict or None
        depth         – ply depth from root (0 = root)
        is_player_move – True if the player made the move to reach this node
        children      – list of child nodes (mainline first)

    Returns {"root": <nested node>, "color": color, "tree_stats": <stats dict>}.
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

    root_node: dict[str, Any] = {
        "id":            "0",
        "parent_id":     None,
        "fen":           root_fen,
        "move_san":      None,
        "move_uci":      None,
        "move_orig":     None,
        "move_dest":     None,
        "comment":       game.comment or "",
        "freq":          None,
        "depth":         0,
        "is_player_move": False,
        "children":      [],
    }

    _build_children(root_node, game, root_board, player_color, depth=0, id_prefix="0")

    tree_stats = _compute_tree_stats(root_node)

    return {"root": root_node, "color": color, "tree_stats": tree_stats}


# ---------------------------------------------------------------------------
# Tree builder
# ---------------------------------------------------------------------------

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
            san  = board.san(move)
            uci  = move.uci()
            orig = uci[:2]
            dest = uci[2:4]
        except Exception:
            continue

        new_board = board.copy()
        new_board.push(move)

        comment = variation.comment or ""
        child_dict: dict[str, Any] = {
            "id":            child_id,
            "parent_id":     parent_dict["id"],
            "fen":           new_board.fen(),
            "move_san":      san,
            "move_uci":      uci,
            "move_orig":     orig,
            "move_dest":     dest,
            "comment":       comment,
            "freq":          _parse_freq(comment) if is_player_move else None,
            "depth":         depth + 1,
            "is_player_move": is_player_move,
            "children":      [],
        }

        parent_dict["children"].append(child_dict)

        _build_children(child_dict, variation, new_board, player_color,
                        depth + 1, child_id)
