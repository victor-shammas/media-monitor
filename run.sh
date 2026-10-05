#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

echo "Pulling latest..."
git -C data-private pull --rebase || true
git pull --rebase || true

# Unified pipeline: fetch RSS + enrich articles + generate summaries
python3 media-monitor.py --enrich "$@"

# Commit and push
./push.sh
