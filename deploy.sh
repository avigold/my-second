#!/bin/bash
set -e
cd /data/mysecond
sudo -u deploy git pull origin main
systemctl restart mysecond-web
echo "Deploy complete."
