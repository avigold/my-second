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
from .fetcher import _backend_key, fetch_player_games, fetch_player_games_chesscom


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
    engine_threads: int | None = None,
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
    tag = f"[habits:{username}]"

    if verbose:
        print(f"{tag} Scanning cache ({color}, {platform}, {speeds}) …", flush=True)

    all_positions = cache.scan_backend(backend)
    # Exclude metadata entries (keys starting with _).
    all_positions = [(f, p) for f, p in all_positions if not f.startswith("_")]

    if verbose:
        print(f"{tag} {len(all_positions)} cached positions found.", flush=True)

    if not all_positions:
        if verbose:
            print(f"{tag} No cached data — fetching from {platform} …", flush=True)
        if platform == "chesscom":
            fetch_player_games_chesscom(
                username=username,
                color=color,
                cache=cache,
                speeds=speeds,
                verbose=verbose,
            )
        else:
            fetch_player_games(
                username=username,
                color=color,
                cache=cache,
                speeds=speeds,
                verbose=verbose,
            )
        all_positions = cache.scan_backend(backend)
        all_positions = [(f, p) for f, p in all_positions if not f.startswith("_")]
        if not all_positions:
            if verbose:
                print(f"{tag} No games found after fetch — check username and speeds.", flush=True)
            return []

    # Collect every (fen, payload, move_data) triple where the position was
    # reached >= min_games times AND the specific move was played >= min_games
    # times.  We check ALL qualifying moves, not just the dominant one, so
    # secondary habits are not missed.
    by_fen: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
    total_pairs = 0
    for fen, payload in all_positions:
        total = payload.get("white", 0) + payload.get("draws", 0) + payload.get("black", 0)
        if total < min_games:
            continue
        qualifying_moves = [
            m for m in payload.get("moves", [])
            if m.get("white", 0) + m.get("draws", 0) + m.get("black", 0) >= min_games
        ]
        if not qualifying_moves:
            continue
        by_fen[fen] = (payload, qualifying_moves)
        total_pairs += len(qualifying_moves)

    if verbose:
        print(
            f"{tag} {len(by_fen)} positions with ≥{min_games} games; "
            f"{total_pairs} (position, move) pairs to evaluate …",
            flush=True,
        )

    # Pre-sort by total game count descending and cap the evaluation queue.
    eval_cap = max_positions * 5
    sorted_fens = sorted(
        by_fen.items(),
        key=lambda kv: kv[1][0].get("white", 0) + kv[1][0].get("draws", 0) + kv[1][0].get("black", 0),
        reverse=True,
    )
    if len(sorted_fens) > eval_cap:
        if verbose:
            print(f"{tag} Capping to top {eval_cap} most-frequent positions.", flush=True)
        sorted_fens = sorted_fens[:eval_cap]

    results: list[HabitInaccuracy] = []

    with Engine(engine_path, threads=engine_threads) as eng:
        for i, (fen, (payload, qualifying_moves)) in enumerate(sorted_fens, 1):
            total = payload.get("white", 0) + payload.get("draws", 0) + payload.get("black", 0)

            try:
                board = chess.Board(fen)
            except ValueError:
                print(f"[progress:{username}] {i}/{len(sorted_fens)}", flush=True)
                continue

            # Verify it's the player's turn (cache should guarantee this, but be safe).
            if board.turn != player_color:
                print(f"[progress:{username}] {i}/{len(sorted_fens)}", flush=True)
                continue

            if verbose:
                print(
                    f"{tag} Position {i}/{len(sorted_fens)} — "
                    f"{total} game{'s' if total != 1 else ''}, "
                    f"{len(qualifying_moves)} qualifying move{'s' if len(qualifying_moves) != 1 else ''}",
                    flush=True,
                )

            # One MultiPV call replaces 1 + len(qualifying_moves) separate calls.
            # Request enough lines to cover all qualifying moves plus the best move.
            multipv_k = min(max(len(qualifying_moves) + 1, 5), 20)
            infos = eng.analyse_multipv(board, depth=depth, multipv=multipv_k)
            if not infos or not infos[0].get("pv"):
                print(f"[progress:{username}] {i}/{len(sorted_fens)}", flush=True)
                continue

            best_move = infos[0]["pv"][0]
            if best_move not in board.legal_moves:
                print(f"[progress:{username}] {i}/{len(sorted_fens)}", flush=True)
                continue

            best_cp = float(
                infos[0]["score"].pov(player_color).score(mate_score=10_000) or 0
            )
            best_move_san = board.san(best_move)

            # Build a UCI → eval_cp lookup from the MultiPV results.
            multipv_evals: dict[str, float] = {}
            for info in infos:
                if info.get("pv"):
                    cp = float(info["score"].pov(player_color).score(mate_score=10_000) or 0)
                    multipv_evals[info["pv"][0].uci()] = cp

            if verbose:
                print(
                    f"{tag}   Engine best: {best_move_san}  "
                    f"(depth {depth}, eval {best_cp / 100:+.2f})",
                    flush=True,
                )

            # Evaluate each qualifying player move and flag if suboptimal.
            pos_habits = 0
            for move_data in qualifying_moves:
                player_uci = move_data.get("uci", "")
                try:
                    player_move = chess.Move.from_uci(player_uci)
                except ValueError:
                    continue
                if player_move not in board.legal_moves:
                    continue
                if player_move == best_move:
                    if verbose:
                        print(
                            f"{tag}   {board.san(player_move)} — already best move, skipping",
                            flush=True,
                        )
                    continue  # player already finds the best move here

                move_games = (
                    move_data.get("white", 0)
                    + move_data.get("draws", 0)
                    + move_data.get("black", 0)
                )

                # Use MultiPV eval directly; fall back to a separate call only if
                # the player's move wasn't covered (outside top multipv_k moves).
                if player_uci in multipv_evals:
                    player_cp = multipv_evals[player_uci]
                else:
                    board_after = board.copy()
                    board_after.push(player_move)
                    info_after = eng.analyse_single(board_after, depth=depth)
                    player_cp = _cp_pov(info_after["score"], player_color)

                eval_gap = best_cp - player_cp
                player_move_san = board.san(player_move)

                if verbose:
                    gap_status = "→ INACCURACY" if eval_gap >= min_eval_gap else "ok"
                    print(
                        f"{tag}   {player_move_san} ({move_games}g): "
                        f"eval {player_cp / 100:+.2f} vs best {best_move_san} {best_cp / 100:+.2f} "
                        f"— gap {eval_gap:+.0f}cp  [{gap_status}]",
                        flush=True,
                    )

                if eval_gap < min_eval_gap:
                    continue

                habit_score = move_games * eval_gap / 100
                pos_habits += 1

                results.append(
                    HabitInaccuracy(
                        fen=fen,
                        total_games=total,
                        player_move_uci=player_uci,
                        player_move_san=player_move_san,
                        player_move_games=move_games,
                        best_move_uci=best_move.uci(),
                        best_move_san=best_move_san,
                        eval_cp=best_cp,
                        player_eval_cp=player_cp,
                        eval_gap_cp=eval_gap,
                        score=habit_score,
                        depth_evals={},
                    )
                )

            if verbose and pos_habits > 0:
                print(
                    f"{tag}   ↳ {pos_habits} inaccurac{'y' if pos_habits == 1 else 'ies'} flagged "
                    f"({len(results)} total so far)",
                    flush=True,
                )

            print(f"[progress:{username}] {i}/{len(sorted_fens)}", flush=True)

    results.sort(key=lambda h: -h.score)
    results = results[:max_positions]

    if verbose:
        print(f"{tag} Done — {len(results)} habit inaccuracies found.", flush=True)

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
