"""Tests for the PGN export layer."""

from __future__ import annotations

import io
from pathlib import Path

import chess
import chess.pgn
import pytest

from mysecond.export import export_pgn
from mysecond.models import EngineEval, NoveltyLine, ScoredNovelty

_START_FEN = chess.STARTING_FEN
# Position after 1.e4 (white has played, black to move)
_POST_E4_FEN = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _evals(*cp_values: int) -> dict[int, EngineEval]:
    depths = [20, 24, 28][: len(cp_values)]
    return {
        depth: EngineEval(depth=depth, cp_white=cp, mate_white=None)
        for depth, cp in zip(depths, cp_values)
    }


def _scored(
    book_moves: list[str],
    novelty_move: str,
    cp: int = 50,
    post_games: int = 0,
) -> ScoredNovelty:
    nov = NoveltyLine(
        book_moves=book_moves,
        novelty_move=novelty_move,
        novelty_ply=len(book_moves),
        evals=_evals(cp, cp + 2, cp - 2),
        pre_novelty_games=5000,
        post_novelty_games=post_games,
        continuations=[],
    )
    return ScoredNovelty(
        novelty=nov,
        eval_cp=float(cp),
        stability=2.0,
        score=float(cp) - 16.0 + 20.0,
    )


def _read_games(path: Path) -> list[chess.pgn.Game]:
    text = path.read_text(encoding="utf-8")
    buf = io.StringIO(text)
    games: list[chess.pgn.Game] = []
    while True:
        game = chess.pgn.read_game(buf)
        if game is None:
            break
        games.append(game)
    return games


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_creates_output_file(tmp_path: Path) -> None:
    out = tmp_path / "ideas.pgn"
    export_pgn([_scored(["e2e4", "e7e5", "g1f3"], "b8c6")], _START_FEN, out)
    assert out.exists()
    assert out.stat().st_size > 0


def test_one_game_per_novelty(tmp_path: Path) -> None:
    out = tmp_path / "ideas.pgn"
    novelties = [
        _scored(["e2e4", "e7e5"], "g1f3"),
        _scored(["d2d4", "d7d5"], "c2c4"),
        _scored(["c2c4"], "e7e5"),
    ]
    export_pgn(novelties, _START_FEN, out)
    games = _read_games(out)
    assert len(games) == 3


def test_novelty_nag_present(tmp_path: Path) -> None:
    """$146 (ChessBase novelty NAG) must appear in the PGN."""
    out = tmp_path / "ideas.pgn"
    export_pgn([_scored(["e2e4", "e7e5"], "g1f3")], _START_FEN, out)
    content = out.read_text(encoding="utf-8")
    assert "$146" in content


def test_novelty_comment_fields(tmp_path: Path) -> None:
    out = tmp_path / "ideas.pgn"
    export_pgn([_scored(["e2e4", "e7e5"], "g1f3")], _START_FEN, out)
    content = out.read_text(encoding="utf-8")
    assert "N |" in content          # novelty marker
    assert "[%eval" in content        # ChessBase eval annotation
    assert "stability=" in content
    assert "pre=" in content
    assert "post=" in content
    assert "score=" in content


def test_game_comment_contains_rank_and_novelty_move(tmp_path: Path) -> None:
    out = tmp_path / "ideas.pgn"
    export_pgn([_scored(["e2e4", "e7e5"], "g1f3")], _START_FEN, out)
    games = _read_games(out)
    assert "Rank 1" in games[0].comment
    assert "Novelty:" in games[0].comment


def test_result_header_is_asterisk(tmp_path: Path) -> None:
    out = tmp_path / "ideas.pgn"
    export_pgn([_scored(["e2e4"], "e7e5")], _START_FEN, out)
    games = _read_games(out)
    assert games[0].headers["Result"] == "*"


def test_non_starting_fen_sets_setup_header(tmp_path: Path) -> None:
    out = tmp_path / "ideas.pgn"
    # After 1.e4 black to move; novelty is ...e5
    export_pgn([_scored([], "e7e5", cp=10)], _POST_E4_FEN, out)
    games = _read_games(out)
    assert games[0].headers.get("SetUp") == "1"
    stored = games[0].headers.get("FEN", "")
    assert "4P3" in stored  # piece layout preserved


def test_games_are_valid_pgn(tmp_path: Path) -> None:
    out = tmp_path / "ideas.pgn"
    export_pgn(
        [_scored(["e2e4", "e7e5", "g1f3", "b8c6"], "f1b5")],
        _START_FEN,
        out,
    )
    games = _read_games(out)
    assert len(games) == 1
    board = games[0].board()
    for node in games[0].mainline():
        assert node.move in board.legal_moves
        board.push(node.move)


def test_creates_parent_directory(tmp_path: Path) -> None:
    out = tmp_path / "nested" / "dir" / "ideas.pgn"
    export_pgn([_scored(["e2e4"], "e7e5", cp=10)], _START_FEN, out)
    assert out.exists()


def test_brilliant_nag_for_high_eval(tmp_path: Path) -> None:
    """eval_cp >= 75 should add $3 (brilliant) in addition to $146."""
    out = tmp_path / "ideas.pgn"
    export_pgn([_scored(["e2e4"], "e7e5", cp=100)], _START_FEN, out)
    content = out.read_text(encoding="utf-8")
    assert "$3" in content


def test_mate_eval_annotation(tmp_path: Path) -> None:
    out = tmp_path / "ideas.pgn"
    nov = NoveltyLine(
        book_moves=["e2e4"],
        novelty_move="e7e5",
        novelty_ply=1,
        evals={20: EngineEval(depth=20, cp_white=None, mate_white=5)},
        pre_novelty_games=1000,
        post_novelty_games=0,
        continuations=[],
    )
    sn = ScoredNovelty(novelty=nov, eval_cp=10000.0, stability=0.0, score=10030.0)
    export_pgn([sn], _START_FEN, out)
    content = out.read_text(encoding="utf-8")
    assert "[%eval #+" in content
