"""Generate a strategic preparation brief for a player vs an opponent.

How it works
------------
1. Auto-fetch both players' games if their caches are empty.
2. Run habit analysis on both players with Stockfish.
3. Compute style profiles from each player's cache (aggression, solidness,
   opening diversity, win rates).
4. Find opening battlegrounds: positions where both players have cache data
   one move apart, so a direct comparison is possible.
5. Map opponent weaknesses reachable from the player's own repertoire.
6. Map the player's prep gaps where the opponent is strong.
7. Optionally call the Claude API to synthesise a strategic brief.
8. Write a structured JSON report to ``out_path``.
"""

from __future__ import annotations

import dataclasses
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import chess

from .cache import Cache
from .engine import Engine
from .fetcher import _backend_key, fetch_player_games, fetch_player_games_chesscom
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
        _log(verbose, f"[strategise] No cache for {player} — fetching …")
        _fetch(player, player_color, player_platform, player_speeds, cache, verbose)
        player_index = _load_index(cache, player_backend)

    if not opponent_index:
        _log(verbose, f"[strategise] No cache for {opponent} — fetching …")
        _fetch(opponent, opponent_color, opponent_platform, opponent_speeds, cache, verbose)
        opponent_index = _load_index(cache, opponent_backend)

    _log(verbose,
         f"[strategise] {len(player_index)} positions for {player}, "
         f"{len(opponent_index)} for {opponent}.")

    # ── 2. Habit analysis (both players in parallel) ─────────────────────────
    _log(verbose, f"[strategise] Analysing habits for {player} and {opponent} in parallel …")

    def _habits(username, color, speeds, platform):
        return analyze_habits(
            username=username, color=color, cache=cache,
            engine_path=engine_path, speeds=speeds, platform=platform,
            min_games=min_games, max_positions=max_positions,
            min_eval_gap=min_eval_gap, depth=depth, verbose=verbose,
            engine_threads=_PARALLEL_THREADS,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        opp_future = pool.submit(_habits, opponent, opponent_color, opponent_speeds, opponent_platform)
        plr_future = pool.submit(_habits, player,   player_color,   player_speeds,   player_platform)
        opponent_habits = opp_future.result()
        player_habits   = plr_future.result()

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
    position_branching: list[float] = []

    for fen, payload in cache_index.items():
        w = payload.get("white", 0)
        d = payload.get("draws", 0)
        b = payload.get("black", 0)
        total = w + d + b
        if total == 0:
            continue
        win_rate = (w if color == "white" else b) / total
        all_win_rates.append(win_rate)
        moves = payload.get("moves", [])
        total_moves_indexed += len(moves)
        position_branching.append(len(moves))

    n = len(all_win_rates)
    if n == 0:
        return _empty_profile()

    avg_branching = sum(position_branching) / n
    avg_win_rate  = sum(all_win_rates) / n
    solidness     = sum(1 for r in all_win_rates if r > 0.5) / n

    # Aggression proxy: fraction of positions with above-average branching
    aggression = sum(1 for b in position_branching if b > avg_branching) / n

    # Opening diversity: 1 - (top_move_fraction at root)
    root_payload = cache_index.get(chess.STARTING_FEN, {})
    root_w = root_payload.get("white", 0)
    root_d = root_payload.get("draws", 0)
    root_b = root_payload.get("black", 0)
    root_total = root_w + root_d + root_b
    root_moves = root_payload.get("moves", [])
    if root_total > 0 and root_moves:
        top_root = max(
            m.get("white", 0) + m.get("draws", 0) + m.get("black", 0)
            for m in root_moves
        )
        diversity = 1.0 - top_root / root_total
    else:
        diversity = 0.5

    # Top openings by game count
    sorted_fens = sorted(
        cache_index.items(),
        key=lambda x: x[1].get("white", 0) + x[1].get("draws", 0) + x[1].get("black", 0),
        reverse=True,
    )
    top_openings: list[dict[str, Any]] = []
    for fen, payload in sorted_fens[:10]:
        w = payload.get("white", 0)
        d = payload.get("draws", 0)
        b = payload.get("black", 0)
        total = w + d + b
        if total == 0:
            continue
        win_rate = (w if color == "white" else b) / total
        moves = payload.get("moves", [])
        top_move_san = ""
        if moves:
            top_m = max(
                moves,
                key=lambda m: m.get("white", 0) + m.get("draws", 0) + m.get("black", 0),
            )
            uci = top_m.get("uci", "")
            if uci:
                try:
                    board = chess.Board(fen)
                    if board.turn == player_chess_color:
                        top_move_san = board.san(chess.Move.from_uci(uci))
                except Exception:
                    top_move_san = uci
        top_openings.append({
            "fen":          fen,
            "games":        total,
            "win_rate":     round(win_rate, 3),
            "top_move_san": top_move_san,
        })

    return {
        "total_positions":     len(cache_index),
        "total_moves_indexed": total_moves_indexed,
        "avg_branching":       round(avg_branching, 2),
        "avg_win_rate":        round(avg_win_rate, 3),
        "aggression_score":    round(aggression, 3),
        "solidness_score":     round(solidness, 3),
        "opening_diversity":   round(diversity, 3),
        "top_openings":        top_openings,
    }


def _empty_profile() -> dict[str, Any]:
    return {
        "total_positions": 0, "total_moves_indexed": 0,
        "avg_branching": 0.0, "avg_win_rate": 0.0,
        "aggression_score": 0.0, "solidness_score": 0.0,
        "opening_diversity": 0.0, "top_openings": [],
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

        top_m = max(
            moves,
            key=lambda m: m.get("white", 0) + m.get("draws", 0) + m.get("black", 0),
        )
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

        opp_fen = board_after.fen()
        opp_payload = opponent_index.get(opp_fen)
        if opp_payload is None:
            continue

        o_w = opp_payload.get("white", 0)
        o_d = opp_payload.get("draws", 0)
        o_b = opp_payload.get("black", 0)
        o_total = o_w + o_d + o_b
        if o_total < min_games:
            continue

        # Win rates from each player's perspective
        p_win_rate = (p_w if player_color == "white" else p_b) / p_total
        opp_color  = "black" if player_color == "white" else "white"
        o_win_rate = (o_b if player_color == "white" else o_w) / o_total  # opp's wins

        adv_delta = p_win_rate - (1.0 - o_win_rate)
        advantage = "player" if adv_delta > 0.08 else "opponent" if adv_delta < -0.08 else "equal"

        # Player's top move SAN
        player_top_san = ""
        try:
            player_top_san = chess.Board(fen).san(chess.Move.from_uci(top_uci))
        except Exception:
            pass

        # Opponent's top response SAN
        opp_moves = opp_payload.get("moves", [])
        opp_top_san = ""
        if opp_moves:
            opp_top_m = max(
                opp_moves,
                key=lambda m: m.get("white", 0) + m.get("draws", 0) + m.get("black", 0),
            )
            opp_uci = opp_top_m.get("uci", "")
            if opp_uci:
                try:
                    opp_board = board_after.copy()
                    opp_top_san = opp_board.san(chess.Move.from_uci(opp_uci))
                except Exception:
                    pass

        results.append({
            "fen":                     fen,
            "player_games":            p_total,
            "player_win_rate":         round(p_win_rate, 3),
            "opponent_games":          o_total,
            "opponent_win_rate":       round(o_win_rate, 3),
            "advantage":               advantage,
            "advantage_delta":         round(adv_delta, 3),
            "player_top_move_san":     player_top_san,
            "opponent_top_response_san": opp_top_san,
        })

    results.sort(key=lambda x: abs(x["advantage_delta"]), reverse=True)
    return results[:max_results]


# ---------------------------------------------------------------------------
# Weakness / gap maps
# ---------------------------------------------------------------------------


def _habit_to_dict(h: HabitInaccuracy) -> dict[str, Any]:
    d = dataclasses.asdict(h)
    uci = h.player_move_uci
    d["player_move_orig"] = uci[:2] if len(uci) >= 4 else ""
    d["player_move_dest"] = uci[2:4] if len(uci) >= 4 else ""
    buci = h.best_move_uci
    d["best_move_orig"] = buci[:2] if len(buci) >= 4 else ""
    d["best_move_dest"] = buci[2:4] if len(buci) >= 4 else ""
    return d


def _reachable_weaknesses(
    opponent_habits: list[HabitInaccuracy],
    player_index: dict[str, dict[str, Any]],
    rank_limit: int = 20,
) -> list[dict[str, Any]]:
    """Opponent habit inaccuracies where the FEN is in the player's cache."""
    results = []
    for rank, habit in enumerate(opponent_habits, 1):
        reachable = habit.fen in player_index
        d = _habit_to_dict(habit)
        d["rank"] = rank
        d["reachable_from_player"] = reachable
        if reachable:
            results.append(d)
        if len(results) >= rank_limit:
            break
    return results


def _prep_gaps(
    player_habits: list[HabitInaccuracy],
    opponent_index: dict[str, dict[str, Any]],
    rank_limit: int = 20,
) -> list[dict[str, Any]]:
    """Player habit inaccuracies where the opponent also has data at that FEN."""
    results = []
    for rank, habit in enumerate(player_habits, 1):
        opp_payload = opponent_index.get(habit.fen)
        if opp_payload is None:
            continue
        o_w = opp_payload.get("white", 0)
        o_d = opp_payload.get("draws", 0)
        o_b = opp_payload.get("black", 0)
        o_total = o_w + o_d + o_b
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
            "fen":   bg["fen"],
            "label": f"Battleground: player {bg['player_win_rate']:.0%} vs opp {bg['opponent_win_rate']:.0%}",
            "type":  "battleground",
            "move_san": bg.get("player_top_move_san", ""),
            "move_orig": "",
            "move_dest": "",
        })

    for w in opponent_weaknesses[:2]:
        picks.append({
            "fen":      w["fen"],
            "label":    f"Opponent weakness: {w['player_move_san']} (gap {w['eval_gap_cp']:+.0f}cp)",
            "type":     "weakness",
            "move_san":  w.get("player_move_san", ""),
            "move_orig": w.get("player_move_orig", ""),
            "move_dest": w.get("player_move_dest", ""),
        })

    for g in prep_gaps[:1]:
        picks.append({
            "fen":      g["fen"],
            "label":    f"Prep gap: your {g['player_move_san']} (gap {g['eval_gap_cp']:+.0f}cp)",
            "type":     "gap",
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
        battlegrounds=battlegrounds,
        opponent_weaknesses=opponent_weaknesses,
        prep_gaps=prep_gaps,
    )

    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


def _build_prompt(
    player: str, player_color: str, player_platform: str,
    opponent: str, opponent_color: str, opponent_platform: str,
    player_style: dict, opponent_style: dict,
    battlegrounds: list[dict],
    opponent_weaknesses: list[dict],
    prep_gaps: list[dict],
) -> str:
    lines = [
        f"You are a grandmaster chess coach. Your job is to write a concise strategic "
        f"preparation brief for {player} (playing {player_color} on {player_platform}) "
        f"preparing to face {opponent} (playing {opponent_color} on {opponent_platform}).",
        "",
        "## Player Style",
        f"- {player_style['total_positions']} opening positions indexed",
        f"- Average win rate: {player_style['avg_win_rate']:.1%}",
        f"- Aggression: {player_style['aggression_score']:.1%}  "
        f"Solidness: {player_style['solidness_score']:.1%}  "
        f"Diversity: {player_style['opening_diversity']:.1%}",
        "",
        "## Opponent Style",
        f"- {opponent_style['total_positions']} opening positions indexed",
        f"- Average win rate: {opponent_style['avg_win_rate']:.1%}",
        f"- Aggression: {opponent_style['aggression_score']:.1%}  "
        f"Solidness: {opponent_style['solidness_score']:.1%}  "
        f"Diversity: {opponent_style['opening_diversity']:.1%}",
        "",
        "## Opening Battlegrounds (top 5 — positions where both players have data)",
    ]
    for bg in battlegrounds[:5]:
        lines.append(
            f"  • Player {bg['player_games']}g @ {bg['player_win_rate']:.0%} WR, "
            f"Opp {bg['opponent_games']}g @ {bg['opponent_win_rate']:.0%} WR — "
            f"advantage: {bg['advantage']}  "
            f"(player plays {bg['player_top_move_san']}, opp responds {bg['opponent_top_response_san']})"
        )

    lines += ["", "## Opponent Weaknesses Reachable From Player's Repertoire (top 5)"]
    for w in opponent_weaknesses[:5]:
        lines.append(
            f"  • Opponent plays {w['player_move_san']} in {w['total_games']} games "
            f"(best: {w['best_move_san']}, gap: {w['eval_gap_cp']:+.0f}cp, score: {w['score']:.1f})"
        )

    lines += ["", "## Player Prep Gaps — positions player plays poorly, opponent knows well (top 5)"]
    for g in prep_gaps[:5]:
        lines.append(
            f"  • Player plays {g['player_move_san']} in {g['total_games']} games "
            f"(best: {g['best_move_san']}, gap: {g['eval_gap_cp']:+.0f}cp, "
            f"opponent has {g['opponent_games_here']} games here)"
        )

    lines += [
        "",
        "Write a strategic preparation brief in 3–4 focused paragraphs covering:",
        "1. Overall style matchup — who is more aggressive, solid, or diverse, and what that means for the game.",
        "2. How to exploit the opponent's specific weaknesses above (be concrete — name the moves).",
        "3. Which prep gaps the player must address or avoid before this encounter.",
        "4. One clear, actionable opening recommendation.",
        "",
        "Be direct, concrete, and use chess terminology. Reference specific moves and win rates from the data.",
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
