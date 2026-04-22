#!/usr/bin/env python3
"""
One-off script: generate AI summaries for the first N articles per feed.

Reuses existing extracts from enriched JSON files where available,
scrapes and extracts text for the rest, then sends all to Gemini Flash.
Updates monitor_state.json and rebuilds feed .txt files.

Usage:
    python backfill_summaries.py             # 20 per feed (default)
    python backfill_summaries.py --per-feed 10
    python backfill_summaries.py --category sweden --per-feed 30
"""

import argparse
import glob
import json
import os
import signal
import sys
import time

SCRAPE_TIMEOUT = 20


class ScrapeTimeout(Exception):
    pass


def _timeout_handler(signum, frame):
    raise ScrapeTimeout()

from article_scraper import (
    article_date_slug,
    extract_article,
    generate_summaries,
    load_existing_enriched,
    resolve_url,
    SKIP_DOMAINS,
    STATE_FILE,
)
from urllib.parse import urlparse


def load_extract_cache(enriched_dir: str) -> dict[str, str]:
    """Build a url→extract lookup from all enriched JSON files."""
    cache = {}
    for path in glob.glob(os.path.join(enriched_dir, "enriched_*.json")):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for a in data.get("articles", []):
            if a.get("extract_status") == "ok" and a.get("extract"):
                cache[a["google_url"]] = a["extract"]
    return cache


def _save_to_enriched(records: list[dict], enriched_dir: str):
    """Merge records into the per-day enriched JSON files."""
    from datetime import datetime, timezone

    by_date: dict[str, list[dict]] = {}
    for rec in records:
        ds = article_date_slug(rec)
        by_date.setdefault(ds, []).append(rec)

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    saved = 0
    for ds, new_recs in by_date.items():
        existing = load_existing_enriched(enriched_dir, ds)
        if existing:
            merged = list(existing.get("articles", []))
            existing_urls = {a["google_url"] for a in merged}
            for r in new_recs:
                if r["google_url"] in existing_urls:
                    for i, a in enumerate(merged):
                        if a["google_url"] == r["google_url"]:
                            merged[i] = r
                            break
                else:
                    merged.append(r)
            generated_at = existing.get("generated_at", timestamp)
        else:
            merged = list(new_recs)
            generated_at = timestamp

        for idx, a in enumerate(merged, 1):
            a["ref"] = idx

        output = {
            "generated_at": generated_at,
            "last_updated_at": timestamp,
            "stats": {
                "total": len(merged),
                "resolved": sum(1 for a in merged if a.get("resolved_url")),
                "extracted": sum(1 for a in merged if a.get("extract_status") == "ok"),
            },
            "articles": merged,
        }

        outpath = os.path.join(enriched_dir, f"enriched_{ds}.json")
        with open(outpath, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, ensure_ascii=False)
        saved += len(new_recs)

    print(f"  Saved {saved} records to enriched JSON files")


def main():
    parser = argparse.ArgumentParser(description="Backfill AI summaries")
    parser.add_argument(
        "--per-feed", type=int, default=20, help="Articles per feed (default: 20)"
    )
    parser.add_argument(
        "--category", default=None, help="Only backfill a specific category"
    )
    parser.add_argument(
        "--enriched-dir", default="data-private", help="Enriched JSON directory"
    )
    args = parser.parse_args()

    if not os.environ.get("GEMINI_API_KEY") and not os.environ.get("GROQ_API_KEY"):
        print("Error: Set at least one of GEMINI_API_KEY or GROQ_API_KEY.")
        sys.exit(1)

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)

    print("Loading extract cache from enriched files...")
    cache = load_extract_cache(args.enriched_dir)
    print(f"  {len(cache)} cached extracts available\n")

    records = []
    scrape_needed = []

    for cat, items in state.items():
        if args.category and cat != args.category:
            continue

        count = 0
        for item in items:
            if count >= args.per_feed:
                break
            if item.get("summary"):
                count += 1
                continue

            url = item.get("url", "")
            rec = {
                "ref": 0,
                "title": item.get("title", ""),
                "source": item.get("source", ""),
                "google_url": url,
                "resolved_url": None,
                "date": item.get("date", ""),
                "category": cat,
                "extract": None,
                "extract_status": "pending",
                "word_count": 0,
                "summary": None,
            }

            if url in cache:
                rec["extract"] = cache[url]
                rec["extract_status"] = "ok"
                records.append(rec)
            else:
                scrape_needed.append(rec)
                records.append(rec)

            count += 1

    if not records:
        print("Nothing to backfill — all top articles already have summaries.")
        return

    cached_count = sum(1 for r in records if r["extract_status"] == "ok")
    print(f"Selected {len(records)} articles across feeds")
    print(f"  From cache: {cached_count}")
    print(f"  Need scraping: {len(scrape_needed)}\n")

    for i, rec in enumerate(scrape_needed, 1):
        title = rec["title"][:55]
        print(f"  [{i}/{len(scrape_needed)}] {title}...")

        signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(SCRAPE_TIMEOUT)
        try:
            resolved_url, resolve_status = resolve_url(rec["google_url"])
            if resolve_status != "ok" or not resolved_url:
                rec["extract_status"] = f"resolve_{resolve_status}"
                print(f"    ✗ Resolve failed: {resolve_status}")
                time.sleep(1)
                continue

            rec["resolved_url"] = resolved_url

            domain = urlparse(resolved_url).netloc.lower()
            if domain in SKIP_DOMAINS:
                rec["extract_status"] = "skipped_domain"
                print(f"    ⊘ Skipped ({domain})")
                time.sleep(1)
                continue

            extract, word_count, extract_status = extract_article(resolved_url)
            rec["extract"] = extract
            rec["extract_status"] = extract_status
            rec["word_count"] = word_count

            if extract_status == "ok":
                print(f"    ✓ {word_count} words ({domain})")
            else:
                print(f"    ✗ {extract_status} ({domain})")
        except ScrapeTimeout:
            rec["extract_status"] = "timeout"
            print(f"    ✗ Timed out after {SCRAPE_TIMEOUT}s")
        finally:
            signal.alarm(0)

        time.sleep(1)

    scraped = [r for r in records if r["extract_status"] != "pending" and r["google_url"] not in cache]
    if scraped:
        _save_to_enriched(scraped, args.enriched_dir)

    summary_count = generate_summaries(records)

    if summary_count:
        _save_to_enriched(
            [r for r in records if r.get("summary")], args.enriched_dir
        )

    if not summary_count:
        print("\nNo summaries generated.")
        return

    summary_records = [r for r in records if r.get("summary")]
    updated = 0
    for rec in summary_records:
        cat = rec["category"]
        google_url = rec["google_url"]
        for item in state.get(cat, []):
            if item.get("url") == google_url:
                item["summary"] = rec["summary"]
                updated += 1
                break

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

    print(f"\nWrote {updated} summaries to {STATE_FILE}")

    import subprocess

    print("Rebuilding feed text files...")
    subprocess.run(
        [sys.executable, "media-monitor.py", "--rebuild"],
        cwd=os.path.dirname(os.path.abspath(__file__)),
    )

    print(f"\nDone. {summary_count} AI summaries generated and written.")


if __name__ == "__main__":
    main()
