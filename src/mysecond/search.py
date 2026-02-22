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
            With ``player_name`` set: only recurse on moves the player
            has actually played (≥ ``min_player_games`` in their games).

  * At OPPONENT's turns
      Follow the ``opponent_responses`` most popular database moves.
      With ``opponent_name`` set: follow that player's own moves from
      their Lichess game history instead of generic masters top moves.

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
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path

import chess
import chess.engine

from .cache import Cache
from .engine import Engine
from .explorer import LichessExplorer
from .models import EngineEval, NoveltyLine
from .repertoire import PlayerExplorer

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

    # --- Player/opponent filtering (optional) ---
    # Set these to a Lichess username to scope the walk to lines the player
    # and opponent actually play, rather than all of master theory.
    player_name: str | None = None    # Lichess username of the player we prepare for
    opponent_name: str | None = None  # Lichess username of the opponent
    min_player_games: int = 3         # min player games for a move to be "in repertoire"
    min_opponent_games: int = 3       # min opponent games for a move to be "their line"
    player_speeds: str = "blitz,rapid,classical"    # time controls for player's games
    opponent_speeds: str = "blitz,rapid,classical"  # time controls for opponent's games
    # When True, PlayerExplorer never makes HTTP requests; use only local cache.
    # Always set to True after running fetch-player-games (the default when
    # player_name/opponent_name are set from the CLI).
    player_local_only: bool = False


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

    # Determine colours for player/opponent explorers.
    player_color = "white" if config.side == chess.WHITE else "black"
    opponent_color = "black" if config.side == chess.WHITE else "white"

    # Build optional player/opponent explorer context managers.
    player_ctx: PlayerExplorer | nullcontext  # type: ignore[type-arg]
    opponent_ctx: PlayerExplorer | nullcontext  # type: ignore[type-arg]

    if config.player_name:
        player_ctx = PlayerExplorer(
            config.player_name, player_color, Cache(_DEFAULT_DB),
            speeds=config.player_speeds,
        )
    else:
        player_ctx = nullcontext()

    if config.opponent_name:
        opponent_ctx = PlayerExplorer(
            config.opponent_name, opponent_color, Cache(_DEFAULT_DB),
            speeds=config.opponent_speeds,
        )
    else:
        opponent_ctx = nullcontext()

    # --- Phase 1: tree walk (sequential; one engine + one explorer) ----------
    pending: list[_PendingNovelty] = []
    visited: set[str] = set()
    positions_visited: list[int] = [0]

    with Engine(config.engine_path) as eng:
        with Cache(_DEFAULT_DB) as cache:
            with LichessExplorer(cache) as explorer:
                with player_ctx as player_explorer:
                    with opponent_ctx as opponent_explorer:
                        _walk(
                            board=chess.Board(config.fen),
                            book_moves=[],
                            config=config,
                            eng=eng,
                            explorer=explorer,
                            pending=pending,
                            visited=visited,
                            positions_visited=positions_visited,
                            player_explorer=player_explorer,
                            opponent_explorer=opponent_explorer,
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
    player_explorer: PlayerExplorer | None = None,
    opponent_explorer: PlayerExplorer | None = None,
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

        # Fetch player's own data once for this position.
        # local_only=True means cache-miss → None → fallback to unrestricted
        # (no live HTTP call; safe and fast after fetch-player-games).
        player_data = (
            player_explorer.get_data(fen, local_only=config.player_local_only)
            if player_explorer
            else None
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
                # Still in book – walk deeper, but only if within player's repertoire.
                if not _player_plays_move(move, player_data, config.min_player_games):
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
                    player_explorer,
                    opponent_explorer,
                )
    else:
        # Opponent's turn: follow the most popular moves.
        # With an opponent explorer: follow the opponent's actual moves.
        move_list = _opponent_moves(
            fen, data, opponent_explorer, config.opponent_responses,
            config.min_opponent_games, local_only=config.player_local_only,
        )
        for move_stats in move_list:
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
                player_explorer,
                opponent_explorer,
            )


# ---------------------------------------------------------------------------
# Walk helpers
# ---------------------------------------------------------------------------


def _player_plays_move(
    move: chess.Move,
    player_data: object,  # ExplorerData | None
    min_games: int,
) -> bool:
    """Return True if the player has played *move*, or if no player filter is active.

    ``player_data`` is the per-position data from the player's own explorer.

    * ``None`` → no player filter active; allow all moves.
    * ``total < min_games`` → not enough games in this position to make a
      reliable repertoire decision; fall back to allowing all moves rather
      than silently blocking the entire subtree.
    * Otherwise check whether the player has played this specific move.
    """
    if player_data is None:
        return True  # no player filter → allow all
    # If the player has no meaningful data in this position, don't restrict.
    if player_data.total < min_games:  # type: ignore[union-attr]
        return True
    return player_data.games_for_move(move.uci()) >= min_games  # type: ignore[union-attr]


def _opponent_moves(
    fen: str,
    masters_data: object,  # ExplorerData
    opponent_explorer: PlayerExplorer | None,
    n: int,
    min_games: int,
    local_only: bool = False,
) -> list:
    """Return the opponent moves to follow at this position.

    Uses the opponent's personal explorer if available and they have enough
    games here; otherwise falls back to the full masters database top moves.
    """
    if opponent_explorer is not None:
        opp_data = opponent_explorer.get_data(fen, local_only=local_only)
        if opp_data is not None and opp_data.total >= min_games:
            top = opp_data.top_moves(n)
            if top:
                return top
    # Fall back to full masters database.
    return masters_data.top_moves(n)  # type: ignore[union-attr]


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
