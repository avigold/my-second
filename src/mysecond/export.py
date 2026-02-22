"""PGN export for novelty candidates, compatible with ChessBase.

Format per game
---------------
Each novelty is exported as a separate PGN game.  The structure is:

  [standard headers]
  [Annotator "mysecond"]
  [White "player_name"] / [Black "player_name"]   (when --player is set)

  { Game-level comment: rank, score, novelty move in SAN, eval summary }

  1. <book move> 1... <book move> ...   (moves through theory, unannotated)
  <novelty move>! $146                  (ChessBase novelty NAG)
  { N | [%eval ±N.NN] | d20: … | d24: … | stability=… | pre=… | post=… | score=… }
  <continuation moves>                  (engine PV after the novelty)

  *

ChessBase-specific annotations
-------------------------------
* ``$146``  — ChessBase NAG for "theoretical novelty" (N).
* ``$1``    — good move (!), added when eval_cp >= 25 cp.
* ``$3``    — brilliant move (!!), added when eval_cp >= 75 cp.
* ``[%eval N.NN]`` inside a comment — drives the ChessBase engine bar.
"""

from __future__ import annotations

import datetime
from pathlib import Path

import chess
import chess.pgn

from .models import ScoredNovelty

# ChessBase NAG for theoretical novelty
_NAG_NOVELTY = 146
_NAG_GOOD = chess.pgn.NAG_GOOD_MOVE        # $1  (!)
_NAG_BRILLIANT = chess.pgn.NAG_BRILLIANT_MOVE  # $3  (!!)


def export_pgn(
    scored: list[ScoredNovelty],
    root_fen: str,
    out_path: Path,
    *,
    player_name: str | None = None,
    opponent_name: str | None = None,
) -> None:
    """Write all novelties to *out_path* as a multi-game PGN."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    today = datetime.date.today().strftime("%Y.%m.%d")

    with open(out_path, "w", encoding="utf-8") as fh:
        exporter = chess.pgn.FileExporter(fh)
        for rank, sn in enumerate(scored, start=1):
            game = _build_game(
                sn, root_fen, today, rank,
                player_name=player_name,
                opponent_name=opponent_name,
            )
            game.accept(exporter)
            fh.write("\n")


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _build_game(
    sn: ScoredNovelty,
    root_fen: str,
    today: str,
    rank: int,
    *,
    player_name: str | None = None,
    opponent_name: str | None = None,
) -> chess.pgn.Game:
    game = chess.pgn.Game()

    game.headers["Event"] = "MySecond Novelties"
    game.headers["Site"] = "?"
    game.headers["Date"] = today
    game.headers["Round"] = str(rank)
    game.headers["Result"] = "*"
    game.headers["Annotator"] = "mysecond"

    # Populate White/Black headers from player/opponent context when available.
    root_board = chess.Board(root_fen)
    _set_player_headers(game, root_board, player_name, opponent_name)

    game.setup(root_board)  # sets FEN / SetUp headers if non-starting position

    nov = sn.novelty

    # Summarise the novelty in SAN for the game-level comment.
    pre_board = root_board.copy()
    for uci in nov.book_moves:
        pre_board.push(chess.Move.from_uci(uci))
    novelty_san = pre_board.san(chess.Move.from_uci(nov.novelty_move))
    move_number = pre_board.fullmove_number
    turn_label = "" if pre_board.turn == chess.WHITE else "..."

    game.comment = (
        f"Rank {rank} | Score: {sn.score:.1f} | "
        f"Novelty: {move_number}.{turn_label}{novelty_san} (ply {nov.novelty_ply + 1}) | "
        f"Eval: {sn.eval_cp:+.0f}cp | "
        f"Stability: {sn.stability:.1f}cp | "
        f"Pre-novelty: {nov.pre_novelty_games} master games | "
        f"Post-novelty: {nov.post_novelty_games} master games"
    )
    if player_name and opponent_name:
        game.comment += f" | Prepared for: {player_name} vs {opponent_name}"

    # --- Book moves (unannotated) ---
    node: chess.pgn.GameNode = game
    for uci in nov.book_moves:
        move = chess.Move.from_uci(uci)
        node = node.add_variation(move)

    # --- Novelty move ---
    nov_move = chess.Move.from_uci(nov.novelty_move)
    node = node.add_variation(nov_move)

    # ChessBase novelty NAG ($146) plus quality NAG.
    node.nags.add(_NAG_NOVELTY)
    if sn.eval_cp >= 75:
        node.nags.add(_NAG_BRILLIANT)
    elif sn.eval_cp >= 25:
        node.nags.add(_NAG_GOOD)

    node.comment = _novelty_comment(sn)

    # --- Post-novelty engine PV ---
    nov_board = pre_board.copy()
    nov_board.push(nov_move)
    for cont_uci in sn.novelty.continuations:
        if nov_board.is_game_over():
            break
        cont_move = chess.Move.from_uci(cont_uci)
        if cont_move not in nov_board.legal_moves:
            break
        node = node.add_variation(cont_move)
        nov_board.push(cont_move)

    return game


def _set_player_headers(
    game: chess.pgn.Game,
    root_board: chess.Board,
    player_name: str | None,
    opponent_name: str | None,
) -> None:
    """Set White/Black headers based on player/opponent context."""
    white_label = "?"
    black_label = "?"

    if player_name and opponent_name:
        if root_board.turn == chess.WHITE:
            # First move is White's, so White = player (if player is White).
            # We don't know the side here — use generic labels.
            white_label = player_name
            black_label = opponent_name
        else:
            white_label = opponent_name
            black_label = player_name
    elif player_name:
        white_label = player_name
    elif opponent_name:
        black_label = opponent_name

    game.headers["White"] = white_label
    game.headers["Black"] = black_label


def _novelty_comment(sn: ScoredNovelty) -> str:
    """Build the detailed comment on the novelty move."""
    nov = sn.novelty

    # [%eval] annotation for ChessBase engine bar (white-relative).
    eval_annotation = _eval_annotation(nov)

    depth_parts = " | ".join(
        f"d{depth}: {ev.display()}"
        for depth, ev in sorted(nov.evals.items())
    )

    parts: list[str] = ["N"]
    if eval_annotation:
        parts.append(eval_annotation)
    parts += [
        depth_parts,
        f"stability={sn.stability:.1f}cp",
        f"pre={nov.pre_novelty_games}",
        f"post={nov.post_novelty_games}",
        f"score={sn.score:.1f}",
    ]
    return " | ".join(parts)


def _eval_annotation(nov) -> str:
    if not nov.evals:
        return ""
    last_depth = max(nov.evals.keys())
    ev = nov.evals[last_depth]
    if ev.mate_white is not None:
        sign = "+" if ev.mate_white > 0 else "-"
        return f"[%eval #{sign}{abs(ev.mate_white)}]"
    if ev.cp_white is not None:
        return f"[%eval {ev.cp_white / 100:+.2f}]"
    return ""
