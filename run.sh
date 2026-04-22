#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

# Pull latest state so the scraper builds on CI's data
echo "Pulling latest..."
git -C data-private pull --rebase || true
git pull --rebase || true

# Harvest new links, then scrape content and generate summaries
python media-monitor.py
python article_scraper.py "$@"

# Commit and push
./push.sh
