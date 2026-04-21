#!/usr/bin/env python3
"""
Transatlantic Right-Wing Media Monitor

Maintains a database of seen articles from Google News RSS feeds across various
countries and categories. It regenerates cleanly grouped individual text files
for each category, enforcing strict chronological sorting, and never visits
article URLs directly to avoid rate limits and 403 errors.

Usage Examples:
  python media-monitor.py                              → Fetch all feeds (default)
  python media-monitor.py --feeds norway usa            → Fetch specific feeds only
  python media-monitor.py -d custom_folder/            → Write text files to custom dir
  python media-monitor.py --rebuild                    → Regenerate text files from state (no fetch)

Blocklist Management:
  python media-monitor.py --block <url>                → Block a single URL and purge from state
  python media-monitor.py --block-source "Daily Mail"  → Block all items from a source
  python media-monitor.py --block-pattern "horoscope"  → Block titles containing a phrase
  python media-monitor.py --show-blocklist             → Display the current blocklist
  python media-monitor.py --unblock "Daily Mail"       → Remove an entry from the blocklist

Flags:
  -d, --outdir DIR          Output directory for per-category text files (default: feeds/)
  --feeds [ID ...]          Only run specific feed IDs (e.g., norway, usa, sweden, germany, networks)
  --rebuild                 Regenerate all text files from monitor_state.json without fetching
  --archive-days N          Archive articles older than N days (default: 60)
  --block URL               Block a single article by URL and prevent re-ingestion
  --block-source SOURCE     Block all articles from a named source (case-insensitive)
  --block-pattern PHRASE    Block any article whose title contains this phrase (case-insensitive)
  --unblock ENTRY           Remove a URL, source, or pattern from the blocklist
  --show-blocklist          Print the current blocklist and exit

Data files:
  monitor_state.json        Persistent article database (one key per feed category)
  archive.jsonl             Long-term archive of pruned articles (one JSON record per line)
  blocklist.json            Blocked URLs, sources, and title patterns (created on first use)
  feeds/*.txt               Human-readable per-category article listings (regenerated each run)
"""

import argparse
import html as html_mod
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote
from urllib.request import Request, urlopen

import tomllib

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.toml"), "rb") as _f:
    CONFIG = tomllib.load(_f)
CATEGORY_LABELS = CONFIG["categories"]

# ── Configuration ──────────────────────────────────────────────────────────

STATE_FILE = "monitor_state.json"
BLOCKLIST_FILE = "blocklist.json"
ARCHIVE_FILE = "archive.jsonl"
MAX_NEW_PER_RUN = 30  # The universal limit of new articles to add per run per category
DEFAULT_ARCHIVE_DAYS = 60  # Articles older than this are archived and pruned from state

FEEDS = CONFIG["feeds"]

# ── Helpers ────────────────────────────────────────────────────────────────


def build_gnews_url(query: str, lang: str, country: str, window: str = "7d") -> str:
    return (
        f"https://news.google.com/rss/search?"
        f"q={quote(query + ' when:' + window)}"
        f"&hl={lang}&gl={country}&ceid={country}:{lang}"
    )


def fetch_rss(url: str, timeout: int = 15) -> list[dict]:
    req = Request(
        url, headers={"User-Agent": "Mozilla/5.0 (compatible; RWMonitor/4.2)"}
    )
    with urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    root = ET.fromstring(raw)
    items = []
    for item_el in root.iter("item"):
        title = (item_el.findtext("title") or "").strip()
        link = (item_el.findtext("link") or "").strip()
        desc = (item_el.findtext("description") or "").strip()
        pub_date_str = (item_el.findtext("pubDate") or "").strip()

        pub_dt = None
        if pub_date_str:
            try:
                pub_dt = parsedate_to_datetime(pub_date_str)
                if pub_dt.tzinfo is None:
                    pub_dt = pub_dt.replace(tzinfo=timezone.utc)
            except Exception:
                pass

        items.append(
            {
                "title": title,
                "link": link,
                "description": desc,
                "pubDate": pub_dt.isoformat() if pub_dt else pub_date_str,
                "pub_dt": pub_dt,
            }
        )
    return items


def strip_html(text: str) -> str:
    if not text:
        return ""
    t = re.sub(r"<[^>]+>", " ", text)
    t = html_mod.unescape(t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:220] + "…" if len(t) > 220 else t


def extract_source(title: str) -> str:
    m = re.search(r"\s-\s([^-]+)$", title)
    return m.group(1).strip() if m else ""


def clean_title(title: str) -> str:
    return re.sub(r"\s-\s[^-]+$", "", title).strip()


def strip_trailing_source(summary: str, source: str) -> str:
    if not summary or not source:
        return summary
    esc = re.escape(source)
    return re.sub(
        r"[\s\-–—]*" + esc + r"\s*$", "", summary, flags=re.IGNORECASE
    ).strip()


def fmt_date(iso: str) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%b %d")
    except Exception:
        return iso[:10]


def get_sort_time(item: dict) -> datetime:
    """Helper to robustly sort items by publication date."""
    date_str = item.get("date", "")
    try:
        # Try to parse the ISO formatted string saved in the JSON
        return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except Exception:
        # Fallback to the exact time the script added it
        added_str = item.get("added_at", "")
        try:
            return datetime.strptime(added_str, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            )
        except Exception:
            return datetime.min.replace(tzinfo=timezone.utc)


# ── Blocklist ─────────────────────────────────────────────────────────────


def load_blocklist() -> dict:
    """Load the blocklist file. Structure:
    {
      "urls": ["https://..."],
      "sources": ["SomeSpamSite"],
      "title_patterns": ["unwanted phrase"]
    }
    """
    if os.path.exists(BLOCKLIST_FILE):
        try:
            with open(BLOCKLIST_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Normalize: lowercase sources and patterns for case-insensitive matching
            return {
                "urls": set(data.get("urls", [])),
                "sources": {s.lower() for s in data.get("sources", [])},
                "title_patterns": [p.lower() for p in data.get("title_patterns", [])],
            }
        except Exception:
            pass
    return {"urls": set(), "sources": set(), "title_patterns": []}


def save_blocklist(blocklist: dict) -> None:
    """Persist the blocklist to disk."""
    data = {
        "urls": sorted(blocklist["urls"]),
        "sources": sorted(blocklist["sources"]),
        "title_patterns": sorted(blocklist["title_patterns"]),
    }
    with open(BLOCKLIST_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def is_blocked(item: dict, blocklist: dict) -> bool:
    """Check whether an item matches any blocklist rule."""
    if item.get("url", "") in blocklist["urls"]:
        return True
    if item.get("source", "").lower() in blocklist["sources"]:
        return True
    title_lower = item.get("title", "").lower()
    for pattern in blocklist["title_patterns"]:
        if pattern in title_lower:
            return True
    return False


def purge_blocked_from_state(state: dict, blocklist: dict) -> int:
    """Remove any blocked items already in state. Returns count removed."""
    removed = 0
    for fid in state:
        before = len(state[fid])
        state[fid] = [item for item in state[fid] if not is_blocked(item, blocklist)]
        removed += before - len(state[fid])
    return removed


# ── Archiving ─────────────────────────────────────────────────────────────


def prune_and_archive(state: dict, archive_days: int) -> int:
    """Move articles older than archive_days from state into archive.jsonl.

    Each archived article is written as a single JSON line with a 'feed_id'
    field added so the record is self-contained.  Returns count archived.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=archive_days)
    archived = 0

    lines_to_write = []
    for fid in state:
        keep = []
        for item in state[fid]:
            item_time = get_sort_time(item)
            if item_time < cutoff:
                record = dict(item, feed_id=fid)
                lines_to_write.append(json.dumps(record, ensure_ascii=False))
                archived += 1
            else:
                keep.append(item)
        state[fid] = keep

    if lines_to_write:
        with open(ARCHIVE_FILE, "a", encoding="utf-8") as f:
            for line in lines_to_write:
                f.write(line + "\n")

    return archived


# ── Core Logic ─────────────────────────────────────────────────────────────


def fetch_feed(
    feed: dict, category_seen_urls: set, timestamp: str, blocklist: dict | None = None
) -> list[dict]:
    queries = feed.get("queries", [feed.get("q", "")])
    window = feed.get("window", "7d")
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    new_items = []
    errors = []
    blocked_count = 0

    # Resolve language variants: explicit list, or synthesize from flat lang/country
    variants = feed.get("variants") or [
        {"lang": feed["lang"], "country": feed["country"]}
    ]

    for variant in variants:
        lang = variant["lang"]
        country = variant["country"]
        # Per-variant query override, falling back to feed-level queries
        variant_queries = variant.get("queries") or (
            [variant["q"]] if "q" in variant else queries
        )
        for q in variant_queries:
            url = build_gnews_url(q, lang, country, window)
            try:
                raw_items = fetch_rss(url)
                for item in raw_items:
                    link = item.get("link", "")

                    # Check against the category's private memory
                    if not link or link in category_seen_urls:
                        continue
                    category_seen_urls.add(link)

                    pub_dt = item.get("pub_dt")
                    if pub_dt and pub_dt < cutoff:
                        continue

                    source = extract_source(item.get("title", ""))
                    summary = strip_html(item.get("description", ""))
                    summary = strip_trailing_source(summary, source)

                    candidate = {
                        "title": clean_title(item.get("title", "")),
                        "url": link,
                        "source": source,
                        "date": item.get("pubDate", ""),
                        "added_at": timestamp,
                        "summary": summary,
                    }

                    # Check against blocklist before accepting
                    if blocklist and is_blocked(candidate, blocklist):
                        blocked_count += 1
                        continue

                    new_items.append(candidate)
            except Exception as e:
                errors.append(f"[{lang}] {q[:40]}…: {e}")

    if blocked_count:
        print(f"    ✗ Blocked {blocked_count} item(s) via blocklist", file=sys.stderr)
    if errors:
        for err in errors[:3]:
            print(f"  ⚠ {err}", file=sys.stderr)

    # Universal cap of new items per run
    return new_items[:MAX_NEW_PER_RUN]


# ── Formatter ──────────────────────────────────────────────────────────────

SEPARATOR = "═" * 72


def format_single_feed(feed: dict, items: list[dict], last_updated: str) -> str:
    lines = []
    lines.append(SEPARATOR)
    lines.append("  TRANSATLANTIC RIGHT-WING MEDIA MONITOR")
    lines.append(f"  Category:     {CATEGORY_LABELS.get(feed['id'], feed['id'])}")
    lines.append(f"  Total Items:  {len(items)}")
    lines.append(f"  Last Updated: {last_updated}")
    lines.append(SEPARATOR)
    lines.append("")

    if not items:
        lines.append("  (no results)")

    for i, item in enumerate(items, 1):
        meta_parts = filter(None, [item.get("source", ""), fmt_date(item["date"])])
        meta = " · ".join(meta_parts)

        lines.append(f"{i}. {item['title']}")
        if meta:
            lines.append(f"   Published: {meta}")

        lines.append(f"   Added:     {item.get('added_at', 'Unknown')}")

        if item["summary"]:
            lines.append(f"   {item['summary']}")
        lines.append(f"   {item['url']}")
        lines.append("")

    return "\n".join(lines)


# ── Main ───────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Stateful media monitor (Multi-File)")
    parser.add_argument(
        "-d",
        "--outdir",
        default="feeds",
        help="Directory to save the individual text files",
    )
    parser.add_argument(
        "--feeds", nargs="*", default=None, help="Specific feeds to run"
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Regenerate all text files from monitor_state.json without fetching",
    )
    parser.add_argument(
        "--archive-days",
        type=int,
        default=DEFAULT_ARCHIVE_DAYS,
        metavar="N",
        help=f"Archive articles older than N days (default: {DEFAULT_ARCHIVE_DAYS})",
    )

    # ── Blocklist management ──────────────────────────────────────────────
    block_group = parser.add_argument_group("blocklist management")
    block_group.add_argument(
        "--block",
        metavar="URL",
        help="Block a URL: removes it from state and prevents future inclusion",
    )
    block_group.add_argument(
        "--block-source",
        metavar="SOURCE",
        help="Block all articles from a source (e.g. 'Daily Express')",
    )
    block_group.add_argument(
        "--block-pattern",
        metavar="PHRASE",
        help="Block articles whose title contains this phrase (case-insensitive)",
    )
    block_group.add_argument(
        "--unblock",
        metavar="ENTRY",
        help="Remove a URL, source, or pattern from the blocklist",
    )
    block_group.add_argument(
        "--show-blocklist",
        action="store_true",
        help="Display the current blocklist and exit",
    )
    args = parser.parse_args()

    # ── Handle blocklist commands (run-and-exit) ──────────────────────────
    blocklist = load_blocklist()

    if args.show_blocklist:
        if not any(
            [blocklist["urls"], blocklist["sources"], blocklist["title_patterns"]]
        ):
            print("Blocklist is empty.")
        else:
            if blocklist["urls"]:
                print(f"Blocked URLs ({len(blocklist['urls'])}):")
                for u in sorted(blocklist["urls"]):
                    print(f"  • {u}")
            if blocklist["sources"]:
                print(f"Blocked sources ({len(blocklist['sources'])}):")
                for s in sorted(blocklist["sources"]):
                    print(f"  • {s}")
            if blocklist["title_patterns"]:
                print(f"Blocked title patterns ({len(blocklist['title_patterns'])}):")
                for p in blocklist["title_patterns"]:
                    print(f'  • "{p}"')
        return

    if args.block:
        blocklist["urls"].add(args.block)
        save_blocklist(blocklist)
        print(f"✓ Blocked URL: {args.block}")

    if args.block_source:
        blocklist["sources"].add(args.block_source.lower())
        save_blocklist(blocklist)
        print(f"✓ Blocked source: {args.block_source}")

    if args.block_pattern:
        blocklist["title_patterns"].append(args.block_pattern.lower())
        save_blocklist(blocklist)
        print(f'✓ Blocked title pattern: "{args.block_pattern}"')

    # If any block command was given, purge from state + regenerate text files
    if args.block or args.block_source or args.block_pattern:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
            removed = purge_blocked_from_state(state, blocklist)
            if removed:
                with open(STATE_FILE, "w", encoding="utf-8") as f:
                    json.dump(state, f, indent=2, ensure_ascii=False)
                print(f"  Purged {removed} matching item(s) from state.")
                # Regenerate text files so feeds/ stays in sync
                timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                os.makedirs(args.outdir, exist_ok=True)
                for feed in FEEDS:
                    fid = feed["id"]
                    items = state.get(fid, [])
                    text = format_single_feed(feed, items, timestamp)
                    filename = feed.get("filename", f"{fid}.txt")
                    filepath = os.path.join(args.outdir, filename)
                    with open(filepath, "w", encoding="utf-8") as f:
                        f.write(text + "\n")
                print(f"  Regenerated {len(FEEDS)} text file(s) in {args.outdir}/")
            else:
                print("  No matching items found in state.")
        return

    if args.unblock:
        entry = args.unblock
        found = False
        if entry in blocklist["urls"]:
            blocklist["urls"].discard(entry)
            found = True
        if entry.lower() in blocklist["sources"]:
            blocklist["sources"].discard(entry.lower())
            found = True
        if entry.lower() in blocklist["title_patterns"]:
            blocklist["title_patterns"].remove(entry.lower())
            found = True
        if found:
            save_blocklist(blocklist)
            print(f"✓ Unblocked: {entry}")
        else:
            print(f"Not found in blocklist: {entry}")
        return

    # ── Rebuild: regenerate text files from state without fetching ──────
    if args.rebuild:
        if not os.path.exists(STATE_FILE):
            print(f"Error: {STATE_FILE} not found.", file=sys.stderr)
            sys.exit(1)
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        # Purge any blocklisted items before writing
        purged = purge_blocked_from_state(state, blocklist)
        if purged:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, ensure_ascii=False)
            print(
                f"  ✗ Purged {purged} blocklisted item(s) from state.", file=sys.stderr
            )
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        os.makedirs(args.outdir, exist_ok=True)
        for feed in FEEDS:
            fid = feed["id"]
            items = state.get(fid, [])
            text = format_single_feed(feed, items, timestamp)
            filename = feed.get("filename", f"{fid}.txt")
            filepath = os.path.join(args.outdir, filename)
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(text + "\n")
            print(f"  ✓ Saved {filename} ({len(items)} items)", file=sys.stderr)
        print(f"\nRebuilt {len(FEEDS)} text file(s) in {args.outdir}/", file=sys.stderr)
        return

    # ── Normal monitor run ────────────────────────────────────────────────

    # Create output directory if it doesn't exist
    os.makedirs(args.outdir, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    active_feeds = FEEDS
    if args.feeds:
        active_feeds = [f for f in FEEDS if f["id"] in args.feeds]

    # Load the database (if it exists)
    state = {}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
        except Exception:
            pass

    for feed in FEEDS:
        if feed["id"] not in state:
            state[feed["id"]] = []

    # Purge any previously-collected items that are now blocklisted
    purged = purge_blocked_from_state(state, blocklist)
    if purged:
        print(f"  ✗ Purged {purged} blocklisted item(s) from state.", file=sys.stderr)

    # Archive old articles to JSONL and prune them from state
    archived = prune_and_archive(state, args.archive_days)
    if archived:
        print(
            f"  ↳ Archived {archived} article(s) older than {args.archive_days} days"
            f" → {ARCHIVE_FILE}",
            file=sys.stderr,
        )

    print(
        f"[{timestamp}] Fetching {len(active_feeds)} feeds for new articles…",
        file=sys.stderr,
    )

    for feed in active_feeds:
        fid = feed["id"]
        print(f"  → {CATEGORY_LABELS.get(fid, fid)}…", file=sys.stderr)

        # Create a private memory pool just for this specific category
        category_seen_urls = {item["url"] for item in state.get(fid, [])}

        # Pass that private memory to the fetcher
        new_items = fetch_feed(feed, category_seen_urls, timestamp, blocklist)

        if new_items:
            print(f"    + Found {len(new_items)} new articles!", file=sys.stderr)
            # Add new items to the category's list
            state[fid] = new_items + state[fid]

        # ---------------------------------------------------------
        # NEW FIX: Sort everything chronologically, newest first!
        # ---------------------------------------------------------
        state[fid].sort(key=get_sort_time, reverse=True)

    # Save the updated, strictly sorted database back to JSON
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

    # Rewrite each feed into its own text file
    print("\n[Writing files...]", file=sys.stderr)
    for feed in active_feeds:
        fid = feed["id"]
        items = state.get(fid, [])
        text = format_single_feed(feed, items, timestamp)

        filename = feed.get("filename", f"{fid}.txt")
        filepath = os.path.join(args.outdir, filename)

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"  ✓ Saved {filename}", file=sys.stderr)


if __name__ == "__main__":
    main()
