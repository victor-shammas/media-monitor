#!/usr/bin/env python3
"""
One-off migration: merge old timestamped enriched files into per-day files.

Reads all enriched_*.json files in data-private/, groups articles by their
publication date, deduplicates by google_url, and writes one
enriched_YYYY-MM-DD.json per day. After verifying the output, delete the
old timestamped files with:  rm data-private/enriched_*_*.json
"""

import glob
import json
import os
import sys
from datetime import datetime

ENRICHED_DIR = "data-private"


def article_date_slug(record: dict) -> str:
    date_str = record.get("date", "")
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return "unknown"


def main():
    pattern = os.path.join(ENRICHED_DIR, "enriched_*.json")
    old_files = sorted(glob.glob(pattern))

    if not old_files:
        print("No enriched files found.")
        sys.exit(0)

    print(f"Found {len(old_files)} enriched file(s) to migrate.\n")

    # Read all articles from all files, dedup by google_url
    seen_urls: set[str] = set()
    by_date: dict[str, list[dict]] = {}
    total_read = 0
    dupes_skipped = 0

    for path in old_files:
        print(f"  Reading {os.path.basename(path)}...")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for article in data.get("articles", []):
            total_read += 1
            url = article.get("google_url", "")
            if url in seen_urls:
                dupes_skipped += 1
                continue
            seen_urls.add(url)
            ds = article_date_slug(article)
            by_date.setdefault(ds, []).append(article)

    print(f"\n  Total articles read:  {total_read}")
    print(f"  Duplicates skipped:   {dupes_skipped}")
    print(f"  Unique articles:      {len(seen_urls)}")
    print(f"  Date buckets:         {len(by_date)}")

    # Write one file per day
    print()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for ds in sorted(by_date.keys()):
        articles = by_date[ds]
        for i, a in enumerate(articles, 1):
            a["ref"] = i

        stats = {
            "total": len(articles),
            "resolved": sum(1 for a in articles if a.get("resolved_url")),
            "extracted": sum(1 for a in articles if a.get("extract_status") == "ok"),
            "skipped": sum(
                1 for a in articles if a.get("extract_status") == "skipped_domain"
            ),
            "blocklisted": sum(
                1
                for a in articles
                if (a.get("extract_status") or "").startswith("blocklisted")
            ),
            "failed_resolve": sum(
                1
                for a in articles
                if (a.get("extract_status") or "").startswith("resolve_")
            ),
            "failed_extract": sum(
                1
                for a in articles
                if a.get("extract_status") in ("fetch_failed", "too_short")
                or (a.get("extract_status") or "").startswith("error")
            ),
        }

        output = {
            "generated_at": timestamp,
            "last_updated_at": timestamp,
            "stats": stats,
            "articles": articles,
        }

        outpath = os.path.join(ENRICHED_DIR, f"enriched_{ds}.json")
        with open(outpath, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, ensure_ascii=False)
        print(f"  ✓ enriched_{ds}.json — {len(articles)} articles")

    print(f"\nMigration complete. Verify the output, then run:")
    print(f"  rm {ENRICHED_DIR}/enriched_*_*.json")


if __name__ == "__main__":
    main()
