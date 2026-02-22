#!/usr/bin/env bash
# run_web.sh – start the mysecond local web interface
#
# Usage:
#   bash scripts/run_web.sh
#   bash scripts/run_web.sh --port 8080
#
# Open http://localhost:5000 (or the port you specify) in your browser.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="$REPO_ROOT/.venv"

if [[ ! -f "$VENV_DIR/bin/activate" ]]; then
    echo "Virtual environment not found. Run scripts/setup_macos.sh first." >&2
    exit 1
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

# Ensure flask is installed.
python -c "import flask" 2>/dev/null || pip install flask --quiet

PORT="${1:-5000}"

export MYSECOND_ROOT="$REPO_ROOT"
export FLASK_ENV=development

echo "Starting mysecond web UI on http://localhost:${PORT}"
echo "Press Ctrl-C to stop."
echo ""

cd "$REPO_ROOT"
python web/server.py
