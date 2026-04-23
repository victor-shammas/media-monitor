#!/usr/bin/env python3
"""
Transatlantic Right-Wing Media Monitor — Unified Pipeline

Fetches Google News RSS feeds and optionally enriches articles with text
extraction and AI-generated summaries. Maintains a database of seen articles
in monitor_state.json and generates per-category text files in feeds/.

Usage Examples:
  python media-monitor.py                               → Fetch all feeds (default)
  python media-monitor.py --enrich                      → Fetch + enrich + summarize
  python media-monitor.py --feeds norway usa             → Fetch specific feeds only
  python media-monitor.py --rebuild                     → Regenerate text files from state
  python media-monitor.py --enrich --enrich-hours 48    → Enrich with wider lookback

Enrichment Flags:
  --enrich                  After fetching, resolve URLs, extract text, generate AI summaries
  --fetch-only              Only fetch RSS feeds (this is the default)
  --enrich-hours N          Look-back window for enrichment (default: 24)
  --enrich-outdir DIR       Output directory for enriched JSON (default: data-private/)

Blocklist Management:
  --block URL               Block a single article by URL
  --block-source SOURCE     Block all articles from a named source
  --block-pattern PHRASE    Block titles containing a phrase
  --unblock ENTRY           Remove an entry from the blocklist
  --show-blocklist          Print the current blocklist and exit
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

from monitor_utils import (
    CONFIG,
    CATEGORY_LABELS,
    STATE_FILE,
    BLOCKLIST_FILE,
    get_sort_time,
    load_blocklist,
    git_sync,
    normalize_title_for_dedup,
)

# ── Configuration ──────────────────────────────────────────────────────────

ARCHIVE_FILE = "data/archive.jsonl"
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


# ── Blocklist ─────────────────────────────────────────────────────────────


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
    feed: dict, category_seen_urls: set, category_seen_titles: set, timestamp: str, blocklist: dict | None = None
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
                    title = clean_title(item.get("title", ""))

                    title_key = normalize_title_for_dedup(title)
                    if title_key and title_key in category_seen_titles:
                        continue
                    category_seen_titles.add(title_key)

                    candidate = {
                        "title": title,
                        "url": link,
                        "source": source,
                        "date": item.get("pubDate", ""),
                        "added_at": timestamp,
                        "summary": "",
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

        if item.get("summary"):
            lines.append(f"   [AI Summary: {item['summary']}]")
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
    parser.add_argument(
        "--dedup-titles",
        action="store_true",
        help="Remove duplicate articles with near-identical titles from state and exit",
    )

    # ── Enrichment flags ─────────────────────────────────────────────────
    enrich_group = parser.add_argument_group("enrichment (scraper + summaries)")
    enrich_group.add_argument(
        "--enrich",
        action="store_true",
        help="After fetching, enrich articles (resolve URLs, extract text, generate AI summaries)",
    )
    enrich_group.add_argument(
        "--fetch-only",
        action="store_true",
        help="Only fetch RSS feeds, do not run enrichment (this is the default)",
    )
    enrich_group.add_argument(
        "--enrich-hours",
        type=int,
        default=24,
        help="Look-back window in hours for enrichment (default: 24)",
    )
    enrich_group.add_argument(
        "--enrich-outdir",
        default="data-private",
        help="Output directory for enriched JSON files (default: data-private/)",
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

    git_sync()

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

    # ── Dedup titles: one-time cleanup of existing state ────────────────
    if args.dedup_titles:
        if not os.path.exists(STATE_FILE):
            print(f"Error: {STATE_FILE} not found.", file=sys.stderr)
            sys.exit(1)
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)

        total_removed = 0
        for fid in state:
            seen = {}
            deduped = []
            for item in state[fid]:
                item["title"] = clean_title(item.get("title", ""))
                key = normalize_title_for_dedup(item["title"])
                if key in seen:
                    existing_idx = seen[key]
                    if not deduped[existing_idx].get("summary") and item.get("summary"):
                        deduped[existing_idx] = item
                    total_removed += 1
                else:
                    seen[key] = len(deduped)
                    deduped.append(item)
            state[fid] = deduped

        if total_removed:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, ensure_ascii=False)
            print(f"Removed {total_removed} duplicate article(s) from state.", file=sys.stderr)
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
            print(f"Regenerated {len(FEEDS)} feed file(s).", file=sys.stderr)
        else:
            print("No duplicate titles found.", file=sys.stderr)
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
        category_seen_titles = {
            normalize_title_for_dedup(item["title"])
            for item in state.get(fid, [])
        }

        # Pass that private memory to the fetcher
        new_items = fetch_feed(feed, category_seen_urls, category_seen_titles, timestamp, blocklist)

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

    # ── Optional enrichment pass ─────────────────────────────────────────
    if args.enrich and not args.fetch_only:
        print("\n[Enriching articles...]", file=sys.stderr)
        from article_scraper import run_scraper

        category = None
        if args.feeds and len(args.feeds) == 1:
            category = args.feeds[0]
        run_scraper(
            hours=args.enrich_hours,
            outdir=args.enrich_outdir,
            category=category,
        )


if __name__ == "__main__":
    main()
