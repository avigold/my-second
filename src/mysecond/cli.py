"""Command-line entry-point for mysecond."""

from __future__ import annotations

import sys
from pathlib import Path

import chess
import click

from .engine import find_stockfish
from .export import export_pgn
from .score import score_novelty
from .search import SearchConfig, find_novelties


@click.command()
@click.option("--fen", required=True, help="Starting position in FEN notation.")
@click.option(
    "--side",
    required=True,
    type=click.Choice(["white", "black"]),
    help="Side to research novelties for.",
)
@click.option(
    "--plies",
    default=25,
    show_default=True,
    help="Maximum theory depth to walk (half-moves).",
)
@click.option(
    "--beam",
    default=10,
    show_default=True,
    help="Engine candidate moves to consider at each of our turns.",
)
@click.option(
    "--min-book-games",
    "min_book_games",
    default=5,
    show_default=True,
    help="Minimum master games for a position to be considered 'in book'.",
)
@click.option(
    "--novelty-threshold",
    "novelty_threshold",
    default=2,
    show_default=True,
    help="A move with ≤ this many master games qualifies as a novelty.",
)
@click.option(
    "--opponent-responses",
    "opponent_responses",
    default=3,
    show_default=True,
    help="Number of most-popular opponent database moves to follow.",
)
@click.option(
    "--depths",
    default="20,24,28",
    show_default=True,
    help="Comma-separated depths for deep evaluation of novelties.",
)
@click.option(
    "--time-ms",
    "time_ms",
    default=200,
    show_default=True,
    help="Per-position time cap (ms) during the theory walk.",
)
@click.option(
    "--min-eval",
    "min_eval",
    default=0,
    show_default=True,
    help=(
        "Minimum centipawn evaluation (perspective-corrected) to keep a novelty. "
        "0 = at least equal; 25 = slightly better; negative values allow dubious lines."
    ),
)
@click.option(
    "--continuations",
    "continuation_plies",
    default=6,
    show_default=True,
    help="Post-novelty engine PV moves to include in the PGN.",
)
@click.option(
    "--workers",
    default=4,
    show_default=True,
    help="Parallel workers for deep evaluation.",
)
@click.option(
    "--max-positions",
    "max_positions",
    default=800,
    show_default=True,
    help="Maximum theory-tree positions to visit (guards against runaway search).",
)
@click.option(
    "--max-candidates",
    "max_candidates",
    default=200,
    show_default=True,
    help="Maximum novelty candidates to send for deep evaluation (top N by quick eval).",
)
@click.option(
    "--out",
    "out_path",
    default="ideas.pgn",
    show_default=True,
    help="Output PGN file.",
)
def main(
    fen: str,
    side: str,
    plies: int,
    beam: int,
    min_book_games: int,
    novelty_threshold: int,
    opponent_responses: int,
    depths: str,
    time_ms: int,
    min_eval: int,
    continuation_plies: int,
    workers: int,
    max_positions: int,
    max_candidates: int,
    out_path: str,
) -> None:
    """mysecond – virtual second for chess novelty discovery.

    Walks opening theory from the given FEN, finds moves not (or barely)
    present in the Lichess masters database, evaluates them with Stockfish,
    and exports an annotated PGN importable into ChessBase.

    Only novelties whose engine evaluation meets --min-eval are kept,
    ensuring all suggestions are solid and advantageous for the researched side.
    """
    depth_list = [int(d.strip()) for d in depths.split(",")]
    chess_side = chess.WHITE if side == "white" else chess.BLACK
    output = Path(out_path)

    click.echo("[mysecond] Configuration")
    click.echo(f"  FEN:                {fen}")
    click.echo(f"  Side:               {side}")
    click.echo(f"  Max theory depth:   {plies} plies")
    click.echo(f"  Engine candidates:  {beam} per position")
    click.echo(f"  Min book games:     {min_book_games}")
    click.echo(f"  Novelty threshold:  ≤ {novelty_threshold} master games")
    click.echo(f"  Opponent responses: {opponent_responses}")
    click.echo(f"  Eval depths:        {depth_list}")
    click.echo(f"  Min eval:           {min_eval:+d} cp")
    click.echo(f"  Workers:            {workers}")
    click.echo(f"  Max candidates:     {max_candidates}")
    click.echo(f"  Output:             {output}")

    try:
        engine_path = find_stockfish()
        click.echo(f"  Engine:             {engine_path}")
    except FileNotFoundError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    config = SearchConfig(
        fen=fen,
        side=chess_side,
        max_book_plies=plies,
        min_book_games=min_book_games,
        novelty_threshold=novelty_threshold,
        engine_candidates=beam,
        opponent_responses=opponent_responses,
        depths=depth_list,
        time_ms=time_ms,
        engine_path=engine_path,
        min_eval_cp=min_eval,
        continuation_plies=continuation_plies,
        max_workers=workers,
        max_positions=max_positions,
        max_candidates=max_candidates,
    )

    click.echo("\n[mysecond] Walking theory …")
    novelties = find_novelties(config)

    if not novelties:
        click.echo(
            "[mysecond] No novelties found.\n"
            "  Try: --min-book-games lower, --novelty-threshold higher,\n"
            "       --plies deeper, --beam wider, or --min-eval lower."
        )
        sys.exit(0)

    click.echo(f"[mysecond] {len(novelties)} novelties passed the eval filter.")

    click.echo("[mysecond] Scoring and ranking …")
    scored = sorted(
        [score_novelty(n, chess_side) for n in novelties],
        key=lambda s: -s.score,
    )

    click.echo(f"[mysecond] Exporting {len(scored)} novelties → {output}")
    export_pgn(scored, fen, output)

    click.echo("\n[mysecond] Top 5 novelties:")
    for i, sn in enumerate(scored[:5], 1):
        nov = sn.novelty
        click.echo(
            f"  {i}. ply={nov.novelty_ply + 1}  "
            f"eval={sn.eval_cp:+.0f}cp  "
            f"stability={sn.stability:.1f}cp  "
            f"pre={nov.pre_novelty_games}  "
            f"post={nov.post_novelty_games}  "
            f"score={sn.score:.1f}"
        )

    click.echo("\n[mysecond] Done.")
