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
import threading
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

# Thread-safe printing for the parallel deep-eval phase.
_PRINT_LOCK = threading.Lock()


def _p(*args: object, **kwargs: object) -> None:
    """Print with flush=True and a thread-safe lock."""
    with _PRINT_LOCK:
        print(*args, **kwargs, flush=True)


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
    player_name: str | None = None
    opponent_name: str | None = None
    min_player_games: int = 3
    min_opponent_games: int = 3
    player_speeds: str = "blitz,rapid,classical"
    opponent_speeds: str = "blitz,rapid,classical"
    player_local_only: bool = False


# ---------------------------------------------------------------------------
# Internal staging type (between walk and evaluation phases)
# ---------------------------------------------------------------------------


@dataclass
class _PendingNovelty:
    board: chess.Board        # position *before* the novelty move
    book_moves: list[str]     # UCI path through theory
    book_moves_san: list[str] # SAN path through theory (for display)
    move: chess.Move          # the novelty move
    pre_novelty_games: int    # games at the position before the novelty
    post_novelty_games: int   # games after the novelty move (0 = true TN)
    quick_eval_cp: float      # perspective-corrected cp from the walk's quick eval


# ---------------------------------------------------------------------------
# Verbose formatting helpers
# ---------------------------------------------------------------------------


def _path_str(book_moves_san: list[str]) -> str:
    """Format a SAN move list as a compact opening path string.

    Example: ["e4", "e5", "Nf3", "Nc6"] → "1.e4 e5 2.Nf3 Nc6"
    """
    if not book_moves_san:
        return "starting position"
    parts: list[str] = []
    for i, san in enumerate(book_moves_san):
        if i % 2 == 0:
            parts.append(f"{i // 2 + 1}.{san}")
        else:
            parts.append(san)
    return " ".join(parts)


def _cp_str(cp: float) -> str:
    return f"{cp:+.2f}".replace("+", "+").replace("-", "−")


# ---------------------------------------------------------------------------
# Public entry-point
# ---------------------------------------------------------------------------


def find_novelties(config: SearchConfig) -> list[NoveltyLine]:
    """Walk theory and return deeply evaluated novelty candidates."""

    player_color   = "white" if config.side == chess.WHITE else "black"
    opponent_color = "black" if config.side == chess.WHITE else "white"

    player_ctx: PlayerExplorer | nullcontext   # type: ignore[type-arg]
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

    # --- Phase 1: tree walk --------------------------------------------------
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
                            book_moves_san=[],
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

    # --- Phase 1b: prune candidates ------------------------------------------
    before_prune = len(pending)
    pending.sort(key=lambda p: -p.quick_eval_cp)
    pending = pending[: config.max_candidates]

    _p(
        f"\n[mysecond] ── Phase 1 complete ─────────────────────────────────────\n"
        f"  Positions visited : {positions_visited[0]}\n"
        f"  Candidates found  : {before_prune}\n"
        f"  After pruning     : {len(pending)}  (top by quick eval)\n"
        f"[mysecond] ─────────────────────────────────────────────────────────"
    )

    # --- Phase 2: deep evaluation (parallel) ---------------------------------
    _p(f"\n[mysecond] ── Phase 2: deep evaluation ({len(pending)} candidates, "
       f"{min(config.max_workers, len(pending))} workers) ──\n")

    results: list[NoveltyLine] = []
    workers = min(config.max_workers, len(pending))
    total = len(pending)
    done_count: list[int] = [0]

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_evaluate_candidate, p, config, i + 1, total): p
            for i, p in enumerate(pending)
        }
        for future in as_completed(futures):
            try:
                result = future.result()
                with _PRINT_LOCK:
                    done_count[0] += 1
                if result is not None:
                    results.append(result)
            except Exception as exc:  # noqa: BLE001
                _p(f"[eval]  Warning: evaluation error – {exc}", file=sys.stderr)

    return results


# ---------------------------------------------------------------------------
# Phase 1 – Theory walk
# ---------------------------------------------------------------------------


def _walk(
    board: chess.Board,
    book_moves: list[str],
    book_moves_san: list[str],
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
        _p(f"[walk]  Position cap reached ({config.max_positions}), stopping walk.")
        return
    if len(book_moves) >= config.max_book_plies:
        return

    visited.add(fen)
    positions_visited[0] += 1
    n = positions_visited[0]
    path = _path_str(book_moves_san)

    data = explorer.get_data(fen)
    if data is None or data.total < config.min_book_games:
        _p(f"[walk] {n:>4}  {path}  → out of book "
           f"({data.total if data else 0} master games, min={config.min_book_games}), stopping.")
        return

    if board.turn == config.side:
        # ── OUR TURN ──────────────────────────────────────────────────────────
        turn_label = "White" if config.side == chess.WHITE else "Black"
        _p(f"[walk] {n:>4}  {path}  "
           f"[{turn_label} to move | {data.total:,} master games | "
           f"asking engine for {config.engine_candidates} candidates]")

        quick_depth = min(config.depths)
        infos = eng.analyse_multipv(
            board,
            depth=quick_depth,
            multipv=config.engine_candidates,
            time_ms=config.time_ms,
        )

        player_data = (
            player_explorer.get_data(fen, local_only=config.player_local_only)
            if player_explorer
            else None
        )

        if config.player_name and player_data is not None:
            _p(f"[walk]       player {config.player_name}: "
               f"{player_data.total} games at this position")

        for info in infos:
            if "pv" not in info or not info["pv"]:
                continue
            move = info["pv"][0]
            san  = board.san(move)

            pov      = info["score"].pov(config.side)
            quick_cp = float(pov.score(mate_score=10_000) or 0)
            post_games = data.games_for_move(move.uci())

            if post_games <= config.novelty_threshold:
                # ── NOVELTY CANDIDATE ─────────────────────────────────────
                label = "TRUE NOVELTY" if post_games == 0 else f"rare ({post_games} master games)"
                if quick_cp >= config.min_eval_cp:
                    _p(f"[walk]       ★ NOVELTY  {san}  "
                       f"post={post_games}  quick_eval={_cp_str(quick_cp)}cp  "
                       f"[{label}]  → queued for deep eval")
                    pending.append(
                        _PendingNovelty(
                            board=board.copy(),
                            book_moves=list(book_moves),
                            book_moves_san=list(book_moves_san),
                            move=move,
                            pre_novelty_games=data.total,
                            post_novelty_games=post_games,
                            quick_eval_cp=quick_cp,
                        )
                    )
                else:
                    _p(f"[walk]       ✗ novelty  {san}  "
                       f"post={post_games}  quick_eval={_cp_str(quick_cp)}cp  "
                       f"[eval below {config.min_eval_cp}cp threshold, skipping]")
            else:
                # ── IN BOOK: consider recursing ────────────────────────────
                if not _player_plays_move(move, player_data, config.min_player_games):
                    player_games = (
                        player_data.games_for_move(move.uci())  # type: ignore[union-attr]
                        if player_data else 0
                    )
                    _p(f"[walk]       · skip      {san}  "
                       f"[in book: {post_games} games | "
                       f"{config.player_name} plays {player_games}×, "
                       f"min={config.min_player_games}]")
                    continue

                player_note = ""
                if config.player_name and player_data is not None:
                    pg = player_data.games_for_move(move.uci())
                    player_note = f" | {config.player_name}: {pg}×"

                _p(f"[walk]       → recurse   {san}  "
                   f"[in book: {post_games:,} games{player_note}]")

                new_board = board.copy()
                new_board.push(move)
                _walk(
                    new_board,
                    book_moves + [move.uci()],
                    book_moves_san + [san],
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
        # ── OPPONENT'S TURN ───────────────────────────────────────────────────
        opp_label = "Black" if config.side == chess.WHITE else "White"
        move_list, source = _opponent_moves_with_source(
            fen, data, opponent_explorer, config.opponent_responses,
            config.min_opponent_games, local_only=config.player_local_only,
        )
        moves_str = "  ".join(
            f"{board.san(chess.Move.from_uci(ms.uci))} ({ms.total}×)"
            for ms in move_list
            if chess.Move.from_uci(ms.uci) in board.legal_moves
        )
        _p(f"[walk] {n:>4}  {path}  "
           f"[{opp_label} to move | {data.total:,} master games | "
           f"following {source}: {moves_str or '(none)'}]")

        for move_stats in move_list:
            move = chess.Move.from_uci(move_stats.uci)
            if move not in board.legal_moves:
                continue
            san = board.san(move)
            new_board = board.copy()
            new_board.push(move)
            _walk(
                new_board,
                book_moves + [move.uci()],
                book_moves_san + [san],
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
    if player_data is None:
        return True
    if player_data.total < min_games:  # type: ignore[union-attr]
        return True
    return player_data.games_for_move(move.uci()) >= min_games  # type: ignore[union-attr]


def _opponent_moves(
    fen: str,
    masters_data: object,
    opponent_explorer: PlayerExplorer | None,
    n: int,
    min_games: int,
    local_only: bool = False,
) -> list:
    moves, _ = _opponent_moves_with_source(
        fen, masters_data, opponent_explorer, n, min_games, local_only=local_only
    )
    return moves


def _opponent_moves_with_source(
    fen: str,
    masters_data: object,
    opponent_explorer: PlayerExplorer | None,
    n: int,
    min_games: int,
    local_only: bool = False,
) -> tuple[list, str]:
    """Return (move_list, source_label) for opponent moves at this position."""
    if opponent_explorer is not None:
        opp_data = opponent_explorer.get_data(fen, local_only=local_only)
        if opp_data is not None and opp_data.total >= min_games:
            top = opp_data.top_moves(n)
            if top:
                name = getattr(opponent_explorer, "_username", "opponent")
                return top, f"{name} ({opp_data.total} games)"
    top = masters_data.top_moves(n)  # type: ignore[union-attr]
    return top, "masters DB"


# ---------------------------------------------------------------------------
# Phase 2 – Deep evaluation (one engine per worker thread)
# ---------------------------------------------------------------------------


def _evaluate_candidate(
    p: _PendingNovelty,
    config: SearchConfig,
    candidate_num: int,
    total_candidates: int,
) -> NoveltyLine | None:
    """Evaluate one novelty candidate deeply; return None if below eval floor."""
    path = _path_str(p.book_moves_san)
    san  = p.board.san(p.move)
    full_line = f"{path} {san}".strip() if path != "starting position" else san
    prefix = f"[eval] {candidate_num:>3}/{total_candidates}"

    _p(f"{prefix}  ── {full_line}  "
       f"(ply {len(p.book_moves) + 1}, pre={p.pre_novelty_games:,}, "
       f"post={p.post_novelty_games}, quick={_cp_str(p.quick_eval_cp)}cp)")

    with Engine(config.engine_path) as eng:
        post_board = p.board.copy()
        post_board.push(p.move)

        evals: dict[int, EngineEval] = {}
        for depth in sorted(config.depths):
            info  = eng.analyse_single(post_board, depth=depth)
            score = info["score"]
            ev = EngineEval(
                depth=depth,
                cp_white=score.white().score(mate_score=10_000),
                mate_white=score.white().mate(),
            )
            evals[depth] = ev
            pov_str = ev.display() if config.side == chess.WHITE else (
                f"{-ev.cp_white / 100:+.2f}" if ev.cp_white is not None
                else ("M+" if (ev.mate_white or 0) < 0 else "M-") + str(abs(ev.mate_white or 0))
            )
            _p(f"{prefix}       depth {depth:>2}: {ev.display()}")

        cp_values = [
            ev.cp_pov(config.side)
            for ev in evals.values()
            if ev.cp_white is not None
        ]
        if cp_values:
            mean_cp = sum(cp_values) / len(cp_values)
        else:
            mean_cp = 10_000.0 if _any_mate_for(evals, config.side) else -10_000.0

        if mean_cp < config.min_eval_cp:
            _p(f"{prefix}       ✗ FAILED  mean={_cp_str(mean_cp)}cp "
               f"< min {config.min_eval_cp}cp — discarded")
            return None

        _p(f"{prefix}       ✓ PASSED  mean={_cp_str(mean_cp)}cp — kept")

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
