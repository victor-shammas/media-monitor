#!/usr/bin/env python3
"""
One-off script: generate AI summaries for articles in a specific enriched file.

Reads an enriched JSON file, finds articles with extracts but no summaries,
sends them to Gemini Flash (→ Mistral fallback), then writes summaries back
to both the enriched file and monitor_state.json, and rebuilds feed .txt files.

Usage:
    python backfill_enriched_summaries.py enriched_2026-04-17.json
    python backfill_enriched_summaries.py enriched_2026-04-17.json --dry-run
"""

import argparse
import json
import os
import sys
import subprocess

from article_scraper import generate_summaries, STATE_FILE


def main():
    parser = argparse.ArgumentParser(
        description="Backfill summaries from a specific enriched file"
    )
    parser.add_argument("enriched_file", help="Path to enriched JSON file")
    parser.add_argument(
        "--dry-run", action="store_true", help="Show what would be done without calling the LLM"
    )
    args = parser.parse_args()

    if not os.path.exists(args.enriched_file):
        print(f"Error: {args.enriched_file} not found")
        sys.exit(1)

    if not os.environ.get("GEMINI_API_KEY") and not os.environ.get("MISTRAL_API_KEY"):
        print("Error: Set at least one of GEMINI_API_KEY or MISTRAL_API_KEY.")
        sys.exit(1)

    with open(args.enriched_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    articles = data.get("articles", [])
    need_summary = [
        a for a in articles
        if a.get("extract_status") == "ok" and a.get("extract") and not a.get("summary")
    ]

    print(f"Total articles: {len(articles)}")
    print(f"Need summaries: {len(need_summary)}")

    if not need_summary:
        print("Nothing to do — all extracted articles already have summaries.")
        return

    if args.dry_run:
        for a in need_summary:
            print(f"  [{a.get('category', '?')}] {a['title'][:70]}")
        print(f"\nDry run: would send {len(need_summary)} articles to LLM.")
        return

    # Step 1: Generate summaries via Gemini/Mistral
    summary_count = generate_summaries(need_summary)

    if not summary_count:
        print("\nNo summaries generated.")
        return

    # Step 2: Write summaries back to the enriched file
    summary_lookup = {
        a["google_url"]: a["summary"]
        for a in need_summary if a.get("summary")
    }

    for a in articles:
        if a["google_url"] in summary_lookup:
            a["summary"] = summary_lookup[a["google_url"]]

    from datetime import datetime, timezone
    data["last_updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    with open(args.enriched_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"\nWrote {len(summary_lookup)} summaries to {args.enriched_file}")

    # Step 3: Update monitor_state.json
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)

    updated = 0
    for a in need_summary:
        if not a.get("summary"):
            continue
        cat = a.get("category", "")
        google_url = a.get("google_url", "")
        for item in state.get(cat, []):
            if item.get("url") == google_url:
                item["summary"] = a["summary"]
                updated += 1
                break

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    print(f"Updated {updated} entries in {STATE_FILE}")

    # Step 4: Rebuild feed .txt files
    print("Rebuilding feed text files...")
    subprocess.run(
        [sys.executable, "media-monitor.py", "--rebuild"],
        cwd=os.path.dirname(os.path.abspath(__file__)),
    )

    print(f"\nDone. {summary_count} summaries generated, {updated} state entries updated.")


if __name__ == "__main__":
    main()
