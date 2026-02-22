"""Tests for the SQLite explorer cache."""

from __future__ import annotations

from pathlib import Path

import pytest

from mysecond.cache import Cache

_FEN = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1"
_BACKEND = "lichess_masters"


def test_cache_miss(tmp_path: Path) -> None:
    with Cache(tmp_path / "test.sqlite") as cache:
        assert cache.get(_FEN, _BACKEND) is None


def test_cache_hit(tmp_path: Path) -> None:
    payload = {"white": 100, "draws": 50, "black": 30}
    with Cache(tmp_path / "test.sqlite") as cache:
        cache.set(_FEN, _BACKEND, payload)
        assert cache.get(_FEN, _BACKEND) == payload


def test_cache_overwrite(tmp_path: Path) -> None:
    with Cache(tmp_path / "test.sqlite") as cache:
        cache.set(_FEN, _BACKEND, {"v": 1})
        cache.set(_FEN, _BACKEND, {"v": 2})
        assert cache.get(_FEN, _BACKEND) == {"v": 2}


def test_cache_different_backends(tmp_path: Path) -> None:
    with Cache(tmp_path / "test.sqlite") as cache:
        cache.set(_FEN, "backend_a", {"a": 1})
        cache.set(_FEN, "backend_b", {"b": 2})
        assert cache.get(_FEN, "backend_a") == {"a": 1}
        assert cache.get(_FEN, "backend_b") == {"b": 2}


def test_cache_different_fens(tmp_path: Path) -> None:
    fen2 = "rnbqkbnr/pppppppp/8/8/3P4/8/PPP1PPPP/RNBQKBNR b KQkq d3 0 1"
    with Cache(tmp_path / "test.sqlite") as cache:
        cache.set(_FEN, _BACKEND, {"fen": "first"})
        cache.set(fen2, _BACKEND, {"fen": "second"})
        assert cache.get(_FEN, _BACKEND) == {"fen": "first"}
        assert cache.get(fen2, _BACKEND) == {"fen": "second"}


def test_cache_persists_across_instances(tmp_path: Path) -> None:
    db = tmp_path / "persist.sqlite"
    payload = {"white": 200, "draws": 80, "black": 40}
    with Cache(db) as c1:
        c1.set(_FEN, _BACKEND, payload)
    with Cache(db) as c2:
        assert c2.get(_FEN, _BACKEND) == payload


def test_cache_creates_parent_directory(tmp_path: Path) -> None:
    nested_db = tmp_path / "a" / "b" / "cache.sqlite"
    with Cache(nested_db) as cache:
        cache.set("fen", "backend", {"ok": True})
    assert nested_db.exists()
