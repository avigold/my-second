"""Tests for the game-fetch / opening-book-build pipeline."""

from __future__ import annotations

import io
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import chess
import chess.pgn
import pytest

from mysecond.fetcher import (
    _build_book,
    _merge_payloads,
    _to_payload,
    fetch_player_games,
    last_fetch_ts,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pgn(games: list[tuple[list[str], str]]) -> str:
    """Build a PGN string from a list of (move_ucis, result) tuples."""
    parts: list[str] = []
    for moves, result in games:
        board = chess.Board()
        san_moves: list[str] = []
        move_num = 1
        for i, uci in enumerate(moves):
            m = chess.Move.from_uci(uci)
            if board.turn == chess.WHITE:
                san_moves.append(f"{move_num}.")
                move_num += 1
            san_moves.append(board.san(m))
            board.push(m)
        parts.append(
            f'[Event "Test"]\n[White "A"]\n[Black "B"]\n[Result "{result}"]\n\n'
            + " ".join(san_moves)
            + f" {result}\n\n"
        )
    return "\n".join(parts)


def _mock_cache() -> MagicMock:
    cache = MagicMock()
    cache.get.return_value = None
    return cache


# ---------------------------------------------------------------------------
# _build_book
# ---------------------------------------------------------------------------


def test_build_book_records_player_moves_as_white() -> None:
    """As White, only positions at White's turn should be recorded."""
    pgn = _make_pgn([
        (["e2e4", "e7e5", "g1f3"], "1-0"),
    ])
    book = _build_book(pgn, "white", max_plies=6)

    # Starting position (White's turn) should be in the book.
    start_fen = chess.STARTING_FEN
    assert start_fen in book

    # After 1.e4 e5 it is White's turn again – should also be recorded.
    b = chess.Board()
    b.push(chess.Move.from_uci("e2e4"))
    b.push(chess.Move.from_uci("e7e5"))
    fen_after_e4e5 = b.fen()
    assert fen_after_e4e5 in book

    # After 1.e4 it is Black's turn – must NOT be recorded for White.
    b2 = chess.Board()
    b2.push(chess.Move.from_uci("e2e4"))
    assert b2.fen() not in book


def test_build_book_records_player_moves_as_black() -> None:
    """As Black, only positions at Black's turn should be recorded."""
    pgn = _make_pgn([
        (["e2e4", "e7e5"], "0-1"),
    ])
    book = _build_book(pgn, "black", max_plies=4)

    # Starting position is White's turn – must NOT be recorded for Black.
    assert chess.STARTING_FEN not in book

    # After 1.e4 it is Black's turn – should be recorded.
    b = chess.Board()
    b.push(chess.Move.from_uci("e2e4"))
    assert b.fen() in book


def test_build_book_counts_wins_draws_losses() -> None:
    """Win/draw/loss counts must be accumulated correctly."""
    pgn = _make_pgn([
        (["e2e4", "e7e5"], "1-0"),   # white wins
        (["e2e4", "d7d5"], "0-1"),   # white loses
        (["e2e4", "c7c5"], "1/2-1/2"),  # draw
    ])
    book = _build_book(pgn, "white", max_plies=2)

    start = book[chess.STARTING_FEN]
    assert start["white"] == 1
    assert start["draws"] == 1
    assert start["black"] == 1

    # e4 was played in all 3 games.
    assert start["moves"]["e2e4"]["white"] == 1
    assert start["moves"]["e2e4"]["draws"] == 1
    assert start["moves"]["e2e4"]["black"] == 1


def test_build_book_multiple_moves_from_same_position() -> None:
    """Multiple distinct moves from the same position are all recorded."""
    pgn = _make_pgn([
        (["e2e4"], "1-0"),
        (["d2d4"], "1-0"),
        (["c2c4"], "1-0"),
    ])
    book = _build_book(pgn, "white", max_plies=1)
    moves_in_start = set(book[chess.STARTING_FEN]["moves"].keys())
    assert moves_in_start == {"e2e4", "d2d4", "c2c4"}


def test_build_book_respects_max_plies() -> None:
    """Moves beyond max_plies must not be recorded."""
    pgn = _make_pgn([
        (["e2e4", "e7e5", "g1f3", "b8c6", "f1b5"], "1-0"),
    ])
    # max_plies=2: walk 1.e4 e5 then stop
    book = _build_book(pgn, "white", max_plies=2)

    # Position after 1.e4 e5 (White's turn, ply 2) is NOT included since
    # ply==2 is the boundary — ply 0 (start) and ply 2 (Nf3 position).
    # After e4 e5, ply=2 is White's Nf3. With max_plies=2 the loop breaks
    # BEFORE processing ply 2.
    b = chess.Board()
    b.push(chess.Move.from_uci("e2e4"))
    b.push(chess.Move.from_uci("e7e5"))
    # ply 2 == Nf3 position (White's turn); this IS ply=2, which is >= max_plies=2
    # so the loop breaks before adding it.
    assert b.fen() not in book


def test_build_book_skips_unfinished_games() -> None:
    """Games with result '*' must be skipped."""
    pgn = (
        '[Event "Test"]\n[White "A"]\n[Black "B"]\n[Result "*"]\n\n'
        "1. e4 e5 *\n\n"
    )
    book = _build_book(pgn, "white", max_plies=4)
    assert len(book) == 0


# ---------------------------------------------------------------------------
# _to_payload
# ---------------------------------------------------------------------------


def test_to_payload_structure() -> None:
    pos = {
        "white": 3, "draws": 1, "black": 1,
        "moves": {
            "e2e4": {"white": 2, "draws": 1, "black": 0},
            "d2d4": {"white": 1, "draws": 0, "black": 1},
        },
    }
    payload = _to_payload(pos)
    assert payload["white"] == 3
    assert payload["draws"] == 1
    assert payload["black"] == 1
    assert len(payload["moves"]) == 2
    # Sorted by total descending: e4 (3 total) before d4 (2 total).
    assert payload["moves"][0]["uci"] == "e2e4"
    assert payload["moves"][1]["uci"] == "d2d4"


# ---------------------------------------------------------------------------
# _merge_payloads
# ---------------------------------------------------------------------------


def test_merge_payloads_adds_counts() -> None:
    existing = {
        "white": 10, "draws": 5, "black": 3,
        "moves": [
            {"uci": "e2e4", "white": 8, "draws": 4, "black": 2, "averageRating": 0},
        ],
    }
    new = {
        "white": 5, "draws": 2, "black": 1,
        "moves": [
            {"uci": "e2e4", "white": 4, "draws": 1, "black": 1, "averageRating": 0},
            {"uci": "d2d4", "white": 1, "draws": 1, "black": 0, "averageRating": 0},
        ],
    }
    merged = _merge_payloads(existing, new)

    assert merged["white"] == 15
    assert merged["draws"] == 7
    assert merged["black"] == 4

    moves_by_uci = {m["uci"]: m for m in merged["moves"]}
    assert moves_by_uci["e2e4"]["white"] == 12
    assert moves_by_uci["e2e4"]["draws"] == 5
    assert moves_by_uci["d2d4"]["white"] == 1


def test_merge_payloads_adds_new_move() -> None:
    existing = {
        "white": 5, "draws": 0, "black": 0,
        "moves": [{"uci": "e2e4", "white": 5, "draws": 0, "black": 0, "averageRating": 0}],
    }
    new = {
        "white": 2, "draws": 0, "black": 0,
        "moves": [{"uci": "d2d4", "white": 2, "draws": 0, "black": 0, "averageRating": 0}],
    }
    merged = _merge_payloads(existing, new)
    ucis = {m["uci"] for m in merged["moves"]}
    assert "e2e4" in ucis
    assert "d2d4" in ucis


# ---------------------------------------------------------------------------
# fetch_player_games (mocked HTTP)
# ---------------------------------------------------------------------------


def test_fetch_player_games_writes_to_cache(tmp_path: Path) -> None:
    """A successful download should populate the cache with position data."""
    pgn = _make_pgn([
        (["e2e4", "e7e5", "g1f3"], "1-0"),
        (["d2d4", "d7d5"], "1/2-1/2"),
    ])

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = pgn
    mock_resp.raise_for_status = MagicMock()

    from mysecond.cache import Cache

    with patch("mysecond.fetcher.requests.Session") as mock_cls:
        mock_session = MagicMock()
        mock_session.get.return_value = mock_resp
        mock_cls.return_value = mock_session

        with Cache(tmp_path / "cache.sqlite") as cache:
            count = fetch_player_games(
                "testuser", "white", cache,
                speeds="blitz", max_plies=6, max_games=100,
                verbose=False,
            )

    assert count > 0  # at least the starting position was indexed


def test_fetch_player_games_returns_zero_on_empty_response(tmp_path: Path) -> None:
    """Empty PGN response should return 0 and not crash."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = ""
    mock_resp.raise_for_status = MagicMock()

    from mysecond.cache import Cache

    with patch("mysecond.fetcher.requests.Session") as mock_cls:
        mock_session = MagicMock()
        mock_session.get.return_value = mock_resp
        mock_cls.return_value = mock_session

        with Cache(tmp_path / "cache.sqlite") as cache:
            count = fetch_player_games(
                "ghost", "white", cache, verbose=False
            )

    assert count == 0


def test_fetch_player_games_raises_on_http_error(tmp_path: Path) -> None:
    """HTTP errors from the games API should raise RuntimeError."""
    import requests as req_lib
    from mysecond.cache import Cache

    with patch("mysecond.fetcher.requests.Session") as mock_cls:
        mock_session = MagicMock()
        mock_session.get.side_effect = req_lib.RequestException("timeout")
        mock_cls.return_value = mock_session

        with Cache(tmp_path / "cache.sqlite") as cache:
            with pytest.raises(RuntimeError, match="Failed to download"):
                fetch_player_games("ghost", "white", cache, verbose=False)


# ---------------------------------------------------------------------------
# last_fetch_ts
# ---------------------------------------------------------------------------


def test_last_fetch_ts_none_before_fetch(tmp_path: Path) -> None:
    from mysecond.cache import Cache

    with Cache(tmp_path / "cache.sqlite") as cache:
        ts = last_fetch_ts("nobody", "white", "blitz", cache)
    assert ts is None


def test_last_fetch_ts_set_after_fetch(tmp_path: Path) -> None:
    pgn = _make_pgn([(["e2e4"], "1-0")])
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = pgn
    mock_resp.raise_for_status = MagicMock()

    from mysecond.cache import Cache

    with patch("mysecond.fetcher.requests.Session") as mock_cls:
        mock_session = MagicMock()
        mock_session.get.return_value = mock_resp
        mock_cls.return_value = mock_session

        with Cache(tmp_path / "cache.sqlite") as cache:
            fetch_player_games(
                "testuser", "white", cache,
                speeds="blitz", verbose=False,
            )
            ts = last_fetch_ts("testuser", "white", "blitz", cache)

    assert ts is not None
    assert ts > 0
