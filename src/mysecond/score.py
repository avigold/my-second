"""Scoring for novelty candidates.

A good novelty has three properties:

1. **Solid eval** – the position is good (or at least equal) for the researched
   side across *all* evaluated depths.  This is the primary ranking criterion.

2. **Stability** – the eval does not fluctuate wildly as depth increases.  An
   unstable eval (large stddev) suggests the engine is confused and the line
   may be refuted at deeper depth.

3. **Appropriate depth** – a novelty too early (ply < 4) is probably in the
   theoretical repertoire of any prepared opponent; one too late (ply > 30)
   may be unreachable in practice.  We apply a mild Gaussian bonus that peaks
   around ply 12–16.

Score formula:
    score = eval_cp  −  STABILITY_W × stability  +  DEPTH_W × depth_bonus

Where:
    eval_cp    — mean cp across depths (perspective-corrected; positive = good)
    stability  — stddev of cp across depths
    depth_bonus — bell-curve in [0, 1], centred at ply DEPTH_PEAK
"""

from __future__ import annotations

import math
import statistics

import chess

from .models import NoveltyLine, ScoredNovelty

_STABILITY_WEIGHT: float = 8.0   # penalty per cp of stddev
_DEPTH_WEIGHT: float = 30.0      # max bonus for optimal novelty depth
_DEPTH_PEAK: int = 14            # ideal novelty ply (half-moves, 0-indexed)
_DEPTH_SIGMA: float = 8.0        # width of the depth-bonus bell curve


def score_novelty(nov: NoveltyLine, side: chess.Color) -> ScoredNovelty:
    """Compute composite score for a novelty candidate."""
    cp_values = [
        ev.cp_pov(side)
        for ev in nov.evals.values()
        if ev.cp_white is not None
    ]

    if cp_values:
        eval_cp = statistics.mean(cp_values)
        stability = statistics.stdev(cp_values) if len(cp_values) > 1 else 0.0
    else:
        eval_cp = 10_000.0 if _has_mate_for(nov, side) else -10_000.0
        stability = 0.0

    depth_bonus = math.exp(
        -((nov.novelty_ply - _DEPTH_PEAK) ** 2) / (2 * _DEPTH_SIGMA ** 2)
    )

    score = (
        eval_cp
        - _STABILITY_WEIGHT * stability
        + _DEPTH_WEIGHT * depth_bonus
    )

    return ScoredNovelty(
        novelty=nov,
        eval_cp=eval_cp,
        stability=stability,
        score=score,
    )


def _has_mate_for(nov: NoveltyLine, side: chess.Color) -> bool:
    for ev in nov.evals.values():
        if ev.mate_white is not None:
            return bool(ev.mate_white > 0) == bool(side)
    return False
