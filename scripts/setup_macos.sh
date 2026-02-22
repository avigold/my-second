#!/usr/bin/env bash
# setup_macos.sh – bootstrap mysecond on macOS
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="$REPO_ROOT/.venv"

echo "==> mysecond setup (macOS)"

# ── Stockfish ────────────────────────────────────────────────────────────────
if command -v stockfish &>/dev/null; then
    echo "    stockfish already installed: $(which stockfish)"
else
    if ! command -v brew &>/dev/null; then
        echo "ERROR: Homebrew not found. Install it from https://brew.sh/ and re-run." >&2
        exit 1
    fi
    echo "    Installing stockfish via Homebrew …"
    brew install stockfish
fi

# ── Python venv ──────────────────────────────────────────────────────────────
PYTHON="python3.11"
if ! command -v "$PYTHON" &>/dev/null; then
    # Fall back to whatever python3 is available and warn.
    PYTHON="python3"
    echo "    WARNING: python3.11 not found; using $PYTHON ($(python3 --version))."
    echo "             Install Python 3.11 with: brew install python@3.11"
fi

echo "    Creating virtual environment at $VENV_DIR …"
"$PYTHON" -m venv "$VENV_DIR"

# ── Dependencies ─────────────────────────────────────────────────────────────
echo "    Installing dependencies …"
"$VENV_DIR/bin/pip" install --upgrade pip --quiet
"$VENV_DIR/bin/pip" install --quiet -e "$REPO_ROOT[test]"

# ── data dir ─────────────────────────────────────────────────────────────────
mkdir -p "$REPO_ROOT/data"

# ── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo "Setup complete!"
echo ""
echo "Usage:"
echo "  source $VENV_DIR/bin/activate"
echo ""
echo "  mysecond \\"
echo '    --fen "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1" \\'
echo "    --side white \\"
echo "    --plies 12 \\"
echo "    --beam 30 \\"
echo "    --depths 16,20,24 \\"
echo "    --time-ms 150 \\"
echo "    --out ideas.pgn"
echo ""
echo "  # Override engine path:"
echo "  export MYSECOND_STOCKFISH_PATH=/opt/homebrew/bin/stockfish"
echo ""
echo "Run tests:"
echo "  pytest tests/"
