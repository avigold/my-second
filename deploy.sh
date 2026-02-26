#!/bin/bash
set -e
cd /data/mysecond
sudo -u deploy git pull origin main
cd web/novelty-browser && npm ci --prefer-offline && npm run build && cd ../..
systemctl restart mysecond-web
echo "Deploy complete."
