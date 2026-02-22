"""Tests for the PlayerExplorer repertoire module."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import chess
import pytest

from mysecond.models import ExplorerData, MoveStats
from mysecond.repertoire import PlayerExplorer, _parse


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_cache(hit: dict | None = None) -> MagicMock:
    """Cache that either returns a hit or misses."""
    cache = MagicMock()
    cache.get.return_value = hit
    return cache


def _lichess_response(moves: dict[str, int]) -> dict:
    """Build a minimal Lichess /player JSON response."""
    total = sum(moves.values())
    move_list = []
    for uci, count in moves.items():
        move_list.append(
            {
                "uci": uci,
                "white": count // 3,
                "draws": count // 3,
                "black": count - 2 * (count // 3),
                "averageRating": 2500,
            }
        )
    return {
        "white": total // 3,
        "draws": total // 3,
        "black": total - 2 * (total // 3),
        "moves": move_list,
    }


# ---------------------------------------------------------------------------
# _parse
# ---------------------------------------------------------------------------


def test_parse_empty_response() -> None:
    data = _parse({"white": 0, "draws": 0, "black": 0, "moves": []})
    assert data.total == 0
    assert data.moves == []


def test_parse_with_moves() -> None:
    raw = _lichess_response({"e2e4": 30, "d2d4": 15})
    data = _parse(raw)
    assert data.total > 0
    ucis = {m.uci for m in data.moves}
    assert "e2e4" in ucis
    assert "d2d4" in ucis


# ---------------------------------------------------------------------------
# PlayerExplorer – cache hit
# ---------------------------------------------------------------------------


def test_get_data_returns_cached_result() -> None:
    raw = _lichess_response({"e2e4": 20})
    cache = _mock_cache(hit=raw)

    exp = PlayerExplorer("testuser", "white", cache)
    data = exp.get_data(chess.STARTING_FEN)

    assert data is not None
    assert data.games_for_move("e2e4") > 0
    # No HTTP call made.
    exp.close()


def test_get_data_cache_miss_fetches_and_stores() -> None:
    raw = _lichess_response({"d2d4": 10})
    cache = _mock_cache(hit=None)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = raw

    with patch("mysecond.repertoire.requests.Session") as mock_session_cls:
        mock_session = MagicMock()
        mock_session.get.return_value = mock_resp
        mock_session_cls.return_value = mock_session

        exp = PlayerExplorer("testuser", "white", cache)
        data = exp.get_data(chess.STARTING_FEN)

    assert data is not None
    cache.set.assert_called_once()
    exp.close()


def test_get_data_returns_none_on_http_error() -> None:
    cache = _mock_cache(hit=None)

    mock_resp = MagicMock()
    mock_resp.status_code = 404

    with patch("mysecond.repertoire.requests.Session") as mock_session_cls:
        mock_session = MagicMock()
        mock_session.get.return_value = mock_resp
        mock_session_cls.return_value = mock_session

        exp = PlayerExplorer("testuser", "white", cache)
        data = exp.get_data(chess.STARTING_FEN)

    assert data is None
    cache.set.assert_not_called()
    exp.close()


# ---------------------------------------------------------------------------
# PlayerExplorer – validation
# ---------------------------------------------------------------------------


def test_invalid_color_raises() -> None:
    cache = _mock_cache()
    with pytest.raises(ValueError, match="color must be"):
        PlayerExplorer("testuser", "red", cache)


def test_backend_key_differs_by_color() -> None:
    cache = _mock_cache()
    white_exp = PlayerExplorer("testuser", "white", cache)
    black_exp = PlayerExplorer("testuser", "black", cache)
    assert white_exp._backend != black_exp._backend
    white_exp.close()
    black_exp.close()


def test_backend_key_differs_by_username() -> None:
    cache = _mock_cache()
    exp1 = PlayerExplorer("alice", "white", cache)
    exp2 = PlayerExplorer("bob", "white", cache)
    assert exp1._backend != exp2._backend
    exp1.close()
    exp2.close()


# ---------------------------------------------------------------------------
# PlayerExplorer – context manager
# ---------------------------------------------------------------------------


def test_context_manager_closes_session() -> None:
    cache = _mock_cache()
    with patch("mysecond.repertoire.requests.Session") as mock_session_cls:
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session

        with PlayerExplorer("testuser", "white", cache):
            pass

        mock_session.close.assert_called_once()


# ---------------------------------------------------------------------------
# _walk integration: opponent explorer overrides master top moves
# ---------------------------------------------------------------------------


def test_walk_uses_opponent_explorer_at_opponent_turns() -> None:
    """When opponent_explorer is set, _walk must use its moves at opponent turns."""
    from mysecond.search import SearchConfig, _PendingNovelty, _walk

    def _config() -> SearchConfig:
        return SearchConfig(
            fen=chess.STARTING_FEN,
            side=chess.WHITE,
            max_book_plies=4,
            min_book_games=1,
            novelty_threshold=0,
            engine_candidates=1,
            opponent_responses=1,
            depths=[16],
            time_ms=100,
            engine_path=Path("/fake/stockfish"),
            min_eval_cp=0,
            continuation_plies=2,
            max_workers=1,
        )

    # White position: engine suggests e4; it's in the masters book (5000 games).
    # Black position (after e4): masters top move is e5, but opponent plays c5.
    def masters_get_data(fen: str) -> ExplorerData:
        board = chess.Board(fen)
        if board.fullmove_number == 1 and board.turn == chess.WHITE:
            return ExplorerData(
                white=3000, draws=1000, black=1000,
                moves=[MoveStats("e2e4", 2000, 800, 1000)],
            )
        if board.fullmove_number == 1 and board.turn == chess.BLACK:
            # Masters: e5 most popular.
            return ExplorerData(
                white=1000, draws=500, black=500,
                moves=[MoveStats("e7e5", 1000, 400, 600)],
            )
        # Out of book after Black's move.
        return ExplorerData(white=0, draws=0, black=0, moves=[])

    masters_explorer = MagicMock()
    masters_explorer.get_data.side_effect = masters_get_data

    # Opponent explorer: opponent plays c5 (Sicilian).
    def opp_get_data(fen: str, local_only: bool = False) -> ExplorerData:
        board = chess.Board(fen)
        if board.fullmove_number == 1 and board.turn == chess.BLACK:
            return ExplorerData(
                white=0, draws=0, black=20,
                moves=[MoveStats("c7c5", 0, 0, 20)],
            )
        return ExplorerData(white=0, draws=0, black=0, moves=[])

    opponent_explorer = MagicMock()
    opponent_explorer.get_data.side_effect = opp_get_data

    # Engine always suggests e4.
    eng = MagicMock()
    eng.analyse_multipv.return_value = [
        {
            "pv": [chess.Move.from_uci("e2e4")],
            "score": chess.engine.PovScore(chess.engine.Cp(30), chess.WHITE),
        }
    ]

    pending: list[_PendingNovelty] = []
    visited: set[str] = set()
    counter: list[int] = [0]

    _walk(
        board=chess.Board(),
        book_moves=[],
        config=_config(),
        eng=eng,
        explorer=masters_explorer,
        pending=pending,
        visited=visited,
        positions_visited=counter,
        opponent_explorer=opponent_explorer,
    )

    # The walk followed c5 (opponent's move), not e5 (masters top move).
    # After 1.e4 c5 the position is out of book → walk stops.
    # No candidates queued (e4 is in book, and after c5 it's out of book).
    # We verify opponent_explorer.get_data was called (at Black's turn).
    fen_after_e4 = chess.Board()
    fen_after_e4.push(chess.Move.from_uci("e2e4"))
    opponent_explorer.get_data.assert_any_call(fen_after_e4.fen(), local_only=False)


# ---------------------------------------------------------------------------
# _walk integration: player explorer restricts recursion
# ---------------------------------------------------------------------------


def test_walk_respects_player_repertoire_for_recursion() -> None:
    """In-book moves not in the player's repertoire must not be recursed."""
    from mysecond.search import SearchConfig, _PendingNovelty, _walk

    def _config() -> SearchConfig:
        return SearchConfig(
            fen=chess.STARTING_FEN,
            side=chess.WHITE,
            max_book_plies=6,
            min_book_games=1,
            novelty_threshold=0,
            engine_candidates=2,
            opponent_responses=1,
            depths=[16],
            time_ms=100,
            engine_path=Path("/fake/stockfish"),
            min_eval_cp=0,
            continuation_plies=2,
            max_workers=1,
            min_player_games=3,
        )

    # Masters database: both e4 and d4 are in book.
    masters_explorer = MagicMock()
    masters_explorer.get_data.return_value = ExplorerData(
        white=3000, draws=1000, black=1000,
        moves=[
            MoveStats("e2e4", 2000, 600, 800),
            MoveStats("d2d4", 1000, 400, 200),
        ],
    )

    # Player explorer: player only plays e4 (≥3 games), not d4 (0 games).
    def player_get_data(fen: str, local_only: bool = False) -> ExplorerData:
        return ExplorerData(
            white=10, draws=2, black=2,
            moves=[MoveStats("e2e4", 8, 2, 2)],  # d2d4 absent → 0 games
        )

    player_explorer = MagicMock()
    player_explorer.get_data.side_effect = player_get_data

    # Engine suggests both e4 and d4.
    def mock_multipv(board, depth, multipv, time_ms=None):
        moves = ["e2e4", "d2d4"]
        results = []
        for uci in moves[:multipv]:
            m = chess.Move.from_uci(uci)
            if m in board.legal_moves:
                results.append({
                    "pv": [m],
                    "score": chess.engine.PovScore(chess.engine.Cp(25), chess.WHITE),
                })
        return results

    eng = MagicMock()
    eng.analyse_multipv.side_effect = mock_multipv

    pending: list[_PendingNovelty] = []
    visited: set[str] = set()
    counter: list[int] = [0]

    _walk(
        board=chess.Board(),
        book_moves=[],
        config=_config(),
        eng=eng,
        explorer=masters_explorer,
        pending=pending,
        visited=visited,
        positions_visited=counter,
        player_explorer=player_explorer,
    )

    # d4 is in book AND the engine suggests it, but the player hasn't played it.
    # So the walk must not recurse into 1.d4 lines.
    book_move_paths = [p.book_moves for p in pending]
    for path in book_move_paths:
        assert "d2d4" not in path, f"d4 line should not be explored: {path}"
