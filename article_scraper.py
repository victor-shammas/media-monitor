#!/usr/bin/env python3
“””
Article Scraper — Enriches monitor data with article text extracts.

Reads the RSS metadata from monitor_state.json, resolves Google News redirect
URLs, extracts the first 300 words of article text via trafilatura, and writes dated enriched
JSON files for the downstream AI Reporter.

Designed to be run multiple times per day. Each run accumulates into a single
daily file (enriched_YYYY-MM-DD.json), merging new articles without duplicates.
Already-enriched articles are skipped automatically.

Usage Examples:
python article_scraper.py              # Scrape articles from the last 24 hours
python article_scraper.py –hours 48   # Expand window to the last 48 hours
python article_scraper.py –category frp   # Scrape only a single specific category

Flags:
–hours INT       Look-back window in hours to scrape (default: 24)
–outdir DIR      Output directory for the enriched JSON files (default: enriched/)
–category ID     Scrape only a specific feed ID (e.g., ‘frp’, ‘maga’)
“””

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

try:
import trafilatura
from googlenewsdecoder import new_decoderv1
except ImportError:
print(“Install dependencies: pip install trafilatura googlenewsdecoder”)
sys.exit(1)

STATE_FILE = “monitor_state.json”

CATEGORY_LABELS = {
“maga”: “🇺🇸 MAGA / Trump”,
“frp”: “🇳🇴 Fremskrittspartiet”,
“sd”: “🇸🇪 Sverigedemokraterna”,
“rn”: “🇫🇷 Rassemblement National”,
“fdi”: “🇮🇹 Fratelli d’Italia / Lega”,
“reform”: “🇬🇧 Reform UK”,
“afd”: “🇩🇪 Alternative für Deutschland”,
“general”: “🌍 General Right-Wing”,
“nodes”: “🕸️ Transnational Networks”,
“hungary”: “🇭🇺 Hungary (Fidesz / Tisza)”,
“poland”: “🇵🇱 Prawo i Sprawiedliwość”,
“spain”: “🇪🇸 Vox”,
}

# Domains known to fail — skip to save time

SKIP_DOMAINS = {
“www.facebook.com”,
“facebook.com”,
“www.instagram.com”,
“instagram.com”,
“twitter.com”,
“x.com”,
“www.tiktok.com”,
“tiktok.com”,
“www.youtube.com”,
“youtube.com”,
“www.msn.com”,  # aggregator shell pages
“msn.com”,
}

# Max words to keep per article extract

MAX_EXTRACT_WORDS = 300

# Delay between requests (seconds) — be polite

REQUEST_DELAY = 1.0

# ── Helpers ────────────────────────────────────────────────────────────────

def get_sort_time(item: dict) -> datetime:
date_str = item.get(“date”, “”)
try:
return datetime.fromisoformat(date_str.replace(“Z”, “+00:00”))
except Exception:
added_str = item.get(“added_at”, “”)
try:
return datetime.strptime(added_str, “%Y-%m-%d %H:%M:%S”).replace(
tzinfo=timezone.utc
)
except Exception:
return datetime.min.replace(tzinfo=timezone.utc)

# ── Step 1: Resolve Google News redirect URLs ─────────────────────────────

def resolve_url(google_url: str) -> tuple[str | None, str]:
“”“Decode a Google News URL. Returns (resolved_url, status).”””
try:
decoded = new_decoderv1(google_url, interval=0.5)
if decoded.get(“status”) and decoded.get(“decoded_url”):
return decoded[“decoded_url”], “ok”
return None, “decode_failed”
except Exception as e:
return None, f”error: {e}”

# ── Step 2: Extract article text ──────────────────────────────────────────

def extract_article(url: str) -> tuple[str | None, int, str]:
“”“Fetch and extract article text. Returns (extract, word_count, status).”””
try:
downloaded = trafilatura.fetch_url(url)
if not downloaded:
return None, 0, “fetch_failed”

```
    text = trafilatura.extract(
        downloaded,
        include_comments=False,
        include_tables=False,
        no_fallback=False,
    )
    if not text or len(text.strip()) < 50:
        return text, len(text.split()) if text else 0, "too_short"

    words = text.split()
    truncated = " ".join(words[:MAX_EXTRACT_WORDS])
    return truncated, len(words), "ok"

except Exception as e:
    return None, 0, f"error: {e}"
```

# ── Daily file management ────────────────────────────────────────────────

def load_existing_enriched(path: str) -> tuple[list[dict], set[str]]:
“”“Load an existing enriched file. Returns (articles_list, set_of_google_urls).”””
if not os.path.exists(path):
return [], set()

```
try:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    articles = data.get("articles", [])
    seen_urls = {a.get("google_url", "") for a in articles if a.get("google_url")}
    return articles, seen_urls
except Exception as e:
    print(f"  ⚠ Failed to load existing enriched file: {e}")
    return [], set()
```

# ── Main ──────────────────────────────────────────────────────────────────

def main():
parser = argparse.ArgumentParser(description=“Article Scraper”)
parser.add_argument(
“–hours”, type=int, default=24, help=“Look-back window in hours (default: 24)”
)
parser.add_argument(
“–outdir”, default=“enriched”, help=“Output directory (default: enriched/)”
)
parser.add_argument(
“–category”, default=None, help=“Scrape only a specific category (e.g., ‘frp’)”
)
args = parser.parse_args()

```
if not os.path.exists(STATE_FILE):
    print(f"Error: {STATE_FILE} not found.")
    sys.exit(1)

with open(STATE_FILE, "r", encoding="utf-8") as f:
    state = json.load(f)

cutoff = datetime.now(timezone.utc) - timedelta(hours=args.hours)
os.makedirs(args.outdir, exist_ok=True)

# ── Load existing enriched file for today (if any) ────────────────────
date_slug = datetime.now().strftime("%Y-%m-%d")
outpath = os.path.join(args.outdir, f"enriched_{date_slug}.json")

existing_articles, already_scraped_urls = load_existing_enriched(outpath)

if existing_articles:
    print(
        f"  Loaded existing enriched file with {len(existing_articles)} articles "
        f"({len(already_scraped_urls)} unique URLs)"
    )

# ── Collect candidate articles from state ─────────────────────────────
candidates = []
for cat_id, items in state.items():
    if args.category and cat_id != args.category:
        continue
    for item in items:
        if get_sort_time(item) >= cutoff:
            google_url = item.get("url", "")
            # Skip articles we've already enriched
            if google_url and google_url in already_scraped_urls:
                continue
            candidates.append({**item, "_category": cat_id})

timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

if not candidates:
    total_for_day = len(existing_articles)
    print(
        f"[{timestamp}] No new articles to scrape. "
        f"Enriched file already has {total_for_day} articles."
    )
    sys.exit(0)

cat_count = len(set(a["_category"] for a in candidates))
print(
    f"[{timestamp}] Scraping {len(candidates)} new articles across {cat_count} categories"
)
print(
    f"  Look-back: {args.hours}h | Output: {outpath}"
)
if existing_articles:
    print(
        f"  Merging with {len(existing_articles)} previously enriched articles"
    )
print()

# ── Determine starting ref number ─────────────────────────────────────
# Continue numbering from where the existing file left off
if existing_articles:
    max_existing_ref = max(a.get("ref", 0) for a in existing_articles)
else:
    max_existing_ref = 0
ref_num = max_existing_ref

# ── Process each new article ──────────────────────────────────────────

new_articles = []
stats = {
    "total": len(candidates),
    "resolved": 0,
    "extracted": 0,
    "skipped": 0,
    "failed_resolve": 0,
    "failed_extract": 0,
}

for i, item in enumerate(candidates, 1):
    ref_num += 1
    cat_id = item["_category"]
    cat_label = CATEGORY_LABELS.get(cat_id, cat_id)
    title = item.get("title", "")
    source = item.get("source", "")
    google_url = item.get("url", "")

    record = {
        "ref": ref_num,
        "title": title,
        "source": source,
        "google_url": google_url,
        "resolved_url": None,
        "date": item.get("date", ""),
        "category": cat_id,
        "extract": None,
        "extract_status": "pending",
        "word_count": 0,
    }

    # Progress indicator
    progress = f"[{i}/{len(candidates)}]"
    print(f"  {progress} {cat_label} | {source} | {title[:55]}...")

    # Step 1: Resolve URL
    resolved_url, resolve_status = resolve_url(google_url)

    if resolve_status != "ok" or not resolved_url:
        record["extract_status"] = f"resolve_{resolve_status}"
        stats["failed_resolve"] += 1
        print(f"      ✗ Redirect failed: {resolve_status}")
        new_articles.append(record)
        continue

    record["resolved_url"] = resolved_url
    stats["resolved"] += 1

    # Check for known-bad domains
    domain = urlparse(resolved_url).netloc.lower()
    if domain in SKIP_DOMAINS:
        record["extract_status"] = "skipped_domain"
        stats["skipped"] += 1
        print(f"      ⊘ Skipped ({domain})")
        new_articles.append(record)
        continue

    # Step 2: Extract article text
    extract, word_count, extract_status = extract_article(resolved_url)
    record["extract"] = extract
    record["word_count"] = word_count
    record["extract_status"] = extract_status

    if extract_status == "ok":
        stats["extracted"] += 1
        print(f"      ✓ {word_count} words ({domain})")
    else:
        stats["failed_extract"] += 1
        print(f"      ✗ {extract_status} ({domain})")

    new_articles.append(record)
    time.sleep(REQUEST_DELAY)

# ── Merge and write output ────────────────────────────────────────────

merged_articles = existing_articles + new_articles

output = {
    "date": date_slug,
    "last_updated": timestamp,
    "stats": {
        "total_articles": len(merged_articles),
        "this_run": stats,
    },
    "articles": merged_articles,
}

with open(outpath, "w", encoding="utf-8") as f:
    json.dump(output, f, indent=2, ensure_ascii=False)

# ── Summary ───────────────────────────────────────────────────────────

total_new = stats["total"]
print()
print("=" * 60)
print("SCRAPER SUMMARY")
print("=" * 60)
print(f"  New articles processed: {total_new}")
print(
    f"  URLs resolved:   {stats['resolved']}/{total_new} ({100 * stats['resolved'] // max(total_new, 1)}%)"
)
print(
    f"  Text extracted:  {stats['extracted']}/{total_new} ({100 * stats['extracted'] // max(total_new, 1)}%)"
)
print(f"  Domains skipped: {stats['skipped']}")
print(f"  Failed (resolve): {stats['failed_resolve']}")
print(f"  Failed (extract):  {stats['failed_extract']}")
print(f"  ─────────────────────────────")
print(f"  Total in enriched file: {len(merged_articles)}")
if existing_articles:
    print(f"    (was {len(existing_articles)}, added {len(new_articles)})")
print(f"\n  ✓ Saved → {outpath}")
print()
```

if **name** == “**main**”:
main()
