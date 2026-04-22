#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

# 1. Push submodule (data-private)
if git -C data-private status --porcelain | grep -q .; then
    echo "Pushing data-private..."
    git -C data-private add -A
    git -C data-private commit -m "Update data $(date -u +%Y-%m-%d)"
    git -C data-private push
else
    echo "data-private: nothing to commit"
fi

# 2. Push parent repo
if git status --porcelain | grep -q .; then
    echo "Pushing parent repo..."
    git add -A
    git commit -m "Automated monitor update"
    git push
else
    echo "Parent repo: nothing to commit"
fi

echo "Done."
