"""Shared data-model types used across all modules."""

from __future__ import annotations

from dataclasses import dataclass, field

import chess


# ---------------------------------------------------------------------------
# Engine evaluation
# ---------------------------------------------------------------------------


@dataclass
class EngineEval:
    """Engine evaluation at a single depth."""

    depth: int
    cp_white: int | None   # centipawns from white's perspective; None when mate
    mate_white: int | None  # mate-in-N from white's perspective; None when cp

    def cp_pov(self, side: chess.Color) -> int:
        """Centipawns from *side*'s perspective (positive = good for side)."""
        if self.cp_white is None:
            mate = self.mate_white or 0
            white_val = 10_000 if mate > 0 else -10_000
            return white_val if side else -white_val
        return self.cp_white if side else -self.cp_white

    def display(self) -> str:
        """Human-readable evaluation string."""
        if self.mate_white is not None:
            sign = "+" if self.mate_white > 0 else "-"
            return f"M{sign}{abs(self.mate_white)}"
        cp = self.cp_white or 0
        return f"{cp / 100:+.2f}"


# ---------------------------------------------------------------------------
# Opening explorer
# ---------------------------------------------------------------------------


@dataclass
class MoveStats:
    """Per-move statistics returned by the opening explorer."""

    uci: str
    white: int
    draws: int
    black: int
    average_rating: int = 0

    @property
    def total(self) -> int:
        return self.white + self.draws + self.black


@dataclass
class ExplorerData:
    """Full opening-explorer response for a position.

    ``moves`` contains only moves that have been played in the reference
    database.  Any move *not* present in this list has **zero** master games.
    """

    white: int
    draws: int
    black: int
    moves: list[MoveStats] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.white + self.draws + self.black

    def games_for_move(self, uci: str) -> int:
        """Return the number of master games for *uci*, 0 if not in database."""
        for m in self.moves:
            if m.uci == uci:
                return m.total
        return 0

    def top_moves(self, n: int) -> list[MoveStats]:
        """Return the *n* most-played moves, sorted by game count descending."""
        return sorted(self.moves, key=lambda m: -m.total)[:n]


# ---------------------------------------------------------------------------
# Novelty candidates
# ---------------------------------------------------------------------------


@dataclass
class NoveltyLine:
    """A discovered novelty: path through theory + the new move + deep eval."""

    book_moves: list[str]         # UCI moves through theory (before the novelty)
    novelty_move: str             # UCI of the novelty move itself
    novelty_ply: int              # 0-indexed ply at which the novelty occurs
    evals: dict[int, EngineEval]  # depth → eval of the position *after* novelty
    pre_novelty_games: int        # master games in the position *before* the novelty
    post_novelty_games: int       # master games *after* the novelty (0 = true TN)
    continuations: list[str]      # engine's suggested PV after the novelty move


@dataclass
class ScoredNovelty:
    """A novelty candidate annotated with composite scoring data."""

    novelty: NoveltyLine
    eval_cp: float    # perspective-corrected mean cp across depths
    stability: float  # stddev of cp across depths (lower = more reliable)
    score: float      # final score used for ranking
