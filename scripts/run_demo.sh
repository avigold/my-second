#!/usr/bin/env bash
# run_demo.sh – find White novelties from the Ruy Lopez (after 1.e4 e5 2.Nf3 Nc6 3.Bb5)
#
# The FEN below is the exact FEN python-chess derives after those moves:
#   r1bqkbnr/pppp1ppp/2n5/1B2p3/4P3/5N2/PPPP1PPP/RNBQK2R b KQkq - 3 3
# It is Black's turn — mysecond will follow Black's database responses and
# search for White novelties from move 4 onward.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="$REPO_ROOT/.venv"

if [[ ! -f "$VENV_DIR/bin/activate" ]]; then
    echo "Virtual environment not found. Run scripts/setup_macos.sh first." >&2
    exit 1
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

echo "==> mysecond demo"
echo "    White novelties from: 1.e4 e5 2.Nf3 Nc6 3.Bb5 (Ruy Lopez)"
echo ""

mysecond \
    --fen "r1bqkbnr/pppp1ppp/2n5/1B2p3/4P3/5N2/PPPP1PPP/RNBQK2R b KQkq - 3 3" \
    --side white \
    --plies 20 \
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
    --out "$REPO_ROOT/ideas.pgn"

echo ""
echo "Output written to: $REPO_ROOT/ideas.pgn"
echo "Import into ChessBase: File → Open → ideas.pgn"
echo ""
echo "PGN structure per novelty:"
echo "  - Book moves played unannotated (the known theory)"
echo "  - Novelty move marked N + \$146 (ChessBase novelty marker)"
echo "  - [%eval] drives the ChessBase engine evaluation bar"
echo "  - Engine PV continues for 8 moves after the novelty"
