"""Tests for novelty detection logic (mocked engine + explorer)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import chess
import chess.engine
import pytest

from mysecond.models import ExplorerData, MoveStats
from mysecond.search import SearchConfig, _PendingNovelty, _walk


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config(
    fen: str = chess.STARTING_FEN,
    side: chess.Color = chess.WHITE,
    max_book_plies: int = 4,
    min_book_games: int = 1,
    novelty_threshold: int = 0,
    engine_candidates: int = 3,
    opponent_responses: int = 2,
) -> SearchConfig:
    return SearchConfig(
        fen=fen,
        side=side,
        max_book_plies=max_book_plies,
        min_book_games=min_book_games,
        novelty_threshold=novelty_threshold,
        engine_candidates=engine_candidates,
        opponent_responses=opponent_responses,
        depths=[16, 20],
        time_ms=100,
        engine_path=Path("/fake/stockfish"),
        min_eval_cp=0,
        continuation_plies=4,
        max_workers=1,
    )


def _mock_engine(moves: list[str]) -> MagicMock:
    """Engine that always suggests *moves* as its MultiPV result."""
    eng = MagicMock()

    def analyse_multipv(board, depth, multipv, time_ms=None):
        results = []
        for uci in moves[:multipv]:
            m = chess.Move.from_uci(uci)
            if m not in board.legal_moves:
                continue
            info = {
                "pv": [m],
                "score": chess.engine.PovScore(chess.engine.Cp(30), chess.WHITE),
            }
            results.append(info)
        return results

    eng.analyse_multipv.side_effect = analyse_multipv
    return eng


def _explorer_with(move_games: dict[str, int], total: int = 5000) -> MagicMock:
    """Explorer that returns fixed game counts per move."""
    moves = [
        MoveStats(uci=uci, white=n // 3, draws=n // 3, black=n - 2 * (n // 3))
        for uci, n in move_games.items()
    ]
    data = ExplorerData(white=total // 3, draws=total // 3, black=total // 3, moves=moves)

    explorer = MagicMock()
    explorer.get_data.return_value = data
    return explorer


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_move_with_zero_games_becomes_candidate() -> None:
    """A move with 0 master games must be collected as a novelty candidate."""
    eng = _mock_engine(["e2e4"])          # engine suggests 1.e4
    explorer = _explorer_with({"d2d4": 3000})  # 1.e4 is NOT in the DB

    pending: list[_PendingNovelty] = []
    visited: set[str] = set()
    counter: list[int] = [0]

    _walk(
        board=chess.Board(),
        book_moves=[],
        config=_config(engine_candidates=1, novelty_threshold=0),
        eng=eng,
        explorer=explorer,
        pending=pending,
        visited=visited,
        positions_visited=counter,
    )

    assert len(pending) == 1
    assert pending[0].move == chess.Move.from_uci("e2e4")
    assert pending[0].post_novelty_games == 0


def test_in_book_move_causes_recursion() -> None:
    """A move with many games must trigger recursion, not become a candidate."""
    # 1.e4 is in the book (5000 games); after 1.e4 the position has <min_book_games
    # so the walk stops there — but e4 itself should NOT appear as a pending novelty.
    def get_data_side_effect(fen):
        board = chess.Board(fen)
        if board.fullmove_number == 1 and board.turn == chess.WHITE:
            # Root position: e4 is in the database with many games.
            return ExplorerData(
                white=3000, draws=1000, black=1000,
                moves=[MoveStats("e2e4", 2000, 800, 1000)],
            )
        # Any other position: out of book (total < min_book_games).
        return ExplorerData(white=1, draws=0, black=0, moves=[])

    eng = _mock_engine(["e2e4"])
    explorer = MagicMock()
    explorer.get_data.side_effect = get_data_side_effect

    pending: list[_PendingNovelty] = []
    visited: set[str] = set()
    counter: list[int] = [0]

    _walk(
        board=chess.Board(),
        book_moves=[],
        config=_config(engine_candidates=1, novelty_threshold=0, min_book_games=5),
        eng=eng,
        explorer=explorer,
        pending=pending,
        visited=visited,
        positions_visited=counter,
    )

    # 1.e4 is in the book so it recurses; the position after 1.e4 is out-of-book
    # so walk stops — no pending novelties, but no crash either.
    assert len(pending) == 0


def test_novelty_threshold_respected() -> None:
    """Moves with games ≤ novelty_threshold are collected; moves above are not."""
    eng = _mock_engine(["e2e4", "d2d4"])
    explorer = _explorer_with({"e2e4": 1, "d2d4": 100})  # e4=1, d4=100 games

    pending: list[_PendingNovelty] = []
    visited: set[str] = set()
    counter: list[int] = [0]

    cfg = _config(engine_candidates=2, novelty_threshold=2)

    _walk(
        board=chess.Board(),
        book_moves=[],
        config=cfg,
        eng=eng,
        explorer=explorer,
        pending=pending,
        visited=visited,
        positions_visited=counter,
    )

    # Only e2e4 (1 game ≤ 2) should be a candidate; d2d4 (100 games) is in book.
    uci_set = {p.move.uci() for p in pending}
    assert "e2e4" in uci_set
    assert "d2d4" not in uci_set


def test_out_of_book_position_stops_walk() -> None:
    """Positions with fewer than min_book_games stop the walk immediately."""
    explorer = MagicMock()
    explorer.get_data.return_value = ExplorerData(
        white=0, draws=0, black=0, moves=[]
    )
    eng = _mock_engine(["e2e4"])

    pending: list[_PendingNovelty] = []
    visited: set[str] = set()
    counter: list[int] = [0]

    _walk(
        board=chess.Board(),
        book_moves=[],
        config=_config(min_book_games=10),
        eng=eng,
        explorer=explorer,
        pending=pending,
        visited=visited,
        positions_visited=counter,
    )

    assert len(pending) == 0


def test_visited_set_prevents_revisit() -> None:
    """The same FEN must not be visited twice."""
    explorer = _explorer_with({"e2e4": 0})
    eng = _mock_engine(["e2e4"])

    pending: list[_PendingNovelty] = []
    visited: set[str] = set()
    counter: list[int] = [0]

    cfg = _config()
    board = chess.Board()

    _walk(board, [], cfg, eng, explorer, pending, visited, counter)
    first_count = counter[0]

    # Walking again should not visit the already-seen root.
    _walk(board, [], cfg, eng, explorer, pending, visited, counter)
    assert counter[0] == first_count  # no new positions visited


def test_max_positions_guard() -> None:
    """Walk must stop when max_positions is reached."""
    explorer = _explorer_with({"e2e4": 5000, "d2d4": 3000})
    eng = _mock_engine(["e2e4", "d2d4"])

    pending: list[_PendingNovelty] = []
    visited: set[str] = set()
    counter: list[int] = [0]

    cfg = _config(max_book_plies=20)
    cfg.max_positions = 1  # stop after visiting 1 position

    _walk(chess.Board(), [], cfg, eng, explorer, pending, visited, counter)

    assert counter[0] <= 1
