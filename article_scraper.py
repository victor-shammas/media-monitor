#!/usr/bin/env python3
"""
Article Scraper — Enrichment library for the media monitor pipeline.

Resolves Google News redirect URLs, extracts article text via trafilatura,
and generates AI summaries. Called by media-monitor.py --enrich or standalone.

Usage (standalone):
    python article_scraper.py              # Scrape articles from the last 24 hours
    python article_scraper.py --hours 48   # Expand window to the last 48 hours
    python article_scraper.py --category usa  # Scrape only a single category

Usage (as library):
    from article_scraper import run_scraper
    run_scraper(hours=24, outdir="data-private")
"""

import argparse
import json
import os
import re
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

from monitor_utils import (
    STATE_FILE,
    BLOCKLIST_FILE,
    CATEGORY_LABELS,
    get_sort_time,
    git_sync,
    normalize_title_for_dedup,
)

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

# AI summary generation
SUMMARY_MODEL = "gemini-2.5-flash"
MISTRAL_BASE_URL = "https://api.mistral.ai/v1"
MISTRAL_MODEL = "mistral-small-latest"
SUMMARY_SYSTEM_PROMPT = (
    "You are a news summarizer. Write exactly one sentence (max 25 words) "
    "that summarizes the key news from the article. Be concrete and specific. "
    "Always translate and respond in English, even if the article is in another "
    "language. Return ONLY the summary sentence, nothing else."
)

genai = None


def _ensure_gemini():
    global genai
    if genai is not None:
        return True
    if not os.environ.get("GEMINI_API_KEY"):
        return False
    try:
        from google import genai as _genai

        genai = _genai
        return True
    except ImportError:
        print("  Warning: google-genai not installed, skipping summary generation")
        return False


def _call_gemini_batch(prompt: str) -> str:
    client = genai.Client()
    response = client.models.generate_content(model=SUMMARY_MODEL, contents=prompt)
    return response.text or ""


def _ensure_mistral():
    return bool(os.environ.get("MISTRAL_API_KEY"))


def _call_mistral_batch(user_prompt: str) -> str:
    import urllib.request

    api_key = os.environ["MISTRAL_API_KEY"]
    payload = json.dumps(
        {
            "model": MISTRAL_MODEL,
            "messages": [
                {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": 1024,
        }
    ).encode()
    req = urllib.request.Request(
        f"{MISTRAL_BASE_URL}/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())
    text = data["choices"][0]["message"]["content"]
    return re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL)


# ── Helpers ────────────────────────────────────────────────────────────────


def load_blocklist_raw() -> dict:
    """Load blocklist.json in raw list form (for substring matching in scraper)."""
    default = {"urls": [], "sources": [], "title_patterns": []}
    if not os.path.exists(BLOCKLIST_FILE):
        return default
    try:
        with open(BLOCKLIST_FILE, "r", encoding="utf-8") as f:
            bl = json.load(f)
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
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")


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


# ── Step 3: Generate one-sentence summaries (Gemini Flash → Mistral fallback)


def _extract_summary(text: str) -> str | None:
    """Extract a clean summary sentence from a single-article LLM response."""
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^\d+\.\s*(.+)", line)
        if m:
            line = m.group(1).strip()
        if len(line) > 10:
            return line
    return None


def generate_summaries(records: list[dict]) -> int:
    """Generate one-sentence AI summaries, one article at a time."""
    has_gemini = _ensure_gemini()
    has_mistral = _ensure_mistral()

    if not has_gemini and not has_mistral:
        print("  Warning: neither GEMINI_API_KEY nor MISTRAL_API_KEY set, skipping summaries")
        return 0

    extractable = [
        r for r in records if r.get("extract_status") == "ok" and r.get("extract")
    ]
    if not extractable:
        return 0

    print(f"\n  Generating summaries for {len(extractable)} articles...")
    generated = 0

    for i, rec in enumerate(extractable, 1):
        snippet = " ".join(rec["extract"].split()[:200])
        user_prompt = f"Title: {rec['title']}\n\n{snippet}"

        text = None

        if has_mistral:
            try:
                text = _call_mistral_batch(user_prompt)
            except Exception as e:
                if not has_gemini:
                    print(f"    [{i}] Mistral failed: {e}")

        if text is None and has_gemini:
            gemini_prompt = f"{SUMMARY_SYSTEM_PROMPT}\n\n{user_prompt}"
            try:
                text = _call_gemini_batch(gemini_prompt)
            except Exception as e:
                print(f"    [{i}] All providers failed: {e}")

        if text:
            summary = _extract_summary(text)
            if summary:
                rec["summary"] = summary
                generated += 1

        if i % 10 == 0:
            print(f"    Progress: {i}/{len(extractable)} ({generated} summaries)")

        if i < len(extractable):
            time.sleep(REQUEST_DELAY)

    print(f"  Summaries generated: {generated}/{len(extractable)}")
    return generated


def _write_summaries_to_state(records: list[dict]) -> int:
    """Write AI-generated summaries back to monitor_state.json."""
    summary_records = [r for r in records if r.get("summary")]
    if not summary_records:
        return 0

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)

    updated = 0
    for rec in summary_records:
        cat = rec.get("category", "")
        google_url = rec.get("google_url", "")
        for item in state.get(cat, []):
            if item.get("url") == google_url:
                item["summary"] = rec["summary"]
                updated += 1
                break

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

    return updated


# ── Main ──────────────────────────────────────────────────────────────────


def run_scraper(
    hours: int = 24,
    outdir: str = "data-private",
    category: str | None = None,
) -> None:
    """Programmatic entry point for the scraper. Called by media-monitor.py --enrich."""
    if not os.path.exists(STATE_FILE):
        print(f"Error: {STATE_FILE} not found.")
        return

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)

    blocklist = load_blocklist_raw()
    bl_sources = len(blocklist.get("sources", []))
    bl_patterns = len(blocklist.get("title_patterns", []))
    bl_urls = len(blocklist.get("urls", []))
    if bl_sources or bl_patterns or bl_urls:
        print(
            f"  Blocklist loaded: {bl_sources} sources, {bl_patterns} title patterns, {bl_urls} URLs"
        )

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    os.makedirs(outdir, exist_ok=True)

    candidates = []
    for cat_id, items in state.items():
        if category and cat_id != category:
            continue
        for item in items:
            if get_sort_time(item) >= cutoff:
                candidates.append({**item, "_category": cat_id})

    if not candidates:
        print(f"No articles found in the last {hours} hours.")
        return

    candidate_dates = {article_date_slug(c) for c in candidates}
    already_processed_urls = set()
    existing_by_date = {}
    for ds in candidate_dates:
        data = load_existing_enriched(outdir, ds)
        if data:
            existing_by_date[ds] = data
            for a in data.get("articles", []):
                already_processed_urls.add(a.get("google_url", ""))

    articles = [c for c in candidates if c.get("url", "") not in already_processed_urls]

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    if not articles:
        print(f"[{timestamp}] All {len(candidates)} articles already processed.")
        return

    cat_count = len(set(a["_category"] for a in articles))
    print(
        f"[{timestamp}] Scraping {len(articles)} new articles across {cat_count} categories"
    )
    print(f"  ({len(candidates) - len(articles)} already processed, skipped)")
    print(f"  Look-back: {hours}h | Output: {outdir}/enriched_YYYY-MM-DD.json")
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
            "summary": None,
        }

        progress = f"[{i}/{len(articles)}]"
        print(f"  {progress} {cat_label} | {source} | {title[:55]}...")

        if is_blocklisted_source(source, blocklist):
            record["extract_status"] = "blocklisted_source"
            stats["blocklisted"] += 1
            print(f"    ⊘ Blocklisted source ({source})")
            ds = article_date_slug(record)
            new_by_date.setdefault(ds, []).append(record)
            continue

        if is_blocklisted_title(title, blocklist):
            record["extract_status"] = "blocklisted_title"
            stats["blocklisted"] += 1
            print(f"    ⊘ Blocklisted title pattern")
            ds = article_date_slug(record)
            new_by_date.setdefault(ds, []).append(record)
            continue

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

        if is_blocklisted_url(resolved_url, blocklist):
            record["extract_status"] = "blocklisted_url"
            stats["blocklisted"] += 1
            print(f"    ⊘ Blocklisted URL ({resolved_url})")
            ds = article_date_slug(record)
            new_by_date.setdefault(ds, []).append(record)
            continue

        domain = urlparse(resolved_url).netloc.lower()
        if domain in SKIP_DOMAINS:
            record["extract_status"] = "skipped_domain"
            stats["skipped"] += 1
            print(f"    ⊘ Skipped ({domain})")
            ds = article_date_slug(record)
            new_by_date.setdefault(ds, []).append(record)
            continue

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

    # ── Step 3: Generate AI summaries ────────────────────────────────────

    all_new_records = [r for recs in new_by_date.values() for r in recs]
    summary_count = generate_summaries(all_new_records)

    # ── Merge into per-day files and write ────────────────────────────────

    written_files = []
    for ds in sorted(new_by_date.keys()):
        new_articles = new_by_date[ds]
        existing = existing_by_date.get(ds)

        if existing:
            merged = list(existing.get("articles", []))
            existing_urls_in_file = {a["google_url"] for a in merged}
            existing_titles_in_file = {
                normalize_title_for_dedup(a.get("title", "")) for a in merged
            }
            for a in new_articles:
                title_key = normalize_title_for_dedup(a.get("title", ""))
                if (a["google_url"] not in existing_urls_in_file
                        and title_key not in existing_titles_in_file):
                    merged.append(a)
                    existing_titles_in_file.add(title_key)
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
            "summarized": sum(1 for a in merged if (a.get("summary") or "").strip()),
        }

        output = {
            "generated_at": generated_at,
            "last_updated_at": timestamp,
            "stats": file_stats,
            "articles": merged,
        }

        outpath = os.path.join(outdir, f"enriched_{ds}.json")
        with open(outpath, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, ensure_ascii=False)

        written_files.append((outpath, len(new_articles), len(merged)))

    # ── Write AI summaries back to monitor_state.json + rebuild feeds ────

    if summary_count:
        state_updated = _write_summaries_to_state(all_new_records)
        print(f"\n  Wrote {state_updated} summaries back to {STATE_FILE}")

        import subprocess

        print("  Rebuilding feed text files...")
        subprocess.run(
            [sys.executable, "media-monitor.py", "--rebuild"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
        )

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
    print(f"  AI summaries:      {summary_count}")
    print(f"  Blocklisted:       {stats['blocklisted']}")
    print(f"  Domains skipped:   {stats['skipped']}")
    print(f"  Failed (resolve):  {stats['failed_resolve']}")
    print(f"  Failed (extract):  {stats['failed_extract']}")
    print()
    for outpath, new_count, total_count in written_files:
        print(f"  ✓ {outpath} — {new_count} new, {total_count} total")
    print()


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

    git_sync()
    run_scraper(hours=args.hours, outdir=args.outdir, category=args.category)


if __name__ == "__main__":
    main()
