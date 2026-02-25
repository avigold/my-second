#!/usr/bin/env bash
# Deploy latest main branch to the production server.
#
# Usage:
#   ./scripts/deploy.sh
#
# Assumes:
#   - SSH access as deploy@157.180.60.88 (key auth)
#   - Code already pushed to origin/main

set -euo pipefail

HOST="deploy@157.180.60.88"
APP_DIR="/data/mysecond"

echo "[deploy] Pulling latest code..."
ssh "$HOST" "cd $APP_DIR && git pull"

echo "[deploy] Installing any new dependencies..."
ssh "$HOST" "cd $APP_DIR && .venv/bin/pip install --quiet -e '.[web]'"

echo "[deploy] Restarting web service..."
ssh "$HOST" "sudo systemctl restart mysecond-web"

echo "[deploy] Waiting for service..."
sleep 3
STATUS=$(ssh "$HOST" "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:5000/")
if [ "$STATUS" = "200" ]; then
  echo "[deploy] OK — server responding 200"
else
  echo "[deploy] WARNING — server returned $STATUS"
  ssh "$HOST" "sudo systemctl status mysecond-web --no-pager"
  exit 1
fi
