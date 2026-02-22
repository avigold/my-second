#!/usr/bin/env bash
# build_web.sh – build the React novelty browser
#
# Usage:
#   bash scripts/build_web.sh          # production build
#   bash scripts/build_web.sh --watch  # rebuild on file changes (dev mode)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BROWSER_DIR="$REPO_ROOT/web/novelty-browser"

cd "$BROWSER_DIR"

if [[ ! -d node_modules ]]; then
    echo "Installing npm dependencies…"
    npm install
fi

# Copy chessground CSS into src/ (its package.json exports block direct imports)
cp node_modules/chessground/assets/chessground.base.css  src/
cp node_modules/chessground/assets/chessground.brown.css src/

if [[ "${1:-}" == "--watch" ]]; then
    echo "Watching for changes (Ctrl-C to stop)…"
    npm run build -- --watch
else
    echo "Building novelty browser…"
    npm run build
    echo "Build complete → web/static/dist/"
fi
