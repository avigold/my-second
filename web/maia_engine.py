"""Maia-2 chess engine interface for bot move generation.

Maia-2 (NeurIPS 2024) is a unified human-like chess model conditioned on
``elo_self`` (the bot's strength) and ``elo_oppo`` (the opponent's strength).
A single network covers the full amateur-to-GM range, making it far more
realistic than Stockfish with ``UCI_LimitStrength``.

The model is loaded lazily on first call and cached for the lifetime of the
process. Falls back to Stockfish if the ``maia2`` package is not installed or
the model fails to load.
"""
from __future__ import annotations

import threading

import chess

# ---------------------------------------------------------------------------
# Lazy model cache
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_maia2_model = None
_maia2_available: bool | None = None  # None = not yet tried

_MAIA2_ELO_MIN = 1100
_MAIA2_ELO_MAX = 2600


def _clamp_maia2(elo: int) -> int:
    return max(_MAIA2_ELO_MIN, min(_MAIA2_ELO_MAX, elo))


def _ensure_maia2() -> bool:
    """Import + load the Maia-2 model once. Returns True on success."""
    global _maia2_available, _maia2_model
    if _maia2_available is not None:
        return _maia2_available
    with _lock:
        if _maia2_available is not None:
            return _maia2_available
        try:
            from maia2 import model as _m
            _maia2_model = _m.from_pretrained(type="rapid", device="cpu")
            _maia2_available = True
        except Exception:
            _maia2_available = False
    return _maia2_available


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def get_move(fen: str, elo: int) -> str | None:
    """Return the bot's move for *fen* as a UCI string.

    Uses Maia-2 if available, otherwise falls back to Stockfish with
    ``UCI_LimitStrength``.

    Parameters
    ----------
    fen:
        FEN string of the position the bot must play from.
    elo:
        The bot's Elo rating.  Used as ``elo_self`` in Maia-2 (the user's
        strength is assumed to be 1500 since we don't know it).
    """
    board = chess.Board(fen)
    if board.is_game_over():
        return None

    if _ensure_maia2():
        try:
            return _maia2_move(board, _clamp_maia2(elo))
        except Exception:
            pass  # fall through to Stockfish

    return _stockfish_move(board, elo)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _maia2_move(board: chess.Board, elo: int) -> str | None:
    from maia2 import inference as _inf

    results = _inf.inference_batch(
        fens=[board.fen()],
        elo_self=elo,
        elo_oppo=1500,  # user strength unknown; 1500 is a reasonable default
    )
    if not results:
        return None

    policy, _value = results[0]

    # policy is expected to be a dict {uci_str: probability}
    if isinstance(policy, dict) and policy:
        return max(policy, key=lambda k: policy[k])

    # Some versions return a list of (move, prob) pairs
    if isinstance(policy, (list, tuple)) and policy:
        best = max(policy, key=lambda x: x[1])
        return str(best[0])

    return None


def _stockfish_move(board: chess.Board, elo: int) -> str | None:
    import chess.engine as _ce
    from mysecond.engine import find_stockfish

    _SF_MIN, _SF_MAX = 1320, 3190
    clamped = max(_SF_MIN, min(_SF_MAX, elo))

    sf_path = str(find_stockfish())
    engine = _ce.SimpleEngine.popen_uci(sf_path)
    try:
        engine.configure({
            "UCI_LimitStrength": True,
            "UCI_Elo": clamped,
            "Threads": 1,
        })
        result = engine.play(board, _ce.Limit(depth=10))
        return result.move.uci() if result.move else None
    finally:
        engine.quit()
