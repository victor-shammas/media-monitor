#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

# Pull latest from both repos to avoid conflicts with CI
echo "Pulling latest..."
git -C data-private pull --rebase || true
git pull --rebase || true

# If rebase left conflicts (CI ran during our scraper), resolve by keeping local
if git -C data-private diff --name-only --diff-filter=U 2>/dev/null | grep -q .; then
    git -C data-private checkout --theirs . && git -C data-private add -A
    git -C data-private rebase --continue || true
fi
if git diff --name-only --diff-filter=U 2>/dev/null | grep -q .; then
    git checkout --theirs feeds/ data/monitor_state.json 2>/dev/null || true
    git add feeds/ data/monitor_state.json 2>/dev/null || true
    git rebase --continue || true
fi

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
