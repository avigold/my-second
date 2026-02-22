"""Tests for the engine wrapper (no real Stockfish required)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import chess
import chess.engine
import pytest

from mysecond.engine import Engine, find_stockfish


# ---------------------------------------------------------------------------
# find_stockfish
# ---------------------------------------------------------------------------


def test_find_stockfish_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = tmp_path / "stockfish"
    fake.touch()
    monkeypatch.setenv("MYSECOND_STOCKFISH_PATH", str(fake))
    assert find_stockfish() == fake


def test_find_stockfish_env_var_missing_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MYSECOND_STOCKFISH_PATH", "/nonexistent/stockfish")
    with pytest.raises(FileNotFoundError, match="MYSECOND_STOCKFISH_PATH"):
        find_stockfish()


def test_find_stockfish_via_which(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MYSECOND_STOCKFISH_PATH", raising=False)
    with patch("mysecond.engine.shutil.which", return_value="/usr/bin/stockfish"):
        result = find_stockfish()
    assert result == Path("/usr/bin/stockfish")


def test_find_stockfish_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MYSECOND_STOCKFISH_PATH", raising=False)
    with patch("mysecond.engine.shutil.which", return_value=None):
        with pytest.raises(FileNotFoundError, match="stockfish not found"):
            find_stockfish()


# ---------------------------------------------------------------------------
# Engine wrapper
# ---------------------------------------------------------------------------


def _mock_engine_returning(info: dict) -> MagicMock:
    """Return a mock SimpleEngine whose analyse() returns *info*."""
    mock = MagicMock()
    mock.analyse.return_value = info
    return mock


def _make_info() -> dict:
    return {
        "score": chess.engine.PovScore(chess.engine.Cp(50), chess.WHITE),
        "pv": [chess.Move.from_uci("e2e4")],
        "depth": 10,
    }


def test_analyse_multipv_wraps_single_dict() -> None:
    """analyse_multipv should always return a list even if engine gives one dict."""
    mock = _mock_engine_returning(_make_info())
    with patch("chess.engine.SimpleEngine.popen_uci", return_value=mock):
        eng = Engine(Path("/fake/sf"))
        result = eng.analyse_multipv(chess.Board(), depth=10, multipv=1)
        eng.close()

    assert isinstance(result, list)
    assert len(result) == 1


def test_analyse_multipv_preserves_list() -> None:
    """analyse_multipv should pass through a list from the engine unchanged."""
    mock = _mock_engine_returning([_make_info(), _make_info()])
    with patch("chess.engine.SimpleEngine.popen_uci", return_value=mock):
        eng = Engine(Path("/fake/sf"))
        result = eng.analyse_multipv(chess.Board(), depth=10, multipv=2)
        eng.close()

    assert isinstance(result, list)
    assert len(result) == 2


def test_analyse_single_unwraps_list() -> None:
    """analyse_single should unwrap a list and return a single InfoDict."""
    mock = _mock_engine_returning([_make_info()])
    with patch("chess.engine.SimpleEngine.popen_uci", return_value=mock):
        eng = Engine(Path("/fake/sf"))
        result = eng.analyse_single(chess.Board(), depth=10)
        eng.close()

    assert isinstance(result, dict)


def test_engine_context_manager_calls_quit() -> None:
    """Engine.__exit__ must call engine.quit()."""
    mock = _mock_engine_returning(_make_info())
    with patch("chess.engine.SimpleEngine.popen_uci", return_value=mock):
        with Engine(Path("/fake/sf")):
            pass
    mock.quit.assert_called_once()
