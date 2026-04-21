#!/usr/bin/env python3
"""
Article Scraper — Enriches monitor data with article text extracts.

Reads the RSS metadata from monitor_state.json, resolves Google News redirect
URLs, extracts the first 300 words of article text via trafilatura, and writes
one enriched JSON file per day (enriched_YYYY-MM-DD.json). Multiple scraper runs
merge into the same day file, with articles placed by their publication date.
Already-processed articles are skipped on subsequent runs.

Usage Examples:
    python article_scraper.py              # Scrape articles from the last 24 hours
    python article_scraper.py --hours 48   # Expand window to the last 48 hours
    python article_scraper.py --category frp  # Scrape only a single specific category

Flags:
    --hours INT        Look-back window in hours to scrape (default: 24)
    --outdir DIR       Output directory for the enriched JSON files (default: data-private/)
    --category ID      Scrape only a specific feed ID (e.g., 'frp', 'maga')
"""

import argparse
import json
import os
import socket
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

socket.setdefaulttimeout(30)

try:
    import trafilatura
    from googlenewsdecoder import new_decoderv1
except ImportError:
    print("Install dependencies: pip install trafilatura googlenewsdecoder")
    sys.exit(1)

STATE_FILE = "monitor_state.json"
BLOCKLIST_FILE = "blocklist.json"

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
    "www.msn.com",  # aggregator shell pages
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


def load_blocklist() -> dict:
    """Load blocklist.json if it exists. Returns dict with urls, sources, title_patterns."""
    default = {"urls": [], "sources": [], "title_patterns": []}
    if not os.path.exists(BLOCKLIST_FILE):
        return default
    try:
        with open(BLOCKLIST_FILE, "r", encoding="utf-8") as f:
            bl = json.load(f)
        # Normalize source entries to lowercase for matching
        bl.setdefault("urls", [])
        bl.setdefault("sources", [])
        bl.setdefault("title_patterns", [])
        return bl
    except Exception as e:
        print(f"  Warning: could not load {BLOCKLIST_FILE}: {e}")
        return default


def is_blocklisted_source(source: str, blocklist: dict) -> bool:
    """Check if a source name matches any blocklist source entry (case-insensitive substring)."""
    source_lower = source.lower()
    return any(b.lower() in source_lower for b in blocklist.get("sources", []))


def is_blocklisted_title(title: str, blocklist: dict) -> bool:
    """Check if a title matches any blocklist title pattern (case-insensitive substring)."""
    title_lower = title.lower()
    return any(p.lower() in title_lower for p in blocklist.get("title_patterns", []))


def is_blocklisted_url(url: str, blocklist: dict) -> bool:
    """Check if a resolved URL matches any blocklist URL entry (substring match)."""
    return any(u in url for u in blocklist.get("urls", []))


def article_date_slug(record: dict) -> str:
    """Determine which day's file an article belongs to, based on its publication date."""
    date_str = record.get("date", "")
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d")


def load_existing_enriched(outdir: str, date_slug: str) -> dict | None:
    """Load an existing enriched_YYYY-MM-DD.json file if it exists."""
    path = os.path.join(outdir, f"enriched_{date_slug}.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return None


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
        "--hours", type=int, default=24, help="Look-back window in hours (default: 24)"
    )
    parser.add_argument(
        "--outdir", default="data-private", help="Output directory (default: data-private/)"
    )
    parser.add_argument(
        "--category", default=None, help="Scrape only a specific category (e.g., 'frp')"
    )
    args = parser.parse_args()

    if not os.path.exists(STATE_FILE):
        print(f"Error: {STATE_FILE} not found.")
        sys.exit(1)

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)

    # Load blocklist
    blocklist = load_blocklist()
    bl_sources = len(blocklist.get("sources", []))
    bl_patterns = len(blocklist.get("title_patterns", []))
    bl_urls = len(blocklist.get("urls", []))
    if bl_sources or bl_patterns or bl_urls:
        print(
            f"  Blocklist loaded: {bl_sources} sources, {bl_patterns} title patterns, {bl_urls} URLs"
        )

    cutoff = datetime.now(timezone.utc) - timedelta(hours=args.hours)
    os.makedirs(args.outdir, exist_ok=True)

    # Collect candidate articles from state
    candidates = []
    for cat_id, items in state.items():
        if args.category and cat_id != args.category:
            continue
        for item in items:
            if get_sort_time(item) >= cutoff:
                candidates.append({**item, "_category": cat_id})

    if not candidates:
        print(f"No articles found in the last {args.hours} hours.")
        sys.exit(0)

    # Load existing enriched files for relevant dates to skip already-processed articles
    candidate_dates = {article_date_slug(c) for c in candidates}
    already_processed_urls = set()
    existing_by_date = {}
    for ds in candidate_dates:
        data = load_existing_enriched(args.outdir, ds)
        if data:
            existing_by_date[ds] = data
            for a in data.get("articles", []):
                already_processed_urls.add(a.get("google_url", ""))

    articles = [c for c in candidates if c.get("url", "") not in already_processed_urls]

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if not articles:
        print(f"[{timestamp}] All {len(candidates)} articles already processed.")
        sys.exit(0)

    cat_count = len(set(a["_category"] for a in articles))
    print(
        f"[{timestamp}] Scraping {len(articles)} new articles across {cat_count} categories"
    )
    print(f"  ({len(candidates) - len(articles)} already processed, skipped)")
    print(f"  Look-back: {args.hours}h | Output: {args.outdir}/enriched_YYYY-MM-DD.json")
    print()

    # ── Process each article ──────────────────────────────────────────────

    new_by_date: dict[str, list[dict]] = {}
    stats = {
        "total": len(articles),
        "resolved": 0,
        "extracted": 0,
        "skipped": 0,
        "blocklisted": 0,
        "failed_resolve": 0,
        "failed_extract": 0,
    }

    for i, item in enumerate(articles, 1):
        cat_id = item["_category"]
        cat_label = CATEGORY_LABELS.get(cat_id, cat_id)
        title = item.get("title", "")
        source = item.get("source", "")
        google_url = item.get("url", "")

        record = {
            "ref": 0,
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

        progress = f"[{i}/{len(articles)}]"
        print(f"  {progress} {cat_label} | {source} | {title[:55]}...")

        # ── Blocklist: check source name ──────────────────────────────
        if is_blocklisted_source(source, blocklist):
            record["extract_status"] = "blocklisted_source"
            stats["blocklisted"] += 1
            print(f"    ⊘ Blocklisted source ({source})")
            ds = article_date_slug(record)
            new_by_date.setdefault(ds, []).append(record)
            continue

        # ── Blocklist: check title patterns ───────────────────────────
        if is_blocklisted_title(title, blocklist):
            record["extract_status"] = "blocklisted_title"
            stats["blocklisted"] += 1
            print(f"    ⊘ Blocklisted title pattern")
            ds = article_date_slug(record)
            new_by_date.setdefault(ds, []).append(record)
            continue

        # Step 1: Resolve URL
        resolved_url, resolve_status = resolve_url(google_url)

        if resolve_status != "ok" or not resolved_url:
            record["extract_status"] = f"resolve_{resolve_status}"
            stats["failed_resolve"] += 1
            print(f"    ✗ Redirect failed: {resolve_status}")
            ds = article_date_slug(record)
            new_by_date.setdefault(ds, []).append(record)
            continue

        record["resolved_url"] = resolved_url
        stats["resolved"] += 1

        # ── Blocklist: check resolved URL ─────────────────────────────
        if is_blocklisted_url(resolved_url, blocklist):
            record["extract_status"] = "blocklisted_url"
            stats["blocklisted"] += 1
            print(f"    ⊘ Blocklisted URL ({resolved_url})")
            ds = article_date_slug(record)
            new_by_date.setdefault(ds, []).append(record)
            continue

        # Check for known-bad domains
        domain = urlparse(resolved_url).netloc.lower()
        if domain in SKIP_DOMAINS:
            record["extract_status"] = "skipped_domain"
            stats["skipped"] += 1
            print(f"    ⊘ Skipped ({domain})")
            ds = article_date_slug(record)
            new_by_date.setdefault(ds, []).append(record)
            continue

        # Step 2: Extract article text
        extract, word_count, extract_status = extract_article(resolved_url)
        record["extract"] = extract
        record["word_count"] = word_count
        record["extract_status"] = extract_status

        if extract_status == "ok":
            stats["extracted"] += 1
            print(f"    ✓ {word_count} words ({domain})")
        else:
            stats["failed_extract"] += 1
            print(f"    ✗ {extract_status} ({domain})")

        ds = article_date_slug(record)
        new_by_date.setdefault(ds, []).append(record)
        time.sleep(REQUEST_DELAY)

    # ── Merge into per-day files and write ────────────────────────────────

    written_files = []
    for ds in sorted(new_by_date.keys()):
        new_articles = new_by_date[ds]
        existing = existing_by_date.get(ds)

        if existing:
            merged = list(existing.get("articles", []))
            existing_urls_in_file = {a["google_url"] for a in merged}
            for a in new_articles:
                if a["google_url"] not in existing_urls_in_file:
                    merged.append(a)
            generated_at = existing.get("generated_at", timestamp)
        else:
            merged = list(new_articles)
            generated_at = timestamp

        for idx, a in enumerate(merged, 1):
            a["ref"] = idx

        file_stats = {
            "total": len(merged),
            "resolved": sum(1 for a in merged if a.get("resolved_url")),
            "extracted": sum(1 for a in merged if a.get("extract_status") == "ok"),
            "skipped": sum(
                1 for a in merged if a.get("extract_status") == "skipped_domain"
            ),
            "blocklisted": sum(
                1
                for a in merged
                if (a.get("extract_status") or "").startswith("blocklisted")
            ),
            "failed_resolve": sum(
                1
                for a in merged
                if (a.get("extract_status") or "").startswith("resolve_")
            ),
            "failed_extract": sum(
                1
                for a in merged
                if a.get("extract_status")
                in ("fetch_failed", "too_short")
                or (a.get("extract_status") or "").startswith("error")
            ),
        }

        output = {
            "generated_at": generated_at,
            "last_updated_at": timestamp,
            "stats": file_stats,
            "articles": merged,
        }

        outpath = os.path.join(args.outdir, f"enriched_{ds}.json")
        with open(outpath, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, ensure_ascii=False)

        written_files.append((outpath, len(new_articles), len(merged)))

    # ── Summary ───────────────────────────────────────────────────────────

    total = stats["total"]
    print()
    print("=" * 60)
    print("SCRAPER SUMMARY")
    print("=" * 60)
    print(f"  New articles:      {total}")
    print(f"  Already processed: {len(candidates) - len(articles)}")
    if total:
        print(
            f"  URLs resolved:     {stats['resolved']}/{total} ({100 * stats['resolved'] // total}%)"
        )
        print(
            f"  Text extracted:    {stats['extracted']}/{total} ({100 * stats['extracted'] // total}%)"
        )
    print(f"  Blocklisted:       {stats['blocklisted']}")
    print(f"  Domains skipped:   {stats['skipped']}")
    print(f"  Failed (resolve):  {stats['failed_resolve']}")
    print(f"  Failed (extract):  {stats['failed_extract']}")
    print()
    for outpath, new_count, total_count in written_files:
        print(f"  ✓ {outpath} — {new_count} new, {total_count} total")
    print()


if __name__ == "__main__":
    main()
