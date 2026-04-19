#!/usr/bin/env python3
"""
Article Scraper — enriches monitor data with article text extracts.

Reads monitor_state.json, resolves Google News redirect URLs, extracts
article text via trafilatura, and writes dated enriched JSON files.

Usage:
    python article_scraper.py                    # last 24 hours
    python article_scraper.py --hours 48         # wider window
    python article_scraper.py --category frp     # single category
"""
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
    print("Install dependencies: pip install trafilatura googlenewsdecoder")
    sys.exit(1)

STATE_FILE = "monitor_state.json"

CATEGORY_LABELS = {
    "maga": "🇺🇸 MAGA / Trump",
    "frp": "🇳🇴 Fremskrittspartiet",
    "sd": "🇸🇪 Sverigedemokraterna",
    "rn": "🇫🇷 Rassemblement National",
    "fdi": "🇮🇹 Fratelli d'Italia / Lega",
    "reform": "🇬🇧 Reform UK",
    "afd": "🇩🇪 Alternative für Deutschland",
    "general": "🌍 General Right-Wing",
    "nodes": "🕸️ Transnational Networks",
    "hungary": "🇭🇺 Hungary (Fidesz / Tisza)",
    "poland": "🇵🇱 Prawo i Sprawiedliwość",
    "spain": "🇪🇸 Vox",
}

# Domains known to fail — skip to save time
SKIP_DOMAINS = {
    "www.facebook.com",
    "facebook.com",
    "www.instagram.com",
    "instagram.com",
    "twitter.com",
    "x.com",
    "www.tiktok.com",
    "tiktok.com",
    "www.youtube.com",
    "youtube.com",
    "www.msn.com",        # aggregator shell pages
    "msn.com",
}

# Max words to keep per article extract
MAX_EXTRACT_WORDS = 300

# Delay between requests (seconds) — be polite
REQUEST_DELAY = 1.0


# ── Helpers ────────────────────────────────────────────────────────────────

def get_sort_time(item: dict) -> datetime:
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


# ── Step 1: Resolve Google News redirect URLs ─────────────────────────────

def resolve_url(google_url: str) -> tuple[str | None, str]:
    """Decode a Google News URL. Returns (resolved_url, status)."""
    try:
        decoded = new_decoderv1(google_url, interval=0.5)
        if decoded.get("status") and decoded.get("decoded_url"):
            return decoded["decoded_url"], "ok"
        return None, "decode_failed"
    except Exception as e:
        return None, f"error: {e}"


# ── Step 2: Extract article text ──────────────────────────────────────────

def extract_article(url: str) -> tuple[str | None, int, str]:
    """Fetch and extract article text. Returns (extract, word_count, status)."""
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return None, 0, "fetch_failed"

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


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Article Scraper")
    parser.add_argument(
        "--hours", type=int, default=24,
        help="Look-back window in hours (default: 24)"
    )
    parser.add_argument(
        "--outdir", default="enriched",
        help="Output directory (default: enriched/)"
    )
    parser.add_argument(
        "--category", default=None,
        help="Scrape only a specific category (e.g., 'frp')"
    )
    args = parser.parse_args()

    if not os.path.exists(STATE_FILE):
        print(f"Error: {STATE_FILE} not found.")
        sys.exit(1)

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)

    cutoff = datetime.now(timezone.utc) - timedelta(hours=args.hours)
    os.makedirs(args.outdir, exist_ok=True)

    # Collect articles to scrape
    articles = []
    for cat_id, items in state.items():
        if args.category and cat_id != args.category:
            continue
        for item in items:
            if get_sort_time(item) >= cutoff:
                articles.append({**item, "_category": cat_id})

    if not articles:
        print(f"No articles found in the last {args.hours} hours.")
        sys.exit(0)

    date_slug = datetime.now().strftime("%Y-%m-%d")
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cat_count = len(set(a["_category"] for a in articles))

    print(f"[{timestamp}] Scraping {len(articles)} articles across {cat_count} categories")
    print(f"  Look-back: {args.hours}h | Output: {args.outdir}/enriched_{date_slug}.json")
    print()

    # ── Process each article ──────────────────────────────────────────────
    enriched = []
    stats = {"total": len(articles), "resolved": 0, "extracted": 0,
             "skipped": 0, "failed_resolve": 0, "failed_extract": 0}
    ref_num = 0

    for i, item in enumerate(articles, 1):
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
        progress = f"[{i}/{len(articles)}]"
        print(f"  {progress} {cat_label} | {source} | {title[:55]}...")

        # Step 1: Resolve URL
        resolved_url, resolve_status = resolve_url(google_url)

        if resolve_status != "ok" or not resolved_url:
            record["extract_status"] = f"resolve_{resolve_status}"
            stats["failed_resolve"] += 1
            print(f"         ✗ Redirect failed: {resolve_status}")
            enriched.append(record)
            continue

        record["resolved_url"] = resolved_url
        stats["resolved"] += 1

        # Check for known-bad domains
        domain = urlparse(resolved_url).netloc.lower()
        if domain in SKIP_DOMAINS:
            record["extract_status"] = "skipped_domain"
            stats["skipped"] += 1
            print(f"         ⊘ Skipped ({domain})")
            enriched.append(record)
            continue

        # Step 2: Extract article text
        extract, word_count, extract_status = extract_article(resolved_url)

        record["extract"] = extract
        record["word_count"] = word_count
        record["extract_status"] = extract_status

        if extract_status == "ok":
            stats["extracted"] += 1
            print(f"         ✓ {word_count} words ({domain})")
        else:
            stats["failed_extract"] += 1
            print(f"         ✗ {extract_status} ({domain})")

        enriched.append(record)
        time.sleep(REQUEST_DELAY)

    # ── Write output ──────────────────────────────────────────────────────
    output = {
        "generated_at": timestamp,
        "look_back_hours": args.hours,
        "stats": stats,
        "articles": enriched,
    }

    outpath = os.path.join(args.outdir, f"enriched_{date_slug}.json")
    with open(outpath, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    # ── Summary ───────────────────────────────────────────────────────────
    total = stats["total"]
    print()
    print("=" * 60)
    print("SCRAPER SUMMARY")
    print("=" * 60)
    print(f"  Total articles:    {total}")
    print(f"  URLs resolved:     {stats['resolved']}/{total} ({100*stats['resolved']//total}%)")
    print(f"  Text extracted:    {stats['extracted']}/{total} ({100*stats['extracted']//total}%)")
    print(f"  Domains skipped:   {stats['skipped']}")
    print(f"  Failed (resolve):  {stats['failed_resolve']}")
    print(f"  Failed (extract):  {stats['failed_extract']}")
    print(f"\n  ✓ Saved → {outpath}")
    print()


if __name__ == "__main__":
    main()
