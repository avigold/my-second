"""Parse mysecond output PGN files into structured JSON for the novelty browser."""

from __future__ import annotations

import re
from pathlib import Path

import chess
import chess.pgn


# Regexes for extracting fields from PGN comments.
_RE_SCORE      = re.compile(r"Score:\s*([\d.]+)")
_RE_EVAL_CP    = re.compile(r"Eval:\s*([+-]?\d+)cp")
_RE_STABILITY  = re.compile(r"Stability:\s*([\d.]+)cp")
_RE_PRE        = re.compile(r"Pre-novelty:\s*(\d+)")
_RE_POST       = re.compile(r"Post-novelty:\s*(\d+)")
_RE_DEPTH_EVAL = re.compile(r"d(\d+):\s*([+\-M\d.]+)")


def parse_novelties(pgn_path: str | Path, root_fen: str, side: str) -> list[dict]:
    """Return a list of novelty dicts suitable for JSON serialisation.

    Parameters
    ----------
    pgn_path:
        Path to the output PGN file produced by ``mysecond search``.
    root_fen:
        The starting FEN for the search (from ``job.params``).
    side:
        ``'white'`` or ``'black'`` — the side being prepared for (board orientation).
    """
    path = Path(pgn_path)
    if not path.exists():
        return []

    results: list[dict] = []
    with open(path, encoding="utf-8") as fh:
        while True:
            game = chess.pgn.read_game(fh)
            if game is None:
                break
            entry = _parse_game(game, root_fen, side)
            if entry is not None:
                results.append(entry)
    return results


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _parse_game(game: chess.pgn.Game, root_fen: str, side: str) -> dict | None:
    """Extract novelty data from a single PGN game."""
    rank = int(game.headers.get("Round", 0))
    game_comment = game.comment or ""

    # Parse game-level comment for summary fields.
    eval_cp   = _float(  _RE_EVAL_CP.search(game_comment))
    stability = _float(  _RE_STABILITY.search(game_comment))
    score     = _float(  _RE_SCORE.search(game_comment))
    pre_games = _int(    _RE_PRE.search(game_comment))
    post_games = _int(   _RE_POST.search(game_comment))

    # Walk the main line to find the novelty node ($146 NAG).
    root_board = chess.Board(root_fen)
    node = game
    board = root_board.copy()
    book_moves_san: list[str] = []
    novelty_node = None

    while node.variations:
        next_node = node.variations[0]
        if 146 in next_node.nags:
            novelty_node = next_node
            break
        # Book move.
        san = board.san(next_node.move)
        book_moves_san.append(san)
        board.push(next_node.move)
        node = next_node

    if novelty_node is None:
        return None

    # Position before novelty.
    fen_before = board.fen()
    nov_move   = novelty_node.move
    novelty_san = board.san(nov_move)

    # Squares for chessground lastMove highlight.
    novelty_orig = chess.square_name(nov_move.from_square)
    novelty_dest = chess.square_name(nov_move.to_square)

    # Position after novelty.
    board.push(nov_move)
    fen_after = board.fen()

    # Per-depth evals from the novelty move comment.
    nov_comment = novelty_node.comment or ""
    depth_evals = {
        int(m.group(1)): m.group(2)
        for m in _RE_DEPTH_EVAL.finditer(nov_comment)
    }

    # Continuation moves in SAN.
    continuations_san: list[str] = []
    cont_node = novelty_node
    while cont_node.variations:
        cont_node = cont_node.variations[0]
        if cont_node.move is None:
            break
        continuations_san.append(board.san(cont_node.move))
        board.push(cont_node.move)

    return {
        "rank":             rank,
        "novelty_san":      novelty_san,
        "novelty_orig":     novelty_orig,
        "novelty_dest":     novelty_dest,
        "fen_before":       fen_before,
        "fen_after":        fen_after,
        "book_moves_san":   book_moves_san,
        "continuations_san": continuations_san,
        "depth_evals":      depth_evals,
        "eval_cp":          eval_cp,
        "stability":        stability,
        "score":            score,
        "pre_novelty_games":  pre_games,
        "post_novelty_games": post_games,
        "side":             side,
    }


def _float(m: re.Match | None) -> float:
    return float(m.group(1)) if m else 0.0


def _int(m: re.Match | None) -> int:
    return int(m.group(1)) if m else 0
