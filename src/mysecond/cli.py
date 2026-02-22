"""Command-line entry-point for mysecond.

Usage
-----
  mysecond search   --fen <FEN> --side white ...   (find novelties)
  mysecond fetch-player-games --username <U> --color white ...  (warm cache)

Run ``mysecond <command> --help`` for full option listings.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import chess
import click

from .cache import Cache
from .engine import find_stockfish
from .export import export_pgn
from .fetcher import _DEFAULT_DB as _FETCH_DB
from .fetcher import fetch_player_games, last_fetch_ts
from .score import score_novelty
from .search import SearchConfig, find_novelties


@click.group()
def main() -> None:
    """mysecond – virtual second for chess novelty discovery.

    \b
    Commands:
      search              Walk opening theory and find novelties.
      fetch-player-games  Download a player's games to warm the local cache.
    """


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


@main.command("search")
@click.option(
    "--fen",
    default=chess.STARTING_FEN,
    show_default=False,
    help="Starting position in FEN notation. Defaults to the starting position.",
)
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
        "0 = at least equal; 25 = slightly better; negative allows dubious lines."
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
    help="Maximum novelty candidates to deep-evaluate (top N by quick eval).",
)
@click.option(
    "--out",
    "out_path",
    default="ideas.pgn",
    show_default=True,
    help="Output PGN file.",
)
# --- Player / opponent filtering ---
@click.option(
    "--player",
    "player_name",
    default=None,
    help=(
        "Lichess username of the player to prepare for. "
        "The walk recurses only on in-book moves they have played, "
        "keeping the search focused on their real repertoire. "
        "Run 'mysecond fetch-player-games' first to pre-warm the cache."
    ),
)
@click.option(
    "--opponent",
    "opponent_name",
    default=None,
    help=(
        "Lichess username of the opponent. "
        "The walk follows the opponent's actual moves at their turns instead of "
        "generic masters top moves."
    ),
)
@click.option(
    "--min-player-games",
    "min_player_games",
    default=3,
    show_default=True,
    help="Minimum player games for a move to count as 'in their repertoire'.",
)
@click.option(
    "--min-opponent-games",
    "min_opponent_games",
    default=3,
    show_default=True,
    help="Minimum opponent games for a move to count as 'their line'.",
)
@click.option(
    "--player-speeds",
    "player_speeds",
    default="blitz,rapid,classical",
    show_default=True,
    help="Lichess time controls to include for the player.",
)
@click.option(
    "--opponent-speeds",
    "opponent_speeds",
    default="blitz,rapid,classical",
    show_default=True,
    help="Lichess time controls to include for the opponent.",
)
@click.option(
    "--local-only/--no-local-only",
    "player_local_only",
    default=None,
    help=(
        "When set, player/opponent explorer never makes live HTTP requests — "
        "only local cache data is used (fast; requires prior fetch-player-games). "
        "Default: True when --player/--opponent are set, False otherwise."
    ),
)
def search_cmd(
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
    player_name: str | None,
    opponent_name: str | None,
    min_player_games: int,
    min_opponent_games: int,
    player_speeds: str,
    opponent_speeds: str,
    player_local_only: bool | None,
) -> None:
    """Walk opening theory and find novelties for ChessBase import.

    Walks theory from the given FEN, finds moves not (or barely) present in
    the Lichess masters database, evaluates them with Stockfish, and exports
    an annotated PGN importable into ChessBase.

    Use --player and --opponent to scope the search to lines a specific
    player and opponent actually reach. Run 'mysecond fetch-player-games'
    first to pre-warm the local cache (makes the walk much faster).

    Only novelties whose engine evaluation meets --min-eval are kept,
    ensuring all suggestions are solid and advantageous for the researched side.
    """
    depth_list = [int(d.strip()) for d in depths.split(",")]
    chess_side = chess.WHITE if side == "white" else chess.BLACK
    output = Path(out_path)

    click.echo("[mysecond] Configuration")
    click.echo(f"  FEN:                {fen}")
    click.echo(f"  Side:               {side}")
    if player_name:
        click.echo(f"  Player:             {player_name} (as {side})")
    if opponent_name:
        click.echo(f"  Opponent:           {opponent_name}")
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

    # Default to local-only when player/opponent filtering is active (fast after fetch).
    effective_local_only = (
        player_local_only if player_local_only is not None
        else bool(player_name or opponent_name)
    )

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
        player_name=player_name,
        opponent_name=opponent_name,
        min_player_games=min_player_games,
        min_opponent_games=min_opponent_games,
        player_speeds=player_speeds,
        opponent_speeds=opponent_speeds,
        player_local_only=effective_local_only,
    )

    click.echo("\n[mysecond] Walking theory …")
    if player_name or opponent_name:
        click.echo(
            "  (player/opponent filtering active – walk follows real repertoires)"
        )

    novelties = find_novelties(config)

    if not novelties:
        click.echo(
            "[mysecond] No novelties found.\n"
            "  Try: --min-book-games lower, --novelty-threshold higher,\n"
            "       --plies deeper, --beam wider, or --min-eval lower."
        )
        if player_name or opponent_name:
            click.echo(
                "  With player/opponent filtering: also try --min-player-games lower\n"
                "  or --min-opponent-games lower to widen the repertoire scope.\n"
                "  Ensure you have run 'mysecond fetch-player-games' first."
            )
        sys.exit(0)

    click.echo(f"[mysecond] {len(novelties)} novelties passed the eval filter.")

    click.echo("[mysecond] Scoring and ranking …")
    scored = sorted(
        [score_novelty(n, chess_side) for n in novelties],
        key=lambda s: -s.score,
    )

    click.echo(f"[mysecond] Exporting {len(scored)} novelties → {output}")
    export_pgn(
        scored,
        fen,
        output,
        player_name=player_name,
        opponent_name=opponent_name,
    )

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


# ---------------------------------------------------------------------------
# fetch-player-games
# ---------------------------------------------------------------------------


@main.command("fetch-player-games")
@click.option(
    "--username",
    required=True,
    help="Lichess username to fetch games for.",
)
@click.option(
    "--color",
    required=True,
    type=click.Choice(["white", "black"]),
    help="Fetch games where the player was this colour.",
)
@click.option(
    "--speeds",
    default="blitz,rapid,classical",
    show_default=True,
    help="Comma-separated Lichess time controls to include.",
)
@click.option(
    "--max-games",
    "max_games",
    default=10_000,
    show_default=True,
    help="Maximum number of games to download.",
)
@click.option(
    "--max-plies",
    "max_plies",
    default=30,
    show_default=True,
    help="Walk each game at most this many half-moves (opening depth).",
)
@click.option(
    "--since",
    "since_date",
    default=None,
    help=(
        "Only fetch games played since this date (YYYY-MM-DD). "
        "New counts are merged into the existing cache (incremental update). "
        "Omit to perform a full rebuild."
    ),
)
@click.option(
    "--db",
    "db_path",
    default=str(_FETCH_DB),
    show_default=True,
    help="Path to the SQLite cache database.",
)
def fetch_cmd(
    username: str,
    color: str,
    speeds: str,
    max_games: int,
    max_plies: int,
    since_date: str | None,
    db_path: str,
) -> None:
    """Download a player's games and index them for fast local lookups.

    \b
    Examples:
      # Full fetch (replaces any existing cache for this player/colour):
      mysecond fetch-player-games --username GothamChess --color white

      # Incremental update (merge new games into existing cache):
      mysecond fetch-player-games --username GothamChess --color white \\
          --since 2024-01-01

      # Full preparation workflow:
      mysecond fetch-player-games --username GothamChess   --color white
      mysecond fetch-player-games --username im_eric_rosen --color black
      mysecond search --player GothamChess --opponent im_eric_rosen --side white ...

    After fetching, 'mysecond search' reads repertoire data from the local
    cache instead of making HTTP requests to the Lichess /player endpoint.
    This makes the theory walk orders of magnitude faster.
    """
    since_ts: int | None = None
    if since_date is not None:
        try:
            dt = datetime.strptime(since_date, "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )
            since_ts = int(dt.timestamp() * 1000)
        except ValueError:
            raise click.BadParameter(
                f"Expected YYYY-MM-DD, got {since_date!r}", param_hint="--since"
            )

    db = Path(db_path)
    with Cache(db) as cache:
        # Show last fetch time if available.
        last_ts = last_fetch_ts(username, color, speeds, cache)
        if last_ts:
            last_dt = datetime.fromtimestamp(last_ts / 1000, tz=timezone.utc)
            click.echo(
                f"[fetch] Last fetch for {username}/{color}: "
                f"{last_dt.strftime('%Y-%m-%d %H:%M UTC')}"
            )
            if since_ts is None and since_date is None:
                click.echo(
                    "[fetch] Tip: pass --since <YYYY-MM-DD> for an incremental update."
                )

        count = fetch_player_games(
            username=username,
            color=color,
            cache=cache,
            speeds=speeds,
            max_plies=max_plies,
            max_games=max_games,
            since_ts=since_ts,
            verbose=True,
        )

    if count == 0:
        sys.exit(1)
