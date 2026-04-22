#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

# Pull latest state so the scraper builds on CI's data
echo "Pulling latest..."
git -C data-private pull --rebase || true
git pull --rebase || true

# Run scraper (pass through any args, e.g. --hours 4)
python article_scraper.py "$@"

# Commit and push
./push.sh
