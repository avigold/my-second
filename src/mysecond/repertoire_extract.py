"""Reconstruct a player's opening repertoire from the local game cache.

How it works
------------
The cache is populated automatically if empty — no separate fetch step is
required.  The cache contains per-position statistics for every position the
player reached (at their turn).  This module walks that data as a tree:

* **At the player's turn**: look up the position in the cache and follow
  every move they played at least ``min_games`` times.

* **At the opponent's turn**: there is no explicit opponent data, but we
  can *infer* opponent moves by checking which legal moves lead to a
  position that exists in the player's cache.  If the player reached
  position P'' in N games, then some opponent move led there from the
  prior position — we recover it by exhausting legal moves and checking
  which ones land on a cached FEN.  The most-common opponent responses
  (ranked by how often the player reached the resulting position) come
  first in the variation list.

Output
------
An annotated PGN with branching variations — one branch per meaningful
player choice.  Each player move is annotated with a frequency comment::

    { 42/67 games (63%) }

The mainline at each choice point is the most-played option; alternatives
are encoded as PGN variations, importable into ChessBase, Lichess, etc.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import chess
import chess.pgn

from .cache import Cache
from .fetcher import _backend_key, fetch_player_games, fetch_player_games_chesscom


@dataclass
class RepertoireStats:
    total_positions: int      # unique positions visited
    total_player_moves: int   # unique (position, move) pairs recorded
    max_depth_reached: int    # deepest ply reached


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_repertoire(
    username: str,
    color: str,
    cache: Cache,
    speeds: str = "blitz,rapid,classical",
    platform: str = "lichess",
    min_games: int = 5,
    max_plies: int = 20,
    root_fen: str = chess.STARTING_FEN,
    verbose: bool = True,
) -> tuple[chess.pgn.Game, RepertoireStats]:
    """Build an opening repertoire PGN from the player's cached game data.

    Parameters
    ----------
    username:
        Player username (used to locate the cache key).
    color:
        ``'white'`` or ``'black'``.
    cache:
        Open :class:`~mysecond.cache.Cache` instance.
    speeds:
        Time controls — must match the ``fetch-player-games`` run.
    platform:
        ``'lichess'`` or ``'chesscom'`` — must match the fetch platform.
    min_games:
        A player move must appear at least this many times to be included.
    max_plies:
        Maximum depth of the repertoire tree (half-moves from root).
    root_fen:
        Starting position; defaults to the standard starting position.
    verbose:
        Print progress messages.

    Returns
    -------
    tuple[chess.pgn.Game, RepertoireStats]
        Annotated PGN game and extraction statistics.
    """
    backend = _backend_key(username, color, speeds, platform=platform)
    player_color = chess.WHITE if color == "white" else chess.BLACK

    if verbose:
        print(
            f"[repertoire] Loading cache for {username} "
            f"({color}, {speeds}, {platform}) …",
            flush=True,
        )

    # Load the full backend into memory for O(1) lookup during traversal.
    all_rows = cache.scan_backend(backend)
    cache_index: dict[str, dict[str, Any]] = {
        fen: payload
        for fen, payload in all_rows
        if not fen.startswith("_")   # exclude metadata entries
    }

    if verbose:
        print(f"[repertoire] {len(cache_index)} cached positions loaded.", flush=True)

    if not cache_index:
        if verbose:
            print(
                f"[repertoire] No cached data found — fetching games from {platform} …",
                flush=True,
            )
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
        all_rows = cache.scan_backend(backend)
        cache_index = {
            fen: payload
            for fen, payload in all_rows
            if not fen.startswith("_")
        }
        if not cache_index:
            raise RuntimeError(
                f"No games found after fetch — check username and speeds."
            )

    # Build the PGN game skeleton.
    game = chess.pgn.Game()
    game.headers["Event"]     = f"{username} Repertoire ({color.title()})"
    game.headers["White"]     = username if color == "white" else "Opponent"
    game.headers["Black"]     = "Opponent" if color == "white" else username
    game.headers["Result"]    = "*"
    game.headers["Annotator"] = "mysecond"
    if root_fen != chess.STARTING_FEN:
        game.headers["FEN"]   = root_fen
        game.headers["SetUp"] = "1"
        game.setup(chess.Board(root_fen))

    stats: dict[str, int] = {"positions": 0, "moves": 0, "max_depth": 0}
    visited: set[str] = set()

    _walk(
        node=game,
        board=chess.Board(root_fen),
        player_color=player_color,
        cache_index=cache_index,
        min_games=min_games,
        max_plies=max_plies,
        depth=0,
        visited=visited,
        stats=stats,
        verbose=verbose,
    )

    if verbose:
        print(
            f"[repertoire] Done. "
            f"{stats['positions']} positions visited, "
            f"{stats['moves']} player moves recorded, "
            f"max depth {stats['max_depth']} plies.",
            flush=True,
        )

    return game, RepertoireStats(
        total_positions=stats["positions"],
        total_player_moves=stats["moves"],
        max_depth_reached=stats["max_depth"],
    )


def export_repertoire_pgn(game: chess.pgn.Game, out_path: Path) -> None:
    """Write the repertoire game tree to a PGN file."""
    buf = io.StringIO()
    exporter = chess.pgn.FileExporter(buf)
    game.accept(exporter)
    out_path.write_text(buf.getvalue(), encoding="utf-8")


# ---------------------------------------------------------------------------
# Tree walker
# ---------------------------------------------------------------------------


def _walk(
    node: chess.pgn.GameNode,
    board: chess.Board,
    player_color: chess.Color,
    cache_index: dict[str, dict[str, Any]],
    min_games: int,
    max_plies: int,
    depth: int,
    visited: set[str],
    stats: dict[str, int],
    verbose: bool,
) -> None:
    """Recursively build the repertoire tree."""
    fen = board.fen()

    if depth >= max_plies or fen in visited:
        return

    visited.add(fen)
    stats["positions"] += 1
    if depth > stats["max_depth"]:
        stats["max_depth"] = depth

    if board.turn == player_color:
        _walk_player_turn(node, board, player_color, cache_index,
                          min_games, max_plies, depth, visited, stats, verbose)
    else:
        _walk_opponent_turn(node, board, player_color, cache_index,
                            min_games, max_plies, depth, visited, stats, verbose)


def _walk_player_turn(
    node: chess.pgn.GameNode,
    board: chess.Board,
    player_color: chess.Color,
    cache_index: dict[str, dict[str, Any]],
    min_games: int,
    max_plies: int,
    depth: int,
    visited: set[str],
    stats: dict[str, int],
    verbose: bool,
) -> None:
    data = cache_index.get(board.fen())
    if data is None:
        return

    total = data.get("white", 0) + data.get("draws", 0) + data.get("black", 0)
    if total < min_games:
        return

    qualifying: list[tuple[chess.Move, int]] = []
    for m in data.get("moves", []):
        g = m.get("white", 0) + m.get("draws", 0) + m.get("black", 0)
        if g < min_games:
            continue
        try:
            move = chess.Move.from_uci(m["uci"])
        except (ValueError, KeyError):
            continue
        if move not in board.legal_moves:
            continue
        qualifying.append((move, g))

    if not qualifying:
        return

    # Most-played move first → becomes mainline; rest are variations.
    qualifying.sort(key=lambda x: -x[1])

    # Build a quick lookup: uci → move entry (for WDL data)
    move_data_by_uci: dict[str, dict] = {
        m.get("uci", ""): m for m in data.get("moves", [])
    }

    for i, (move, g) in enumerate(qualifying):
        pct = g / total * 100 if total else 0
        child = node.add_main_variation(move) if i == 0 else node.add_variation(move)

        md = move_data_by_uci.get(move.uci(), {})
        if player_color == chess.WHITE:
            m_wins   = md.get("white", 0)
            m_draws  = md.get("draws", 0)
            m_losses = md.get("black", 0)
        else:
            m_wins   = md.get("black", 0)
            m_draws  = md.get("draws", 0)
            m_losses = md.get("white", 0)

        child.comment = (
            f"{g}/{total} games ({pct:.0f}%) "
            f"W:{m_wins} D:{m_draws} L:{m_losses}"
        )
        stats["moves"] += 1

        new_board = board.copy()
        new_board.push(move)
        _walk(child, new_board, player_color, cache_index,
              min_games, max_plies, depth + 1, visited, stats, verbose)


def _walk_opponent_turn(
    node: chess.pgn.GameNode,
    board: chess.Board,
    player_color: chess.Color,
    cache_index: dict[str, dict[str, Any]],
    min_games: int,
    max_plies: int,
    depth: int,
    visited: set[str],
    stats: dict[str, int],
    verbose: bool,
) -> None:
    # Discover opponent moves by checking which legal moves lead to a
    # position the player has reached (i.e. exists in the cache index).
    reachable: list[tuple[chess.Move, int]] = []
    for move in board.legal_moves:
        new_board = board.copy()
        new_board.push(move)
        next_fen = new_board.fen()
        if next_fen not in cache_index:
            continue
        next_data = cache_index[next_fen]
        total = (next_data.get("white", 0) + next_data.get("draws", 0)
                 + next_data.get("black", 0))
        if total >= min_games:
            reachable.append((move, total))

    if not reachable:
        return

    # Most-common opponent response (most games reaching next player position) first.
    reachable.sort(key=lambda x: -x[1])

    for i, (move, _) in enumerate(reachable):
        child = node.add_main_variation(move) if i == 0 else node.add_variation(move)
        new_board = board.copy()
        new_board.push(move)
        _walk(child, new_board, player_color, cache_index,
              min_games, max_plies, depth + 1, visited, stats, verbose)
