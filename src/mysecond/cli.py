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
from .fetcher import fetch_player_games, fetch_player_games_chesscom, import_pgn_player, last_fetch_ts
from .habits import analyze_habits, export_habits_pgn
from .repertoire_extract import RepertoireStats, export_repertoire_pgn, extract_repertoire
from .strategise import strategise
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
    "--player-platform",
    "player_platform",
    default="lichess",
    show_default=True,
    type=click.Choice(["lichess", "chesscom"]),
    help="Platform the player's games were fetched from.",
)
@click.option(
    "--opponent-platform",
    "opponent_platform",
    default="lichess",
    show_default=True,
    type=click.Choice(["lichess", "chesscom"]),
    help="Platform the opponent's games were fetched from.",
)
@click.option(
    "--player-speeds",
    "player_speeds",
    default="blitz,rapid,classical",
    show_default=True,
    help="Time controls to include for the player.",
)
@click.option(
    "--opponent-speeds",
    "opponent_speeds",
    default="blitz,rapid,classical",
    show_default=True,
    help="Time controls to include for the opponent.",
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
    player_platform: str,
    opponent_platform: str,
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
        click.echo(f"  Player:             {player_name} (as {side}, {player_platform})")
    if opponent_name:
        click.echo(f"  Opponent:           {opponent_name} ({opponent_platform})")
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
        player_platform=player_platform,
        opponent_platform=opponent_platform,
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
    help="Player username (Lichess or Chess.com depending on --platform).",
)
@click.option(
    "--color",
    required=True,
    type=click.Choice(["white", "black"]),
    help="Fetch games where the player was this colour.",
)
@click.option(
    "--platform",
    default="lichess",
    show_default=True,
    type=click.Choice(["lichess", "chesscom"]),
    help=(
        "Platform to fetch from. "
        "Note: Chess.com uses blitz/rapid/bullet/daily — 'classical' will not match."
    ),
)
@click.option(
    "--speeds",
    default="blitz,rapid,classical",
    show_default=True,
    help=(
        "Comma-separated time controls to include. "
        "Lichess: bullet,blitz,rapid,classical. "
        "Chess.com: bullet,blitz,rapid,daily (no classical)."
    ),
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
    platform: str,
    speeds: str,
    max_games: int,
    max_plies: int,
    since_date: str | None,
    db_path: str,
) -> None:
    """Download a player's games and index them for fast local lookups.

    \b
    Examples:
      # Lichess (default):
      mysecond fetch-player-games --username GothamChess --color white

      # Chess.com:
      mysecond fetch-player-games --username Hikaru --platform chesscom \\
          --color white --speeds blitz,rapid

      # Incremental update (merge new games into existing cache):
      mysecond fetch-player-games --username GothamChess --color white \\
          --since 2024-01-01

    After fetching, 'mysecond search --player <username>' reads repertoire
    data from the local cache instead of making HTTP requests.
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
        last_ts = last_fetch_ts(username, color, speeds, cache, platform=platform)
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

        if platform == "chesscom":
            count = fetch_player_games_chesscom(
                username=username,
                color=color,
                cache=cache,
                speeds=speeds,
                max_plies=max_plies,
                max_games=max_games,
                since_ts=since_ts,
                verbose=True,
            )
        else:
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


# ---------------------------------------------------------------------------
# import-pgn-player
# ---------------------------------------------------------------------------


@main.command("import-pgn-player")
@click.option(
    "--pgn",
    "pgn_path",
    required=True,
    help="Path to the PGN file containing the player's games.",
)
@click.option(
    "--username",
    required=True,
    help=(
        "Cache key to store the data under — use the same value you will "
        "pass to 'mysecond search --player' (e.g. 'GothamChess')."
    ),
)
@click.option(
    "--color",
    required=True,
    type=click.Choice(["white", "black"]),
    help="Index games where the player was this colour.",
)
@click.option(
    "--max-plies",
    "max_plies",
    default=30,
    show_default=True,
    help="Walk each game at most this many half-moves (opening depth).",
)
@click.option(
    "--db",
    "db_path",
    default=str(_FETCH_DB),
    show_default=True,
    help="Path to the SQLite cache database.",
)
def import_pgn_cmd(
    pgn_path: str,
    username: str,
    color: str,
    max_plies: int,
    db_path: str,
) -> None:
    """Index a local PGN file as player opening data.

    Use this instead of fetch-player-games when the player's Lichess
    account is inactive or you want to use OTB games from another source
    (chessgames.com, FIDE, TWIC, etc.).

    \b
    Example workflow:
      # 1. Download Levy Rozman's games from chessgames.com as PGN, then:
      mysecond import-pgn-player --pgn levy.pgn --username GothamChess --color white
      # 2. Now search using his real repertoire:
      mysecond search --player GothamChess --opponent im_eric_rosen --side white ...
    """
    p = Path(pgn_path)
    if not p.exists():
        click.echo(f"Error: PGN file not found: {p}", err=True)
        sys.exit(1)

    db = Path(db_path)
    with Cache(db) as cache:
        count = import_pgn_player(
            pgn_path=p,
            username=username,
            color=color,
            cache=cache,
            max_plies=max_plies,
            verbose=True,
        )

    if count == 0:
        click.echo("[import] Warning: no positions were indexed.", err=True)
        sys.exit(1)


# ---------------------------------------------------------------------------
# analyse-habits
# ---------------------------------------------------------------------------


@main.command("analyse-habits")
@click.option("--username", required=True, help="Player username.")
@click.option(
    "--color",
    required=True,
    type=click.Choice(["white", "black"]),
    help="Side the player was playing.",
)
@click.option(
    "--platform",
    default="lichess",
    show_default=True,
    type=click.Choice(["lichess", "chesscom"]),
    help="Platform the games were fetched from.",
)
@click.option(
    "--speeds",
    default="blitz,rapid,classical",
    show_default=True,
    help="Time controls — must match the --speeds used when fetching games.",
)
@click.option(
    "--min-games",
    "min_games",
    default=5,
    show_default=True,
    help="Minimum times the player must have reached a position.",
)
@click.option(
    "--max-positions",
    "max_positions",
    default=50,
    show_default=True,
    help="Maximum number of habit inaccuracies to report.",
)
@click.option(
    "--min-eval-gap",
    "min_eval_gap",
    default=25,
    show_default=True,
    help="Minimum centipawn gap (best move vs player's move) to flag an inaccuracy.",
)
@click.option(
    "--depth",
    default=20,
    show_default=True,
    help="Stockfish search depth per position.",
)
@click.option(
    "--out",
    "out_path",
    default="habits.pgn",
    show_default=True,
    help="Output PGN file.",
)
@click.option(
    "--db",
    "db_path",
    default=str(_FETCH_DB),
    show_default=True,
    help="Path to the SQLite cache database.",
)
def analyze_habits_cmd(
    username: str,
    color: str,
    platform: str,
    speeds: str,
    min_games: int,
    max_positions: int,
    min_eval_gap: int,
    depth: int,
    out_path: str,
    db_path: str,
) -> None:
    """Find positions where a player habitually plays a suboptimal move.

    Identifies positions the player reaches frequently where they consistently
    choose a non-optimal move, and exports an annotated PGN for ChessBase import.
    Games are fetched automatically if not already cached.

    \b
    Example:
      mysecond analyse-habits --username Hikaru --platform chesscom \\
          --color white --speeds blitz,rapid --min-games 10

    The output PGN shows each problem position with the player's habitual move
    marked ?! or ? and a variation showing the engine's recommendation.
    """
    try:
        engine_path = find_stockfish()
        click.echo(f"[habits] Engine: {engine_path}")
    except FileNotFoundError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    output = Path(out_path)
    db = Path(db_path)

    click.echo(f"[habits] Analysing habits for {username} ({color}, {platform}, {speeds})")
    click.echo(f"  Min games: {min_games}  Max positions: {max_positions}  "
               f"Min eval gap: {min_eval_gap}cp  Depth: {depth}")

    with Cache(db) as cache:
        habits = analyze_habits(
            username=username,
            color=color,
            cache=cache,
            engine_path=engine_path,
            speeds=speeds,
            platform=platform,
            min_games=min_games,
            max_positions=max_positions,
            min_eval_gap=min_eval_gap,
            depth=depth,
            verbose=True,
        )

    if not habits:
        click.echo(
            "[habits] No habit inaccuracies found.\n"
            "  Try: --min-games lower, --min-eval-gap lower, "
            "or ensure you have fetched enough games."
        )
        sys.exit(0)

    click.echo(f"[habits] Exporting {len(habits)} inaccuracies → {output}")
    export_habits_pgn(habits, output, username, color)

    click.echo("\n[habits] Top 5 habit inaccuracies:")
    for i, h in enumerate(habits[:5], 1):
        click.echo(
            f"  {i}. {h.player_move_san} → {h.best_move_san}  "
            f"gap={h.eval_gap_cp:+.0f}cp  "
            f"freq={h.total_games}  score={h.score:.1f}"
        )

    click.echo("\n[habits] Done.")


# ---------------------------------------------------------------------------
# extract-repertoire
# ---------------------------------------------------------------------------


@main.command("extract-repertoire")
@click.option("--username", required=True, help="Player username.")
@click.option(
    "--color",
    required=True,
    type=click.Choice(["white", "black"]),
    help="Side to extract repertoire for.",
)
@click.option(
    "--platform",
    default="lichess",
    show_default=True,
    type=click.Choice(["lichess", "chesscom"]),
    help="Platform the games were fetched from.",
)
@click.option(
    "--speeds",
    default="blitz,rapid,classical",
    show_default=True,
    help="Time controls — must match the fetch-player-games run.",
)
@click.option(
    "--min-games",
    "min_games",
    default=5,
    show_default=True,
    help="A move must appear at least this many times to be included.",
)
@click.option(
    "--max-plies",
    "max_plies",
    default=20,
    show_default=True,
    help="Maximum repertoire depth in half-moves.",
)
@click.option(
    "--out",
    "out_path",
    default="repertoire.pgn",
    show_default=True,
    help="Output PGN file.",
)
@click.option(
    "--db",
    "db_path",
    default=str(_FETCH_DB),
    show_default=True,
    help="Path to the SQLite cache database.",
)
def extract_repertoire_cmd(
    username: str,
    color: str,
    platform: str,
    speeds: str,
    min_games: int,
    max_plies: int,
    out_path: str,
    db_path: str,
) -> None:
    """Reconstruct a player's opening repertoire from cached game data.

    Walks the position tree from the starting position, following moves
    the player has played at least --min-games times. The resulting PGN
    contains branching variations with frequency annotations, importable
    into ChessBase, Lichess studies, etc.

    \b
    Example workflow:
      mysecond fetch-player-games --username Hikaru --platform chesscom \\
          --color white --speeds blitz,rapid
      mysecond extract-repertoire --username Hikaru --platform chesscom \\
          --color white --speeds blitz,rapid --min-games 10 --out hikaru_white.pgn
    """
    output = Path(out_path)
    db = Path(db_path)

    click.echo("[repertoire] Configuration")
    click.echo(f"  Username:   {username} ({color}, {platform})")
    click.echo(f"  Speeds:     {speeds}")
    click.echo(f"  Min games:  {min_games}")
    click.echo(f"  Max plies:  {max_plies}")
    click.echo(f"  Output:     {output}")

    with Cache(db) as cache:
        try:
            game, stats = extract_repertoire(
                username=username,
                color=color,
                cache=cache,
                speeds=speeds,
                platform=platform,
                min_games=min_games,
                max_plies=max_plies,
                verbose=True,
            )
        except RuntimeError as exc:
            click.echo(f"Error: {exc}", err=True)
            sys.exit(1)

    export_repertoire_pgn(game, output)
    click.echo(f"\n[repertoire] Exported → {output}")
    click.echo(f"  Positions visited : {stats.total_positions}")
    click.echo(f"  Player moves      : {stats.total_player_moves}")
    click.echo(f"  Max depth         : {stats.max_depth_reached} plies")
    click.echo("\n[repertoire] Done.")


# ---------------------------------------------------------------------------
# strategise
# ---------------------------------------------------------------------------


@main.command("strategise")
@click.option("--player",           required=True, help="Preparing player username.")
@click.option("--player-platform",  "player_platform",
              default="lichess", show_default=True,
              type=click.Choice(["lichess", "chesscom"]))
@click.option("--player-color",     "player_color", required=True,
              type=click.Choice(["white", "black"]),
              help="Colour the preparing player will have.")
@click.option("--player-speeds",    "player_speeds",
              default="blitz,rapid,classical", show_default=True)
@click.option("--opponent",         required=True, help="Opponent username.")
@click.option("--opponent-platform","opponent_platform",
              default="lichess", show_default=True,
              type=click.Choice(["lichess", "chesscom"]))
@click.option("--opponent-speeds",  "opponent_speeds",
              default="blitz,rapid,classical", show_default=True)
@click.option("--min-games",        "min_games",      default=5,   show_default=True)
@click.option("--max-positions",    "max_positions",  default=30,  show_default=True)
@click.option("--min-eval-gap",     "min_eval_gap",   default=25,  show_default=True)
@click.option("--depth",            default=18, show_default=True)
@click.option("--out",              "out_path",        default="strategise.json", show_default=True)
@click.option("--api-key",          "anthropic_api_key",
              default=None, envvar="ANTHROPIC_API_KEY",
              help="Anthropic API key for the AI strategic brief.")
@click.option("--db",               "db_path",
              default=str(_FETCH_DB), show_default=True)
def strategise_cmd(
    player, player_platform, player_color, player_speeds,
    opponent, opponent_platform, opponent_speeds,
    min_games, max_positions, min_eval_gap, depth,
    out_path, anthropic_api_key, db_path,
):
    """Generate a strategic preparation brief for PLAYER vs OPPONENT.

    \b
    Both players' games are fetched automatically if not cached.
    Stockfish is required for habit analysis.
    Pass --api-key (or set ANTHROPIC_API_KEY) to add an AI strategic brief.

    \b
    Example:
      mysecond strategise --player Hikaru --player-platform chesscom \\
          --player-color white --opponent MagnusCarlsen \\
          --opponent-platform lichess --out brief.json --api-key sk-ant-…
    """
    try:
        engine_path = find_stockfish()
        click.echo(f"[strategise] Engine: {engine_path}")
    except FileNotFoundError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    with Cache(Path(db_path)) as cache:
        strategise(
            player=player,
            player_platform=player_platform,
            player_color=player_color,
            player_speeds=player_speeds,
            opponent=opponent,
            opponent_platform=opponent_platform,
            opponent_speeds=opponent_speeds,
            cache=cache,
            engine_path=engine_path,
            out_path=Path(out_path),
            min_games=min_games,
            max_positions=max_positions,
            min_eval_gap=min_eval_gap,
            depth=depth,
            anthropic_api_key=anthropic_api_key,
            verbose=True,
        )
