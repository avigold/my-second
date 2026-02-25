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
from .eval_cache import EvalCache
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
    eval_cache: EvalCache | None = None,
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
    cache_hits = 0

    # Engine is opened lazily — only when the first eval-cache miss occurs.
    eng: Engine | None = None

    try:
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

            # One MultiPV call replaces 1 + len(qualifying_moves) separate calls.
            multipv_k = min(max(len(qualifying_moves) + 1, 5), 20)

            # ── Try eval cache first ─────────────────────────────────────────
            cached_moves = eval_cache.get(fen, depth, multipv_k) if eval_cache else None

            if cached_moves is not None:
                cache_hits += 1
                best_move_uci = cached_moves[0]["uci"]
                try:
                    best_move = chess.Move.from_uci(best_move_uci)
                    assert best_move in board.legal_moves
                except (ValueError, AssertionError):
                    cached_moves = None  # stale entry — fall through to engine

            if cached_moves is not None:
                best_cp = float(
                    cached_moves[0]["white_cp"] if player_color == chess.WHITE
                    else -cached_moves[0]["white_cp"]
                )
                best_move_san = board.san(best_move)
                multipv_evals: dict[str, float] = {
                    e["uci"]: float(e["white_cp"] if player_color == chess.WHITE else -e["white_cp"])
                    for e in cached_moves
                }
                if verbose:
                    print(
                        f"{tag} Position {i}/{len(sorted_fens)} — "
                        f"{total} game{'s' if total != 1 else ''} "
                        f"[cache hit] best: {best_move_san} {best_cp / 100:+.2f}",
                        flush=True,
                    )
            else:
                # ── Engine analysis ──────────────────────────────────────────
                if eng is None:
                    if verbose:
                        print(f"{tag} Opening Stockfish engine …", flush=True)
                    eng = Engine(engine_path, threads=engine_threads)

                if verbose:
                    print(
                        f"{tag} Position {i}/{len(sorted_fens)} — "
                        f"{total} game{'s' if total != 1 else ''}, "
                        f"{len(qualifying_moves)} qualifying move{'s' if len(qualifying_moves) != 1 else ''}",
                        flush=True,
                    )

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

                multipv_evals = {}
                for info in infos:
                    if info.get("pv"):
                        cp = float(info["score"].pov(player_color).score(mate_score=10_000) or 0)
                        multipv_evals[info["pv"][0].uci()] = cp

                # Store to eval cache for future jobs.
                if eval_cache:
                    eval_cache.put(fen, depth, _infos_to_moves(infos))

                if verbose:
                    print(
                        f"{tag}   Engine best: {best_move_san}  "
                        f"(depth {depth}, eval {best_cp / 100:+.2f})",
                        flush=True,
                    )

            # ── Evaluate each qualifying player move ─────────────────────────
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
                    continue

                move_games = (
                    move_data.get("white", 0)
                    + move_data.get("draws", 0)
                    + move_data.get("black", 0)
                )

                if player_uci in multipv_evals:
                    player_cp = multipv_evals[player_uci]
                else:
                    # Player's move wasn't in the MultiPV window — evaluate the
                    # resulting position separately, also checking the eval cache.
                    board_after = board.copy()
                    board_after.push(player_move)
                    after_fen = board_after.fen()

                    after_cached = eval_cache.get(after_fen, depth, 1) if eval_cache else None
                    if after_cached:
                        white_cp = after_cached[0]["white_cp"]
                        player_cp = float(white_cp if player_color == chess.WHITE else -white_cp)
                    else:
                        if eng is None:
                            if verbose:
                                print(f"{tag} Opening Stockfish engine …", flush=True)
                            eng = Engine(engine_path, threads=engine_threads)
                        info_after = eng.analyse_single(board_after, depth=depth)
                        player_cp = _cp_pov(info_after["score"], player_color)
                        if eval_cache:
                            eval_cache.put(after_fen, depth, _infos_to_moves([info_after]))

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

    finally:
        if eng is not None:
            eng.__exit__(None, None, None)

    if verbose and eval_cache:
        total_pos = len(sorted_fens)
        misses = total_pos - cache_hits
        print(
            f"{tag} Eval cache: {cache_hits}/{total_pos} hits "
            f"({cache_hits * 100 // total_pos if total_pos else 0}%) — "
            f"{misses} position{'s' if misses != 1 else ''} sent to Stockfish.",
            flush=True,
        )
        if eng is None:
            print(f"{tag} Engine was never opened — all positions served from cache.", flush=True)

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


def _infos_to_moves(infos: list) -> list[dict]:
    """Convert engine InfoDicts to serialisable ``{"uci", "white_cp"}`` dicts.

    Always uses White's perspective so the cache is POV-neutral and any
    caller can re-derive Black's perspective by negating ``white_cp``.
    """
    moves = []
    for info in infos:
        if not info.get("pv"):
            continue
        uci = info["pv"][0].uci()
        white_cp = int(info["score"].white().score(mate_score=10_000) or 0)
        moves.append({"uci": uci, "white_cp": white_cp})
    return moves
