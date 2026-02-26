"""Generate a strategic preparation brief for a player vs an opponent.

How it works
------------
1. Auto-fetch both players' games if their caches are empty.
2. Run habit analysis on both players with Stockfish.
3. Compute style profiles from each player's cache (draw rate, solidness,
   opening diversity, win rates, decoded opening lines).
4. Fetch a sample of raw games and compute game-phase statistics.
5. Find opening battlegrounds: positions where both players have cache data
   one move apart, so a direct comparison is possible.
6. Map opponent weaknesses reachable from the player's own repertoire.
7. Map the player's prep gaps where the opponent is strong.
8. Optionally call the Claude API to synthesise a strategic brief.
9. Write a structured JSON report to ``out_path``.
"""

from __future__ import annotations

import dataclasses
import json
import os
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import chess

from .cache import Cache
from .engine import Engine
from .eval_cache import EvalCache
from .fetcher import _backend_key, fetch_player_games, fetch_player_games_chesscom
from .game_phases import analyze_game_phases
from .habits import HabitInaccuracy, analyze_habits

# When running two habit analyses in parallel each gets half the cores.
_PARALLEL_THREADS = max(1, (os.cpu_count() or 2) // 2)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def strategise(
    player: str,
    player_platform: str,
    player_color: str,
    player_speeds: str,
    opponent: str,
    opponent_platform: str,
    opponent_speeds: str,
    cache: Cache,
    engine_path: Path,
    out_path: Path,
    min_games: int = 5,
    max_positions: int = 30,
    min_eval_gap: int = 25,
    depth: int = 12,
    anthropic_api_key: str | None = None,
    verbose: bool = True,
    eval_cache: EvalCache | None = None,
) -> dict[str, Any]:
    """Run full strategic analysis and write ``out_path`` JSON.

    Returns the result dict (same content as the JSON file).
    """
    opponent_color = "black" if player_color == "white" else "white"

    _banner(player, player_color, player_platform, opponent, opponent_color,
            opponent_platform, verbose)

    # ── 1. Load / fetch caches ───────────────────────────────────────────────
    player_backend   = _backend_key(player,   player_color,   player_speeds,   platform=player_platform)
    opponent_backend = _backend_key(opponent, opponent_color, opponent_speeds, platform=opponent_platform)

    player_index   = _load_index(cache, player_backend)
    opponent_index = _load_index(cache, opponent_backend)

    if not player_index:
        _log(verbose, f"[strategise] No cache for {player} ({player_platform}, {player_color}, {player_speeds}) — fetching …")
        _fetch(player, player_color, player_platform, player_speeds, cache, verbose)
        player_index = _load_index(cache, player_backend)

    if not opponent_index:
        _log(verbose, f"[strategise] No cache for {opponent} ({opponent_platform}, {opponent_color}, {opponent_speeds}) — fetching …")
        _fetch(opponent, opponent_color, opponent_platform, opponent_speeds, cache, verbose)
        opponent_index = _load_index(cache, opponent_backend)

    _log(verbose,
         f"[strategise] {len(player_index)} positions for {player}, "
         f"{len(opponent_index)} for {opponent}.")

    # ── 2. Habit analysis + phase analysis (all in parallel) ─────────────────
    _log(verbose, f"[strategise] Analysing habits and game phases in parallel …")

    def _habits(username, color, speeds, platform):
        return analyze_habits(
            username=username, color=color, cache=cache,
            engine_path=engine_path, speeds=speeds, platform=platform,
            min_games=min_games, max_positions=max_positions,
            min_eval_gap=min_eval_gap, depth=depth, verbose=verbose,
            engine_threads=_PARALLEL_THREADS,
            eval_cache=eval_cache,
        )

    def _phases(username, color, platform, speeds):
        return analyze_game_phases(
            username=username, color=color, platform=platform,
            speeds=speeds, max_games=200, verbose=verbose,
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        opp_future        = pool.submit(_habits, opponent, opponent_color, opponent_speeds, opponent_platform)
        plr_future        = pool.submit(_habits, player,   player_color,   player_speeds,   player_platform)
        plr_phase_future  = pool.submit(_phases, player,   player_color,   player_platform, player_speeds)
        opp_phase_future  = pool.submit(_phases, opponent, opponent_color, opponent_platform, opponent_speeds)

        opponent_habits  = opp_future.result()
        player_habits    = plr_future.result()
        player_phase     = plr_phase_future.result()
        opponent_phase   = opp_phase_future.result()

    _log(verbose, f"[strategise] {len(opponent_habits)} opponent + {len(player_habits)} player habit inaccuracies found.")

    # ── 3. Style profiles ────────────────────────────────────────────────────
    _log(verbose, "[strategise] Computing style profiles …")
    player_style   = _compute_style_profile(player_index,   player_color)
    opponent_style = _compute_style_profile(opponent_index, opponent_color)

    # ── 4. Battlegrounds ─────────────────────────────────────────────────────
    _log(verbose, "[strategise] Finding opening battlegrounds …")
    battlegrounds = _compute_battlegrounds(
        player_index, opponent_index, player_color, min_games=min_games,
    )
    _log(verbose, f"[strategise] {len(battlegrounds)} battleground positions found.")

    # ── 5. Opponent weaknesses reachable from player repertoire ──────────────
    _log(verbose, "[strategise] Mapping opponent weaknesses reachable from player repertoire …")
    opponent_weaknesses = _reachable_weaknesses(
        opponent_habits, player_index, rank_limit=max_positions,
    )
    _log(verbose, f"[strategise] {len(opponent_weaknesses)} reachable opponent weaknesses.")

    # ── 6. Player prep gaps ──────────────────────────────────────────────────
    _log(verbose, "[strategise] Mapping player prep gaps …")
    prep_gaps = _prep_gaps(
        player_habits, opponent_index, rank_limit=max_positions,
    )
    _log(verbose, f"[strategise] {len(prep_gaps)} player prep gaps identified.")

    # ── 7. Key positions ─────────────────────────────────────────────────────
    key_positions = _key_positions(battlegrounds, opponent_weaknesses, prep_gaps)

    # ── 8. Claude API ────────────────────────────────────────────────────────
    strategic_brief = ""
    ai_available    = False
    if anthropic_api_key:
        _log(verbose, "[strategise] Calling Claude API for strategic brief …")
        try:
            strategic_brief = _call_claude(
                api_key=anthropic_api_key,
                player=player, player_color=player_color, player_platform=player_platform,
                opponent=opponent, opponent_color=opponent_color, opponent_platform=opponent_platform,
                player_style=player_style, opponent_style=opponent_style,
                player_phase=player_phase, opponent_phase=opponent_phase,
                battlegrounds=battlegrounds,
                opponent_weaknesses=opponent_weaknesses,
                prep_gaps=prep_gaps,
            )
            ai_available = True
            _log(verbose, "[strategise] Strategic brief generated.")
        except Exception as exc:
            _log(verbose, f"[strategise] Claude API call failed: {exc}")
    else:
        _log(verbose, "[strategise] No API key — skipping AI brief.")

    # ── 9. Assemble and write result ─────────────────────────────────────────
    result: dict[str, Any] = {
        "player": {
            "username": player,
            "platform": player_platform,
            "color":    player_color,
            "speeds":   player_speeds,
        },
        "opponent": {
            "username": opponent,
            "platform": opponent_platform,
            "color":    opponent_color,
            "speeds":   opponent_speeds,
        },
        "player_style":          player_style,
        "opponent_style":        opponent_style,
        "player_phase_stats":    player_phase,
        "opponent_phase_stats":  opponent_phase,
        "battlegrounds":         battlegrounds,
        "opponent_weaknesses":   opponent_weaknesses,
        "prep_gaps":             prep_gaps,
        "key_positions":         key_positions,
        "strategic_brief":       strategic_brief,
        "ai_available":          ai_available,
        "generated_at":          datetime.now(timezone.utc).isoformat(),
    }

    out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    _log(verbose, f"[strategise] Report written to {out_path}")

    print(
        f"\n[strategise] Complete.\n"
        f"  Battlegrounds:        {len(battlegrounds)}\n"
        f"  Opponent weaknesses:  {len(opponent_weaknesses)}\n"
        f"  Player prep gaps:     {len(prep_gaps)}\n"
        f"  AI brief:             {'yes' if ai_available else 'no (no API key)'}",
        flush=True,
    )
    return result


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------


def _load_index(cache: Cache, backend: str) -> dict[str, dict[str, Any]]:
    rows = cache.scan_backend(backend)
    return {fen: payload for fen, payload in rows if not fen.startswith("_")}


def _fetch(username: str, color: str, platform: str, speeds: str,
           cache: Cache, verbose: bool) -> None:
    if platform == "chesscom":
        fetch_player_games_chesscom(
            username=username, color=color, cache=cache,
            speeds=speeds, verbose=verbose,
        )
    else:
        fetch_player_games(
            username=username, color=color, cache=cache,
            speeds=speeds, verbose=verbose,
        )


# ---------------------------------------------------------------------------
# Opening lines decoder
# ---------------------------------------------------------------------------


def _build_opening_lines(
    cache_index: dict[str, dict[str, Any]],
    color: str,
    top_n: int = 10,
) -> list[dict[str, Any]]:
    """BFS from the starting position through the cache to decode opening move sequences.

    Returns a list of dicts sorted by game count::

        {"move_sequence": "1.e4 c5 2.Nf3 d6", "games": 420, "win_rate": 0.52, "fen": "..."}

    The BFS alternates between:
    - Player's positions (from cache): expand the top player moves.
    - Opponent's positions: try every legal move and keep only those that land
      in a cached player position (very selective, fast in practice).
    """
    if not cache_index:
        return []

    player_chess = chess.WHITE if color == "white" else chess.BLACK
    start_board  = chess.Board()
    start_fen    = start_board.fen()

    # Early-exit for white player if the root isn't in the cache.
    if player_chess == chess.WHITE and start_fen not in cache_index:
        return []

    # path[fen] = ordered list of SAN strings from move 1
    # e.g. ["e4", "c5", "Nf3", "d6"]  →  "1.e4 c5 2.Nf3 d6"
    path: dict[str, list[str]] = {}

    queue: deque[tuple[chess.Board, list[str]]] = deque([(start_board.copy(), [])])
    visited: set[str] = {start_fen}
    max_nodes = 5_000

    while queue and len(visited) < max_nodes:
        board, sans = queue.popleft()

        if len(sans) >= 12:            # cap at ~6 full moves
            continue

        fen = board.fen()

        if board.turn == player_chess:
            # Player's position — use the cache.
            payload = cache_index.get(fen)
            if payload is None:
                continue

            # Record the path to this position (skip the root itself).
            if sans:
                path[fen] = sans[:]

            # Expand the player's top moves (up to 4).
            top_moves = sorted(
                payload.get("moves", []),
                key=lambda m: -(m.get("white", 0) + m.get("draws", 0) + m.get("black", 0)),
            )[:4]

            for move_data in top_moves:
                uci = move_data.get("uci", "")
                if not uci:
                    continue
                try:
                    move = chess.Move.from_uci(uci)
                    san  = board.san(move)
                    after = board.copy()
                    after.push(move)
                    next_fen = after.fen()
                except Exception:
                    continue

                if next_fen not in visited:
                    visited.add(next_fen)
                    queue.append((after, sans + [san]))

        else:
            # Opponent's position — enumerate all legal moves and keep only
            # those that lead to a cached player position.
            for opp_move in board.legal_moves:
                try:
                    opp_san = board.san(opp_move)
                    after   = board.copy()
                    after.push(opp_move)
                    next_fen = after.fen()
                except Exception:
                    continue

                if next_fen not in visited and next_fen in cache_index:
                    visited.add(next_fen)
                    queue.append((after, sans + [opp_san]))

    # Helper: convert ordered SAN list to PGN notation "1.e4 c5 2.Nf3 d6"
    def _to_pgn(sans: list[str]) -> str:
        parts: list[str] = []
        for i, san in enumerate(sans):
            if i % 2 == 0:
                parts.append(f"{i // 2 + 1}.{san}")
            else:
                parts.append(san)
        return " ".join(parts)

    # Pick the top_n positions by game count, build output.
    top_fens = sorted(
        cache_index.items(),
        key=lambda kv: -(kv[1].get("white", 0) + kv[1].get("draws", 0) + kv[1].get("black", 0)),
    )

    results: list[dict[str, Any]] = []
    seen_seqs: set[str] = set()

    for fen, payload in top_fens:
        if fen not in path or not path[fen]:
            continue

        pgn = _to_pgn(path[fen])
        if not pgn or pgn in seen_seqs:
            continue
        seen_seqs.add(pgn)

        g     = payload.get("white", 0) + payload.get("draws", 0) + payload.get("black", 0)
        w_cnt = payload.get("white", 0) if color == "white" else payload.get("black", 0)

        # Best next move from this position
        top_move_san = ""
        moves = payload.get("moves", [])
        if moves:
            top_m = max(moves, key=lambda m: m.get("white", 0) + m.get("draws", 0) + m.get("black", 0))
            uci = top_m.get("uci", "")
            if uci:
                try:
                    board_tmp = chess.Board(fen)
                    top_move_san = board_tmp.san(chess.Move.from_uci(uci))
                except Exception:
                    top_move_san = uci

        results.append({
            "move_sequence": pgn,
            "games":         g,
            "win_rate":      round(w_cnt / g if g > 0 else 0.0, 3),
            "fen":           fen,
            "top_move_san":  top_move_san,
        })

        if len(results) >= top_n:
            break

    return results


# ---------------------------------------------------------------------------
# Style profile
# ---------------------------------------------------------------------------


def _compute_style_profile(
    cache_index: dict[str, dict[str, Any]],
    color: str,
) -> dict[str, Any]:
    """Compute aggregate style metrics from a player's cache index."""
    if not cache_index:
        return _empty_profile()

    player_chess_color = chess.WHITE if color == "white" else chess.BLACK

    all_win_rates: list[float] = []
    total_moves_indexed = 0

    # Aggregate win/draw/loss across all positions for draw_rate.
    agg_white = agg_draws = agg_black = 0

    for fen, payload in cache_index.items():
        w = payload.get("white", 0)
        d = payload.get("draws", 0)
        b = payload.get("black", 0)
        total = w + d + b
        if total == 0:
            continue
        agg_white += w
        agg_draws += d
        agg_black += b
        win_rate = (w if color == "white" else b) / total
        all_win_rates.append(win_rate)
        total_moves_indexed += len(payload.get("moves", []))

    n = len(all_win_rates)
    if n == 0:
        return _empty_profile()

    avg_win_rate = sum(all_win_rates) / n
    solidness    = sum(1 for r in all_win_rates if r > 0.5) / n

    agg_total    = agg_white + agg_draws + agg_black or 1
    draw_rate    = agg_draws / agg_total
    decisive_rate = 1.0 - draw_rate

    # Opening diversity: 1 - (top_move_fraction at root)
    root_payload = cache_index.get(chess.STARTING_FEN, {})
    root_total   = (root_payload.get("white", 0)
                    + root_payload.get("draws", 0)
                    + root_payload.get("black", 0))
    root_moves   = root_payload.get("moves", [])
    if root_total > 0 and root_moves:
        top_root  = max(m.get("white", 0) + m.get("draws", 0) + m.get("black", 0)
                        for m in root_moves)
        diversity = 1.0 - top_root / root_total
    else:
        diversity = 0.5

    # Decode top opening lines via BFS.
    top_openings = _build_opening_lines(cache_index, color, top_n=10)

    return {
        "total_positions":     len(cache_index),
        "total_moves_indexed": total_moves_indexed,
        "avg_win_rate":        round(avg_win_rate, 3),
        "draw_rate":           round(draw_rate, 3),
        "decisive_rate":       round(decisive_rate, 3),
        "solidness_score":     round(solidness, 3),
        "opening_diversity":   round(diversity, 3),
        "top_openings":        top_openings,
    }


def _empty_profile() -> dict[str, Any]:
    return {
        "total_positions":     0,
        "total_moves_indexed": 0,
        "avg_win_rate":        0.0,
        "draw_rate":           0.0,
        "decisive_rate":       0.0,
        "solidness_score":     0.0,
        "opening_diversity":   0.0,
        "top_openings":        [],
    }


# ---------------------------------------------------------------------------
# Battlegrounds
# ---------------------------------------------------------------------------


def _compute_battlegrounds(
    player_index: dict[str, dict[str, Any]],
    opponent_index: dict[str, dict[str, Any]],
    player_color: str,
    min_games: int = 5,
    max_results: int = 20,
) -> list[dict[str, Any]]:
    """Positions where player has data, and after their top move the opponent also has data."""
    player_chess = chess.WHITE if player_color == "white" else chess.BLACK
    results: list[dict[str, Any]] = []

    for fen, player_payload in player_index.items():
        try:
            board = chess.Board(fen)
        except ValueError:
            continue
        if board.turn != player_chess:
            continue

        p_w = player_payload.get("white", 0)
        p_d = player_payload.get("draws", 0)
        p_b = player_payload.get("black", 0)
        p_total = p_w + p_d + p_b
        if p_total < min_games:
            continue

        moves = player_payload.get("moves", [])
        if not moves:
            continue

        top_m   = max(moves, key=lambda m: m.get("white", 0) + m.get("draws", 0) + m.get("black", 0))
        top_uci = top_m.get("uci", "")
        if not top_uci:
            continue

        try:
            move = chess.Move.from_uci(top_uci)
            if move not in board.legal_moves:
                continue
            board_after = board.copy()
            board_after.push(move)
        except ValueError:
            continue

        opp_fen     = board_after.fen()
        opp_payload = opponent_index.get(opp_fen)
        if opp_payload is None:
            continue

        o_w = opp_payload.get("white", 0)
        o_d = opp_payload.get("draws", 0)
        o_b = opp_payload.get("black", 0)
        o_total = o_w + o_d + o_b
        if o_total < min_games:
            continue

        p_win_rate = (p_w if player_color == "white" else p_b) / p_total
        o_win_rate = (o_b if player_color == "white" else o_w) / o_total

        adv_delta = p_win_rate - (1.0 - o_win_rate)
        advantage = "player" if adv_delta > 0.08 else "opponent" if adv_delta < -0.08 else "equal"

        player_top_san = ""
        try:
            player_top_san = chess.Board(fen).san(chess.Move.from_uci(top_uci))
        except Exception:
            pass

        opp_moves   = opp_payload.get("moves", [])
        opp_top_san = ""
        if opp_moves:
            opp_top_m = max(opp_moves, key=lambda m: m.get("white", 0) + m.get("draws", 0) + m.get("black", 0))
            opp_uci   = opp_top_m.get("uci", "")
            if opp_uci:
                try:
                    opp_top_san = board_after.copy().san(chess.Move.from_uci(opp_uci))
                except Exception:
                    pass

        results.append({
            "fen":                       fen,
            "fen_after":                 opp_fen,   # FEN after player's top move
            "player_games":              p_total,
            "player_win_rate":           round(p_win_rate, 3),
            "opponent_games":            o_total,
            "opponent_win_rate":         round(o_win_rate, 3),
            "advantage":                 advantage,
            "advantage_delta":           round(adv_delta, 3),
            "player_top_move_san":       player_top_san,
            "player_top_move_orig":      top_uci[:2] if len(top_uci) >= 4 else "",
            "player_top_move_dest":      top_uci[2:4] if len(top_uci) >= 4 else "",
            "opponent_top_response_san": opp_top_san,
        })

    results.sort(key=lambda x: abs(x["advantage_delta"]), reverse=True)
    return results[:max_results]


# ---------------------------------------------------------------------------
# Weakness / gap maps
# ---------------------------------------------------------------------------


def _habit_to_dict(h: HabitInaccuracy) -> dict[str, Any]:
    d    = dataclasses.asdict(h)
    uci  = h.player_move_uci
    d["player_move_orig"] = uci[:2] if len(uci) >= 4 else ""
    d["player_move_dest"] = uci[2:4] if len(uci) >= 4 else ""
    # FEN *after* the player's habitual move — used for correct board rendering
    # (chessground's lastMove expects the piece to already be at the dest square).
    try:
        b = chess.Board(h.fen)
        b.push(chess.Move.from_uci(uci))
        d["fen_after"] = b.fen()
    except Exception:
        d["fen_after"] = h.fen
    buci = h.best_move_uci
    d["best_move_orig"] = buci[:2] if len(buci) >= 4 else ""
    d["best_move_dest"] = buci[2:4] if len(buci) >= 4 else ""
    return d


def _reachable_weaknesses(
    opponent_habits: list[HabitInaccuracy],
    player_index: dict[str, dict[str, Any]],
    rank_limit: int = 20,
) -> list[dict[str, Any]]:
    """Opponent habit inaccuracies reachable from the player's repertoire.

    player_index has WHITE-to-move FENs; opponent habit FENs are BLACK-to-move.
    We build the set of positions reachable after the player plays any listed
    move, then check which opponent habits land in that set.
    """
    top_positions = sorted(
        player_index.items(),
        key=lambda kv: kv[1].get("white", 0) + kv[1].get("draws", 0) + kv[1].get("black", 0),
        reverse=True,
    )[:2000]

    reachable_fens: set[str] = set()
    for fen, payload in top_positions:
        try:
            board = chess.Board(fen)
        except ValueError:
            continue
        for move_data in payload.get("moves", []):
            uci = move_data.get("uci", "")
            if not uci:
                continue
            try:
                b = board.copy()
                b.push(chess.Move.from_uci(uci))
                reachable_fens.add(b.fen())
            except Exception:
                continue

    results = []
    for rank, habit in enumerate(opponent_habits, 1):
        if habit.fen not in reachable_fens:
            continue
        d = _habit_to_dict(habit)
        d["rank"] = rank
        d["reachable_from_player"] = True
        results.append(d)
        if len(results) >= rank_limit:
            break
    return results


def _prep_gaps(
    player_habits: list[HabitInaccuracy],
    opponent_index: dict[str, dict[str, Any]],
    rank_limit: int = 20,
) -> list[dict[str, Any]]:
    """Player habit inaccuracies where the opponent has data in the resulting position."""
    results = []
    for rank, habit in enumerate(player_habits, 1):
        try:
            board = chess.Board(habit.fen)
            board.push(chess.Move.from_uci(habit.player_move_uci))
            resulting_fen = board.fen()
        except Exception:
            continue
        opp_payload = opponent_index.get(resulting_fen)
        if opp_payload is None:
            continue
        o_total = (opp_payload.get("white", 0)
                   + opp_payload.get("draws", 0)
                   + opp_payload.get("black", 0))
        d = _habit_to_dict(habit)
        d["rank"] = rank
        d["opponent_games_here"] = o_total
        results.append(d)
        if len(results) >= rank_limit:
            break
    return results


# ---------------------------------------------------------------------------
# Key positions
# ---------------------------------------------------------------------------


def _key_positions(
    battlegrounds: list[dict],
    opponent_weaknesses: list[dict],
    prep_gaps: list[dict],
) -> list[dict[str, Any]]:
    """Pick the 5 most important positions across all categories."""
    picks: list[dict[str, Any]] = []

    for bg in battlegrounds[:2]:
        picks.append({
            "fen":       bg["fen"],
            "fen_after": bg.get("fen_after", ""),
            "label":     f"Battleground: player {bg['player_win_rate']:.0%} vs opp {bg['opponent_win_rate']:.0%}",
            "type":      "battleground",
            "move_san":  bg.get("player_top_move_san", ""),
            "move_orig": bg.get("player_top_move_orig", ""),
            "move_dest": bg.get("player_top_move_dest", ""),
        })

    for w in opponent_weaknesses[:2]:
        picks.append({
            "fen":       w["fen"],
            "fen_after": w.get("fen_after", ""),
            "label":     f"Opponent weakness: {w['player_move_san']} (gap {w['eval_gap_cp']:+.0f}cp)",
            "type":      "weakness",
            "move_san":  w.get("player_move_san", ""),
            "move_orig": w.get("player_move_orig", ""),
            "move_dest": w.get("player_move_dest", ""),
        })

    for g in prep_gaps[:1]:
        picks.append({
            "fen":       g["fen"],
            "fen_after": g.get("fen_after", ""),
            "label":     f"Prep gap: your {g['player_move_san']} (gap {g['eval_gap_cp']:+.0f}cp)",
            "type":      "gap",
            "move_san":  g.get("player_move_san", ""),
            "move_orig": g.get("player_move_orig", ""),
            "move_dest": g.get("player_move_dest", ""),
        })

    return picks[:5]


# ---------------------------------------------------------------------------
# Claude API
# ---------------------------------------------------------------------------


def _call_claude(
    api_key: str,
    player: str, player_color: str, player_platform: str,
    opponent: str, opponent_color: str, opponent_platform: str,
    player_style: dict, opponent_style: dict,
    player_phase: dict, opponent_phase: dict,
    battlegrounds: list[dict],
    opponent_weaknesses: list[dict],
    prep_gaps: list[dict],
) -> str:
    try:
        import anthropic
    except ImportError:
        raise RuntimeError(
            "anthropic package not installed. Run: pip install anthropic"
        )

    prompt = _build_prompt(
        player=player, player_color=player_color, player_platform=player_platform,
        opponent=opponent, opponent_color=opponent_color, opponent_platform=opponent_platform,
        player_style=player_style, opponent_style=opponent_style,
        player_phase=player_phase, opponent_phase=opponent_phase,
        battlegrounds=battlegrounds,
        opponent_weaknesses=opponent_weaknesses,
        prep_gaps=prep_gaps,
    )

    client  = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


def _build_prompt(
    player: str, player_color: str, player_platform: str,
    opponent: str, opponent_color: str, opponent_platform: str,
    player_style: dict, opponent_style: dict,
    player_phase: dict, opponent_phase: dict,
    battlegrounds: list[dict],
    opponent_weaknesses: list[dict],
    prep_gaps: list[dict],
) -> str:
    def pct(v: float) -> str:
        return f"{v * 100:.0f}%"

    lines = [
        f"You are a grandmaster chess coach. Write a strategic preparation brief for "
        f"**{player}** (playing {player_color} on {player_platform}) "
        f"preparing to face **{opponent}** (playing {opponent_color} on {opponent_platform}).",
        "",
        "---",
        "",
        f"## {player} — Opening Profile ({player_color})",
        f"- {player_style['total_positions']} positions cached",
        f"- Average win rate: {pct(player_style['avg_win_rate'])}",
        f"- Draw rate: {pct(player_style['draw_rate'])}  |  Decisive rate: {pct(player_style['decisive_rate'])}",
        f"- Solidity: {pct(player_style['solidness_score'])}  |  Opening diversity: {pct(player_style['opening_diversity'])}",
    ]

    plr_openings = player_style.get("top_openings", [])
    if plr_openings:
        lines.append("- Top opening lines:")
        for o in plr_openings[:6]:
            lines.append(
                f"    • {o['move_sequence']}  "
                f"({o['games']}g, {pct(o['win_rate'])} WR"
                + (f", next: {o['top_move_san']}" if o.get("top_move_san") else "")
                + ")"
            )

    if player_phase.get("total_games", 0) > 0:
        by_speed = player_phase.get("avg_length_by_speed", {})
        len_str = ", ".join(f"{s}: {v}m" for s, v in by_speed.items()) if by_speed else "n/a"
        lines += [
            f"- Game phases ({player_phase['total_games']} games analysed):",
            f"    • Avg game length by time control: {len_str}",
            f"    • Endgame reach rate: {pct(player_phase['endgame_reach_rate'])}",
            f"    • Endgame conversion (of decisive endgames, player wins): {pct(player_phase['endgame_conversion_rate'])}",
            f"    • Draw rate in middlegame endings: {pct(player_phase['draw_rate_middlegame'])}",
            f"    • Draw rate in endgame endings: {pct(player_phase['draw_rate_endgame'])}",
        ]

    lines += [
        "",
        f"## {opponent} — Opening Profile ({opponent_color})",
        f"- {opponent_style['total_positions']} positions cached",
        f"- Average win rate: {pct(opponent_style['avg_win_rate'])}",
        f"- Draw rate: {pct(opponent_style['draw_rate'])}  |  Decisive rate: {pct(opponent_style['decisive_rate'])}",
        f"- Solidity: {pct(opponent_style['solidness_score'])}  |  Opening diversity: {pct(opponent_style['opening_diversity'])}",
    ]

    opp_openings = opponent_style.get("top_openings", [])
    if opp_openings:
        lines.append("- Top opening lines:")
        for o in opp_openings[:6]:
            lines.append(
                f"    • {o['move_sequence']}  "
                f"({o['games']}g, {pct(o['win_rate'])} WR"
                + (f", next: {o['top_move_san']}" if o.get("top_move_san") else "")
                + ")"
            )

    if opponent_phase.get("total_games", 0) > 0:
        by_speed = opponent_phase.get("avg_length_by_speed", {})
        len_str = ", ".join(f"{s}: {v}m" for s, v in by_speed.items()) if by_speed else "n/a"
        lines += [
            f"- Game phases ({opponent_phase['total_games']} games analysed):",
            f"    • Avg game length by time control: {len_str}",
            f"    • Endgame reach rate: {pct(opponent_phase['endgame_reach_rate'])}",
            f"    • Endgame conversion: {pct(opponent_phase['endgame_conversion_rate'])}",
            f"    • Draw rate in middlegame endings: {pct(opponent_phase['draw_rate_middlegame'])}",
            f"    • Draw rate in endgame endings: {pct(opponent_phase['draw_rate_endgame'])}",
        ]

    lines += ["", "## Opening Battlegrounds (positions where both players have data)"]
    for bg in battlegrounds[:5]:
        lines.append(
            f"  • {player} {bg['player_games']}g @ {pct(bg['player_win_rate'])} WR, "
            f"{opponent} {bg['opponent_games']}g @ {pct(bg['opponent_win_rate'])} WR — "
            f"advantage: **{bg['advantage']}**  "
            f"({player} plays {bg['player_top_move_san']}, {opponent} responds {bg['opponent_top_response_san']})"
        )

    lines += ["", "## Opponent Weaknesses Reachable From Player's Repertoire (top 5)"]
    for w in opponent_weaknesses[:5]:
        lines.append(
            f"  • {opponent} plays {w['player_move_san']} in {w['total_games']} games "
            f"(best: {w['best_move_san']}, gap: {w['eval_gap_cp']:+.0f}cp, score: {w['score']:.1f})"
        )

    lines += ["", "## Player Prep Gaps — positions player plays poorly, opponent knows well (top 5)"]
    for g in prep_gaps[:5]:
        lines.append(
            f"  • {player} plays {g['player_move_san']} in {g['total_games']} games "
            f"(best: {g['best_move_san']}, gap: {g['eval_gap_cp']:+.0f}cp, "
            f"{opponent} has {g['opponent_games_here']} games here)"
        )

    lines += [
        "",
        "---",
        "",
        "Write your strategic brief in **markdown** using exactly these four headers:",
        "",
        "## Opening Approach",
        "## Middlegame Tendencies",
        "## Endgame & Conversion",
        "## Preparation Recommendations",
        "",
        "Under each header, use 2–4 bullet points. Be specific: name moves, cite win rates, "
        "reference the data above. Do not add a preamble or conclusion — just the four sections.",
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------


def _banner(player, player_color, player_platform, opponent, opponent_color,
            opponent_platform, verbose):
    if not verbose:
        return
    print(
        f"[strategise] {player} ({player_color}, {player_platform}) "
        f"vs {opponent} ({opponent_color}, {opponent_platform})",
        flush=True,
    )


def _log(verbose: bool, msg: str) -> None:
    if verbose:
        print(msg, flush=True)
