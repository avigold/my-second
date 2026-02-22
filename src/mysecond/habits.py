"""Identify positions where a player habitually plays suboptimal moves.

How it works
------------
1. Scan all positions in the player's opening cache.
2. Filter to positions where:
   - The player has reached the position at least ``min_games`` times.
   - The player has a dominant move (played in ≥ 40 % of those games).
3. For each qualifying position, use Stockfish to evaluate:
   - The best move the engine recommends.
   - The player's most-played move.
4. Compute the centipawn gap (best − player's move, from the player's POV).
5. Discard gaps below ``min_eval_gap`` centipawns (not a meaningful mistake).
6. Score = ``total_games × eval_gap / 100`` so frequently-reached positions
   with large mistakes rank highest.
7. Return the top ``max_positions`` inaccuracies sorted by score.

Output
------
An annotated PGN where each game represents one problem position.  Each game
starts at the problem FEN, shows the player's habitual move with a ``?!`` or
``?`` nag, and includes a variation with the engine's best move.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import chess
import chess.pgn

from .cache import Cache
from .engine import Engine
from .fetcher import _backend_key


@dataclass
class HabitInaccuracy:
    fen: str
    total_games: int         # times the player reached this position
    player_move_uci: str
    player_move_san: str
    player_move_games: int   # how often they chose this move
    best_move_uci: str
    best_move_san: str
    eval_cp: float           # eval after best move (player's POV, centipawns)
    player_eval_cp: float    # eval after player's move (player's POV, centipawns)
    eval_gap_cp: float       # eval_cp − player_eval_cp  (positive → player's move is worse)
    score: float             # ranking score = total_games × eval_gap / 100
    depth_evals: dict[int, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def analyze_habits(
    username: str,
    color: str,
    cache: Cache,
    engine_path: Path,
    speeds: str = "blitz,rapid,classical",
    platform: str = "lichess",
    min_games: int = 5,
    max_positions: int = 50,
    min_eval_gap: int = 25,
    depth: int = 20,
    verbose: bool = True,
) -> list[HabitInaccuracy]:
    """Find positions where the player habitually plays a suboptimal move.

    Parameters
    ----------
    username:
        Player username (used to look up the cache key).
    color:
        ``'white'`` or ``'black'``.
    cache:
        Shared :class:`~mysecond.cache.Cache` instance.
    speeds:
        Time controls used when the cache was populated (must match the
        ``fetch-player-games`` run).
    min_games:
        Minimum times the player must have reached a position for it to count.
    max_positions:
        Return at most this many inaccuracies (top by score).
    min_eval_gap:
        Minimum centipawn gap between best move and player's move to flag as
        an inaccuracy.
    depth:
        Stockfish search depth for each position.
    verbose:
        Print progress messages.

    Returns
    -------
    list[HabitInaccuracy]
        Sorted by score descending.
    """
    backend = _backend_key(username, color, speeds, platform=platform)
    player_color = chess.WHITE if color == "white" else chess.BLACK

    if verbose:
        print(f"[habits] Scanning cache for {username} ({color}, {speeds}) …", flush=True)

    all_positions = cache.scan_backend(backend)

    if verbose:
        print(f"[habits] {len(all_positions)} cached positions found.", flush=True)

    # Filter to positions with enough games and a dominant move.
    candidates: list[tuple[str, dict[str, Any]]] = []
    for fen, payload in all_positions:
        total = payload.get("white", 0) + payload.get("draws", 0) + payload.get("black", 0)
        if total < min_games:
            continue
        moves = payload.get("moves", [])
        if not moves:
            continue
        top_move = moves[0]
        top_games = top_move.get("white", 0) + top_move.get("draws", 0) + top_move.get("black", 0)
        if top_games / total < 0.40:
            continue
        candidates.append((fen, payload))

    if verbose:
        print(
            f"[habits] {len(candidates)} positions pass frequency filter "
            f"(≥{min_games} games, dominant move ≥40%). Evaluating …",
            flush=True,
        )

    results: list[HabitInaccuracy] = []

    with Engine(engine_path) as eng:
        for i, (fen, payload) in enumerate(candidates, 1):
            total = payload.get("white", 0) + payload.get("draws", 0) + payload.get("black", 0)
            top_move = payload["moves"][0]
            top_games = top_move.get("white", 0) + top_move.get("draws", 0) + top_move.get("black", 0)
            player_uci = top_move["uci"]

            try:
                board = chess.Board(fen)
            except ValueError:
                continue

            # Verify it's the player's turn.
            if board.turn != player_color:
                continue

            player_move = chess.Move.from_uci(player_uci)
            if player_move not in board.legal_moves:
                continue

            # Engine analysis at this position.
            info = eng.analyse_single(board, depth=depth)
            if "pv" not in info or not info["pv"]:
                continue

            best_move = info["pv"][0]
            if best_move not in board.legal_moves:
                continue

            # Skip if player already plays the best move.
            if best_move == player_move:
                continue

            # Eval after best move (from player's POV).
            board_after_best = board.copy()
            board_after_best.push(best_move)
            info_best = eng.analyse_single(board_after_best, depth=depth)
            best_cp = _cp_pov(info_best["score"], player_color)

            # Eval after player's move (from player's POV).
            board_after_player = board.copy()
            board_after_player.push(player_move)
            info_player = eng.analyse_single(board_after_player, depth=depth)
            player_cp = _cp_pov(info_player["score"], player_color)

            eval_gap = best_cp - player_cp
            if eval_gap < min_eval_gap:
                continue

            score = total * eval_gap / 100

            # Depth evals for display.
            depth_evals: dict[int, str] = {}
            for d in sorted({depth, max(8, depth - 4)}):
                di = eng.analyse_single(board, depth=d)
                pov = di["score"].pov(player_color)
                cp_val = pov.score(mate_score=10_000)
                depth_evals[d] = f"{cp_val / 100:+.2f}" if cp_val is not None else "M"

            results.append(
                HabitInaccuracy(
                    fen=fen,
                    total_games=total,
                    player_move_uci=player_uci,
                    player_move_san=board.san(player_move),
                    player_move_games=top_games,
                    best_move_uci=best_move.uci(),
                    best_move_san=board.san(best_move),
                    eval_cp=best_cp,
                    player_eval_cp=player_cp,
                    eval_gap_cp=eval_gap,
                    score=score,
                    depth_evals=depth_evals,
                )
            )

            if verbose and i % 10 == 0:
                print(f"  … {i}/{len(candidates)} evaluated, {len(results)} inaccuracies so far", flush=True)

    results.sort(key=lambda h: -h.score)
    results = results[:max_positions]

    if verbose:
        print(f"[habits] Done. {len(results)} habit inaccuracies found.", flush=True)

    return results


def export_habits_pgn(
    habits: list[HabitInaccuracy],
    out_path: Path,
    username: str,
    color: str,
) -> None:
    """Write an annotated PGN file with one game per habit inaccuracy."""
    games_text: list[str] = []

    for rank, habit in enumerate(habits, 1):
        board = chess.Board(habit.fen)
        player_move = chess.Move.from_uci(habit.player_move_uci)
        best_move = chess.Move.from_uci(habit.best_move_uci)

        gap_sign = "+" if habit.eval_gap_cp >= 0 else ""
        nag = "?!" if habit.eval_gap_cp < 75 else "?"

        game = chess.pgn.Game()
        game.headers["Event"] = f"Habit Inaccuracy #{rank}"
        game.headers["White"] = username if color == "white" else "?"
        game.headers["Black"] = username if color == "black" else "?"
        game.headers["Result"] = "*"
        game.headers["FEN"] = habit.fen
        game.headers["SetUp"] = "1"
        game.headers["Annotator"] = "mysecond"

        # Root comment: summary stats.
        game.comment = (
            f"score={habit.score:.1f}  "
            f"gap={gap_sign}{habit.eval_gap_cp:.0f}cp  "
            f"freq={habit.total_games}"
        )

        # Main line: player's habitual (suboptimal) move.
        node = game.add_variation(player_move)
        node.nags.add(chess.pgn.NAG_DUBIOUS_MOVE if habit.eval_gap_cp < 75 else chess.pgn.NAG_MISTAKE)
        node.comment = (
            f"{username}'s choice ({habit.player_move_games}/{habit.total_games} games); "
            f"eval {habit.player_eval_cp / 100:+.2f}"
        )

        # Variation: engine's best move.
        best_node = game.add_variation(best_move)
        best_node.comment = (
            f"best move; eval {habit.eval_cp / 100:+.2f}"
        )

        buf = io.StringIO()
        exporter = chess.pgn.FileExporter(buf)
        game.accept(exporter)
        games_text.append(buf.getvalue())

    out_path.write_text("\n\n".join(games_text), encoding="utf-8")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cp_pov(score: Any, player_color: chess.Color) -> float:
    """Extract centipawn score from player's POV (capped at ±1000 for mates)."""
    pov = score.pov(player_color)
    cp = pov.score(mate_score=10_000)
    return float(cp) if cp is not None else 0.0
