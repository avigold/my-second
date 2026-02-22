#!/usr/bin/env bash
# run_demo_players.sh – prepare novelties for a specific player against a named opponent
#
# Example: prepare lines for GothamChess (Levy Rozman, playing White)
#          against im_eric_rosen (Eric Rosen, playing Black).
#
# Step 1: fetch-player-games downloads each player's Lichess game history and
#         indexes it locally.  This is slow once (network-bound) but all data
#         is cached in data/cache.sqlite — subsequent runs are instant.
#
# Step 2: search uses only the local cache.  No /player API calls at runtime.
#
# Usage:
#   bash scripts/run_demo_players.sh
#   bash scripts/run_demo_players.sh GothamChess im_eric_rosen white
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="$REPO_ROOT/.venv"

if [[ ! -f "$VENV_DIR/bin/activate" ]]; then
    echo "Virtual environment not found. Run scripts/setup_macos.sh first." >&2
    exit 1
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

PLAYER="${1:-GothamChess}"
OPPONENT="${2:-im_eric_rosen}"
SIDE="${3:-white}"
SPEEDS="bullet,blitz,rapid,classical"

# Opposite colour for the opponent.
if [[ "$SIDE" == "white" ]]; then
    OPP_COLOR="black"
else
    OPP_COLOR="white"
fi

echo "==> mysecond player-based preparation"
echo "    Player:   $PLAYER (as $SIDE)"
echo "    Opponent: $OPPONENT (as $OPP_COLOR)"
echo ""

# ---------------------------------------------------------------------------
# Step 1: Download and index game histories (skips if already cached)
# ---------------------------------------------------------------------------
echo "--- Step 1: Fetching game histories ---"
echo ""

echo ">> Fetching $PLAYER's games as $SIDE …"
mysecond fetch-player-games \
    --username "$PLAYER" \
    --color "$SIDE" \
    --speeds "$SPEEDS" \
    --max-games 10000 \
    --max-plies 30

echo ""
echo ">> Fetching $OPPONENT's games as $OPP_COLOR …"
mysecond fetch-player-games \
    --username "$OPPONENT" \
    --color "$OPP_COLOR" \
    --speeds "$SPEEDS" \
    --max-games 10000 \
    --max-plies 30

echo ""

# ---------------------------------------------------------------------------
# Step 2: Search for novelties (fast — all repertoire data from local cache)
# ---------------------------------------------------------------------------
echo "--- Step 2: Searching for novelties ---"
echo ""

OUT="$REPO_ROOT/ideas_${PLAYER}_vs_${OPPONENT}.pgn"

mysecond search \
    --side "$SIDE" \
    --player "$PLAYER" \
    --opponent "$OPPONENT" \
    --plies 30 \
    --beam 8 \
    --min-book-games 5 \
    --novelty-threshold 2 \
    --opponent-responses 3 \
    --depths 20,24 \
    --time-ms 200 \
    --min-eval 0 \
    --continuations 8 \
    --workers 4 \
    --max-positions 5000 \
    --max-candidates 100 \
    --min-player-games 3 \
    --min-opponent-games 3 \
    --player-speeds "$SPEEDS" \
    --opponent-speeds "$SPEEDS" \
    --out "$OUT"

echo ""
echo "Output written to: $OUT"
echo "Import into ChessBase: File → Open → $(basename "$OUT")"
echo ""
echo "These are novelties in lines that:"
echo "  - $PLAYER has played before (or would naturally reach as $SIDE)"
echo "  - $OPPONENT has faced and played as $OPP_COLOR in their Lichess games"
echo ""
echo "Tip: run with --since YYYY-MM-DD to update only with new games:"
echo "  mysecond fetch-player-games --username $PLAYER --color $SIDE --since 2024-01-01"
