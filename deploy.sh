#!/bin/bash
set -e
cd /data/mysecond
sudo -u deploy git pull origin main
echo "--- Building frontend ---"
cd web/novelty-browser && npm ci && npm run build && cd ../..
echo "--- Restarting service ---"
systemctl restart mysecond-web
echo "Deploy complete."
