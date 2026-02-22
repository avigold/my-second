"""Theory-walk novelty search with parallel deep evaluation.

Algorithm
---------
Phase 1 – Theory walk (sequential, explorer-rate-limited):

  Starting from the root FEN, traverse the opening tree:

  * At OUR side's turns
      Ask the engine for the top ``engine_candidates`` moves.
      For each candidate move, check the database:
        - games ≤ novelty_threshold → novelty candidate
            The quick eval from analyse_multipv is stored.  Candidates that
            already fail ``min_eval_cp`` are discarded immediately.
        - games > novelty_threshold → still in book → recurse deeper

  * At OPPONENT's turns
      Follow the ``opponent_responses`` most popular database moves → recurse.

  Positions with fewer than ``min_book_games`` total games are considered
  out-of-book and the walk stops there.

Phase 1b – Candidate pruning:

  After the walk, pending candidates are sorted by quick eval (descending) and
  trimmed to ``max_candidates``.  This prevents deep-eval from running on
  thousands of positions when the walk produces many novelty candidates.

Phase 2 – Deep evaluation (parallel, one engine process per worker):

  For each surviving candidate:
    • Play the novelty move on the board.
    • Evaluate at each depth in ``depths``.
    • Extract the engine's principal variation as suggested continuations.
    • Discard if perspective-corrected mean eval < ``min_eval_cp``.
"""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

import chess
import chess.engine

from .cache import Cache
from .engine import Engine
from .explorer import LichessExplorer
from .models import EngineEval, NoveltyLine

_DEFAULT_DB = Path("data/cache.sqlite")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class SearchConfig:
    fen: str
    side: chess.Color
    max_book_plies: int       # stop walking theory after this many half-moves
    min_book_games: int       # minimum games for a position to be "in book"
    novelty_threshold: int    # max games for a move to qualify as a novelty
    engine_candidates: int    # engine moves to consider at each of our turns
    opponent_responses: int   # database moves to follow at opponent's turns
    depths: list[int]         # depths for deep evaluation of novelties
    time_ms: int              # per-position time cap (ms) for quick analysis
    engine_path: Path
    min_eval_cp: int          # minimum perspective-corrected cp to keep a novelty
    continuation_plies: int   # how many post-novelty PV moves to include
    max_workers: int = 4
    max_positions: int = 800  # guard against runaway tree exploration
    max_candidates: int = 200  # max candidates to send to deep evaluation


# ---------------------------------------------------------------------------
# Internal staging type (between walk and evaluation phases)
# ---------------------------------------------------------------------------


@dataclass
class _PendingNovelty:
    board: chess.Board        # position *before* the novelty move
    book_moves: list[str]     # UCI path through theory
    move: chess.Move          # the novelty move
    pre_novelty_games: int    # games at the position before the novelty
    post_novelty_games: int   # games after the novelty move (0 = true TN)
    quick_eval_cp: float      # perspective-corrected cp from the walk's quick eval


# ---------------------------------------------------------------------------
# Public entry-point
# ---------------------------------------------------------------------------


def find_novelties(config: SearchConfig) -> list[NoveltyLine]:
    """Walk theory and return deeply evaluated novelty candidates."""

    # --- Phase 1: tree walk (sequential; one engine + one explorer) ----------
    pending: list[_PendingNovelty] = []
    visited: set[str] = set()
    positions_visited: list[int] = [0]

    with Engine(config.engine_path) as eng:
        with Cache(_DEFAULT_DB) as cache:
            with LichessExplorer(cache) as explorer:
                _walk(
                    board=chess.Board(config.fen),
                    book_moves=[],
                    config=config,
                    eng=eng,
                    explorer=explorer,
                    pending=pending,
                    visited=visited,
                    positions_visited=positions_visited,
                )

    if not pending:
        return []

    # --- Phase 1b: prune candidates before expensive deep eval ---------------
    # Sort by quick eval descending; keep best max_candidates.
    pending.sort(key=lambda p: -p.quick_eval_cp)
    pending = pending[: config.max_candidates]

    print(
        f"[mysecond] Theory walk complete: {positions_visited[0]} positions visited, "
        f"{len(pending)} candidates selected for deep evaluation.",
        flush=True,
    )

    # --- Phase 2: deep evaluation (parallel) --------------------------------
    results: list[NoveltyLine] = []
    workers = min(config.max_workers, len(pending))

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_evaluate_candidate, p, config): p
            for p in pending
        }
        for future in as_completed(futures):
            try:
                result = future.result()
                if result is not None:
                    results.append(result)
            except Exception as exc:  # noqa: BLE001
                print(
                    f"[mysecond] Warning: evaluation failed – {exc}",
                    file=sys.stderr,
                )

    return results


# ---------------------------------------------------------------------------
# Phase 1 – Theory walk
# ---------------------------------------------------------------------------


def _walk(
    board: chess.Board,
    book_moves: list[str],
    config: SearchConfig,
    eng: Engine,
    explorer: LichessExplorer,
    pending: list[_PendingNovelty],
    visited: set[str],
    positions_visited: list[int],
) -> None:
    """Recursively walk the opening tree, collecting novelty candidates."""

    fen = board.fen()

    if fen in visited:
        return
    if positions_visited[0] >= config.max_positions:
        return
    if len(book_moves) >= config.max_book_plies:
        return

    visited.add(fen)
    positions_visited[0] += 1

    data = explorer.get_data(fen)
    if data is None or data.total < config.min_book_games:
        return  # out of book

    if board.turn == config.side:
        # Our turn: look for novelty moves, recurse on in-book moves.
        quick_depth = min(config.depths)
        infos = eng.analyse_multipv(
            board,
            depth=quick_depth,
            multipv=config.engine_candidates,
            time_ms=config.time_ms,
        )

        for info in infos:
            if "pv" not in info or not info["pv"]:
                continue
            move = info["pv"][0]

            # Perspective-corrected quick eval for this candidate.
            pov = info["score"].pov(config.side)
            quick_cp = float(pov.score(mate_score=10_000) or 0)

            post_games = data.games_for_move(move.uci())

            if post_games <= config.novelty_threshold:
                # Pre-filter by min_eval now to avoid queuing bad candidates.
                if quick_cp >= config.min_eval_cp:
                    pending.append(
                        _PendingNovelty(
                            board=board.copy(),
                            book_moves=list(book_moves),
                            move=move,
                            pre_novelty_games=data.total,
                            post_novelty_games=post_games,
                            quick_eval_cp=quick_cp,
                        )
                    )
            else:
                # Still in book – walk deeper.
                new_board = board.copy()
                new_board.push(move)
                _walk(
                    new_board,
                    book_moves + [move.uci()],
                    config,
                    eng,
                    explorer,
                    pending,
                    visited,
                    positions_visited,
                )
    else:
        # Opponent's turn: follow the most popular database responses.
        for move_stats in data.top_moves(config.opponent_responses):
            move = chess.Move.from_uci(move_stats.uci)
            if move not in board.legal_moves:
                continue
            new_board = board.copy()
            new_board.push(move)
            _walk(
                new_board,
                book_moves + [move.uci()],
                config,
                eng,
                explorer,
                pending,
                visited,
                positions_visited,
            )


# ---------------------------------------------------------------------------
# Phase 2 – Deep evaluation (one engine per worker thread)
# ---------------------------------------------------------------------------


def _evaluate_candidate(
    p: _PendingNovelty,
    config: SearchConfig,
) -> NoveltyLine | None:
    """Evaluate one novelty candidate deeply; return None if below eval floor."""
    with Engine(config.engine_path) as eng:
        post_board = p.board.copy()
        post_board.push(p.move)

        # Deep multi-depth evaluation.
        evals: dict[int, EngineEval] = {}
        for depth in sorted(config.depths):
            info = eng.analyse_single(post_board, depth=depth)
            score = info["score"]
            evals[depth] = EngineEval(
                depth=depth,
                cp_white=score.white().score(mate_score=10_000),
                mate_white=score.white().mate(),
            )

        # Perspective-corrected mean eval.
        cp_values = [
            ev.cp_pov(config.side)
            for ev in evals.values()
            if ev.cp_white is not None
        ]
        if cp_values:
            mean_cp = sum(cp_values) / len(cp_values)
        else:
            mean_cp = 10_000.0 if _any_mate_for(evals, config.side) else -10_000.0

        # Final eval filter — the deep eval may differ from the quick eval.
        if mean_cp < config.min_eval_cp:
            return None

        # Suggested continuations from the PV at the shallowest depth.
        continuations: list[str] = []
        cont_info = eng.analyse_single(post_board, depth=min(config.depths))
        if "pv" in cont_info:
            continuations = [
                m.uci() for m in cont_info["pv"][: config.continuation_plies]
            ]

        return NoveltyLine(
            book_moves=p.book_moves,
            novelty_move=p.move.uci(),
            novelty_ply=len(p.book_moves),
            evals=evals,
            pre_novelty_games=p.pre_novelty_games,
            post_novelty_games=p.post_novelty_games,
            continuations=continuations,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _any_mate_for(evals: dict[int, EngineEval], side: chess.Color) -> bool:
    for ev in evals.values():
        if ev.mate_white is not None:
            return bool(ev.mate_white > 0) == bool(side)
    return False
