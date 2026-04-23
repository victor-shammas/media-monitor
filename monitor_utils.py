"""Shared utilities for the media monitor pipeline."""

import json
import os
import re
import subprocess
import sys
import unicodedata
from datetime import datetime, timezone

import tomllib

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

STATE_FILE = "data/monitor_state.json"
BLOCKLIST_FILE = "blocklist.json"

with open(os.path.join(_SCRIPT_DIR, "config.toml"), "rb") as _f:
    CONFIG = tomllib.load(_f)
CATEGORY_LABELS = CONFIG["categories"]


def get_sort_time(item: dict) -> datetime:
    """Robustly sort items by publication date."""
    date_str = item.get("date", "")
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except Exception:
        added_str = item.get("added_at", "")
        try:
            return datetime.strptime(added_str, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            )
        except Exception:
            return datetime.min.replace(tzinfo=timezone.utc)


def normalize_title_for_dedup(title: str) -> str:
    """Normalize a title for dedup comparison. Strips trailing ' - Publisher'
    to catch legacy titles and publishers with hyphens in their names."""
    idx = title.rfind(" - ")
    t = (title[:idx] if idx > 0 else title).lower()
    t = unicodedata.normalize("NFKC", t)
    t = t.replace("’", "'").replace("‘", "'")
    t = t.replace("“", '"').replace("”", '"')
    t = t.replace("–", "-").replace("—", "-")
    t = t.replace("…", "...")
    t = re.sub(r"[^\w\s]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def load_blocklist() -> dict:
    """Load the blocklist file. Returns dict with urls (set), sources (set), title_patterns (list)."""
    if os.path.exists(BLOCKLIST_FILE):
        try:
            with open(BLOCKLIST_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return {
                "urls": set(data.get("urls", [])),
                "sources": {s.lower() for s in data.get("sources", [])},
                "title_patterns": [p.lower() for p in data.get("title_patterns", [])],
            }
        except Exception:
            pass
    return {"urls": set(), "sources": set(), "title_patterns": []}


def git_sync():
    """Pull latest changes if the local branch is behind origin."""
    repo_dir = _SCRIPT_DIR
    try:
        subprocess.run(
            ["git", "fetch"], cwd=repo_dir, capture_output=True, timeout=15
        )
        result = subprocess.run(
            ["git", "status", "--porcelain", "-b"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if "behind" in result.stdout:
            print("  ↓ Local branch is behind origin — pulling...", file=sys.stderr)
            subprocess.run(
                ["git", "pull", "--rebase"],
                cwd=repo_dir,
                capture_output=True,
                timeout=30,
            )
            print("  ✓ Pulled latest changes", file=sys.stderr)
    except Exception as e:
        print(f"  Warning: git sync check failed: {e}", file=sys.stderr)
