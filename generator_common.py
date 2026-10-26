"""Shared article loading, prompt context and archiving for the three
twice-daily generators (hotspots, actors, radar)."""

import json
import os
from datetime import datetime, timedelta, timezone

from ai_reporter import load_enriched
from monitor_utils import (
    CATEGORY_LABELS,
    STATE_FILE,
    get_sort_time,
    normalize_title_for_dedup,
    write_json_atomic,
)

MAX_PROMPT_CHARS = 350_000


def _balance(articles: list[dict], max_per_category: int) -> list[dict]:
    """Keep at most `max_per_category` articles per category, summarized ones
    first, then newest. Without a cap the busiest categories (and, while
    summaries are scarce, whichever ones have them) dominate the prompt."""
    by_cat: dict[str, list[dict]] = {}
    for a in articles:
        by_cat.setdefault(a["category"], []).append(a)
    kept = []
    for items in by_cat.values():
        items.sort(key=get_sort_time, reverse=True)
        items.sort(key=lambda a: not a["summary"])  # stable: summarized first
        kept += items[:max_per_category]
    return kept


def load_recent_articles(enriched_dir: str, hours: int, max_per_category: int = 40) -> list[dict]:
    """Articles from the last `hours` hours, with their summary where one
    exists and headline-only otherwise. Reads the enriched files, falling
    back to monitor_state.json if there are none. Blocklisted items are
    skipped."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    articles: list[dict] = []
    seen: set[str] = set()

    def add(item: dict, category: str, url: str) -> None:
        if get_sort_time(item) < cutoff:
            return
        if (item.get("extract_status") or "").startswith("blocklisted"):
            return
        key = normalize_title_for_dedup(item.get("title", ""))
        if not key or key in seen:
            return
        seen.add(key)
        articles.append({
            "title": item.get("title", ""),
            "source": item.get("source", ""),
            "url": url,
            "date": item.get("date", ""),
            "category": category,
            "summary": (item.get("summary") or "").strip(),
        })

    for a in load_enriched(enriched_dir, hours=hours) or []:
        add(a, a.get("category", "unknown"), a.get("resolved_url") or a.get("google_url", ""))

    if not articles and os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
        except Exception as e:
            print(f"  ⚠ Could not read {STATE_FILE}: {e}")
            state = {}
        for category, items in state.items():
            for item in items:
                add(item, category, item.get("url", ""))

    kept = _balance(articles, max_per_category)
    summarized = sum(1 for a in kept if a["summary"])
    print(
        f"  Using {len(kept)} of {len(articles)} articles "
        f"({summarized} with summaries, {len(kept) - summarized} headline-only)"
    )
    return kept


def build_context(articles: list[dict]) -> tuple[str, dict]:
    """Format articles into the LLM context block. Returns (text, ref_map).

    Articles are sorted newest-first and ref_map maps reference number → article.
    Headline-only articles get no SUMMARY line. Truncates if the joined context
    exceeds MAX_PROMPT_CHARS.
    """
    articles = sorted(articles, key=get_sort_time, reverse=True)
    lines: list[str] = []
    ref_map: dict[int, dict] = {}
    chars_used = 0

    for i, a in enumerate(articles, start=1):
        cat_label = CATEGORY_LABELS.get(a["category"], a["category"])
        date_short = (a.get("date") or "")[:10]
        line = f"[{i}] {a['title']} | {cat_label} | {date_short} | {a['source']}"
        if a["summary"]:
            line += f"\n    SUMMARY: {a['summary']}"
        if lines and chars_used + len(line) > MAX_PROMPT_CHARS:
            print(f"  ⚠ Truncated at {i - 1} articles to fit context window")
            break
        lines.append(line)
        ref_map[i] = a
        chars_used += len(line)

    return "\n".join(lines), ref_map


def archive_payload(payload: dict, archive_dir: str, kind: str) -> None:
    """Save a dated copy of a successful run (e.g. <archive_dir>/radar/
    radar_2026-10-26_1250.json). The public data/<kind>.json is overwritten
    every run; this keeps the history."""
    if payload.get("status") != "ok":
        return
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
    path = os.path.join(archive_dir, kind, f"{kind}_{stamp}.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    write_json_atomic(path, payload)
    print(f"  Archived {path}")
