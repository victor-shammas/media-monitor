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
import unicodedata
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

socket.setdefaulttimeout(30)

try:
    import trafilatura
    try:
        # googlenewsdecoder >= 0.2 renamed new_decoderv1 -> gnewsdecoder
        from googlenewsdecoder import gnewsdecoder
    except ImportError:
        from googlenewsdecoder import new_decoderv1 as gnewsdecoder
    from tenacity import (
        retry,
        retry_if_exception,
        stop_after_attempt,
        wait_exponential,
    )
except ImportError as e:
    print(f"Missing or incompatible dependency: {e}")
    print("Install dependencies: pip install trafilatura \"googlenewsdecoder>=0.2,<0.3\" \"selectolax<1.0\" tenacity")
    sys.exit(1)

from monitor_utils import (
    CONFIG,
    STATE_FILE,
    BLOCKLIST_FILE,
    CATEGORY_LABELS,
    get_sort_time,
    git_sync,
    normalize_title_for_dedup,
    write_json_atomic,
)
from llm_rate_limit import (
    ProviderCircuit,
    is_rate_limit_error,
    is_retryable_error,
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
    "that summarizes the key news from the article. Be concrete and specific: "
    "name the people, parties, organizations and places involved (a person's "
    "name, not 'a politician' or 'a political leader'). "
    "Always translate and respond in English, even if the article is in another "
    "language. Return ONLY the summary sentence, nothing else."
)
BATCH_SYSTEM_PROMPT = (
    "You are a news summarizer. You will receive several UNRELATED articles, "
    "each marked [ref N]. For EACH article write exactly one sentence (max 25 "
    "words) that summarizes the key news from that article. Be concrete: name "
    "the people, parties, organizations and places involved exactly as the "
    "article names them (a person's name, not 'a politician', 'a political "
    "leader' or 'an official'). Use only facts from that article itself, never "
    "from the other articles. Every summary must be in English: "
    "translate if the article is in another language, whatever language the "
    "other articles are in. Return ONLY JSON of the form "
    '{"summaries": [{"ref": 1, "summary": "..."}, ...]} with one entry per article.'
)

# Summaries are the free-tier bottleneck (requests per day, not tokens), so
# articles are sent in small batches of one category and one language. Every
# answer is checked (English, about its own article) before it is kept; a
# rejected one is retried later as a single-article request. batch_size = 1
# restores one request per article. See [summaries] in config.toml.
_SUMMARY_CFG = CONFIG.get("summaries", {})
SUMMARY_BATCH_SIZE = max(1, int(_SUMMARY_CFG.get("batch_size", 10)))
SUMMARY_MAX_REQUESTS = int(_SUMMARY_CFG.get("max_requests_per_run", 20))
MAX_SUMMARY_ATTEMPTS = 3

# Resolve/fetch failures are retried on later runs, up to this many tries.
MAX_ENRICH_ATTEMPTS = 2

# Time budget per run (minutes from the start of run_scraper). The pipeline
# step is killed at 35 min and results are only written at the end, so a run
# that overran lost everything. Scraping stops at max_scrape_minutes (what's
# left waits for the next run) and summary requests stop at
# max_total_minutes, leaving time to write the files and push.
_ENRICH_CFG = CONFIG.get("enrichment", {})
MAX_SCRAPE_MINUTES = float(_ENRICH_CFG.get("max_scrape_minutes", 15))
MAX_TOTAL_MINUTES = float(_ENRICH_CFG.get("max_total_minutes", 22))

# After this many fetch failures from one domain in a run, skip the rest of
# that domain's articles for the run (sites that block the scraper fail every
# time and would otherwise eat the budget).
DOMAIN_FAILURE_LIMIT = 3

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


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=4, max=60),
    retry=retry_if_exception(is_retryable_error),
    reraise=True,
)
def _call_gemini_batch(prompt: str, json_mode: bool = False) -> str:
    client = genai.Client()
    config = {"response_mime_type": "application/json"} if json_mode else None
    response = client.models.generate_content(
        model=SUMMARY_MODEL, contents=prompt, config=config
    )
    return response.text or ""


def _ensure_mistral():
    return bool(os.environ.get("MISTRAL_API_KEY"))


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=4, max=60),
    retry=retry_if_exception(is_retryable_error),
    reraise=True,
)
def _call_mistral_batch(user_prompt: str, json_mode: bool = False) -> str:
    import urllib.request

    api_key = os.environ["MISTRAL_API_KEY"]
    body = {
        "model": MISTRAL_MODEL,
        "messages": [
            {
                "role": "system",
                "content": BATCH_SYSTEM_PROMPT if json_mode else SUMMARY_SYSTEM_PROMPT,
            },
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": 2048 if json_mode else 1024,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    payload = json.dumps(body).encode()
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


def _added_time(item: dict) -> datetime:
    try:
        return datetime.strptime(item.get("added_at", ""), "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


def _is_retryable(record: dict) -> bool:
    """A resolve/fetch failure that may succeed on a later run (decoder
    throttling, a site's transient error) and hasn't used up its tries."""
    status = record.get("extract_status") or ""
    transient = (
        status.startswith("resolve_")
        or status == "fetch_failed"
        or status.startswith("error")
    )
    return transient and record.get("enrich_attempts", 1) < MAX_ENRICH_ATTEMPTS


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
        decoded = gnewsdecoder(google_url, interval=0.5)
        # >= 0.2 reports "success"; <= 0.1.x reported "status"
        if (decoded.get("success") or decoded.get("status")) and decoded.get(
            "decoded_url"
        ):
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


# Function words used to tell an English sentence from one in a monitored
# language. Words shared with those languages (a, in, to, for, over, will, …)
# count on both sides, so an English summary must clearly outweigh them.
_EN_WORDS = {
    "the", "of", "and", "with", "after", "from", "has", "have", "had", "were",
    "are", "its", "his", "their", "that", "amid", "says", "said", "would",
    "into", "against", "about", "which", "who", "while", "following", "than",
    "this", "been", "a", "an", "to", "in", "on", "for", "is", "as", "by", "at",
    "over", "will", "her", "was",
}
_FOREIGN_WORDS = {
    "der", "die", "das", "und", "nicht", "mit", "für", "ist", "sich", "den",
    "dem", "ein", "eine", "les", "des", "est", "une", "pour", "dans", "avec",
    "sur", "pas", "qui", "il", "che", "non", "per", "della", "del", "con",
    "sono", "los", "las", "por", "para", "una", "que", "og", "er", "ikke",
    "til", "som", "av", "på", "och", "är", "inte", "att", "för", "się",
    "jest", "nie", "oraz", "że", "przez", "dla", "na", "w", "z", "az", "és",
    "hogy", "nem", "egy", "não", "uma", "em", "com", "os", "como", "mais",
}


def _words(text: str) -> list[str]:
    return re.findall(r"[^\W\d_]+", text.lower())


def _looks_english(text: str) -> bool:
    words = _words(text)
    en = sum(w in _EN_WORDS for w in words)
    foreign = sum(w in _FOREIGN_WORDS for w in words)
    return en >= 2 and en > foreign


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c))


# Indefinite stand-ins for a named actor ("a politician", "a political
# leader"): the mark of a summary that anonymized its article.
_VAGUE_ACTOR_RE = re.compile(
    r"\b(?:a|an|one|another) (?:(?:former|local|senior|political|party|"
    r"government|opposition|right-wing|far-right) )*(?:politician|leader|"
    r"official|candidate|minister|senator|lawmaker|figure|party|deputy|mayor)\b",
    re.I,
)


def _is_grounded(summary: str, rec: dict) -> bool:
    """True if a batch summary is concrete and about its own article: it names
    something (a capitalized word that isn't a function word), doesn't fall
    back on stand-ins like "a politician", and some named word shares its
    first five letters with a word of the article. Guards batches against
    cross-article mix-ups and anonymized summaries; a rejected one is retried
    on its own."""
    if _VAGUE_ACTOR_RE.search(summary):
        return False
    names = [
        w for w in re.findall(r"[^\W\d_][\w'’-]*", summary)
        if w[0].isupper() and len(w) >= 4 and w.lower() not in _EN_WORDS
    ]
    if not names:
        return False
    source = {w[:5] for w in _words(_fold(f"{rec.get('title', '')} {rec.get('extract', '')}"))}
    return any(_fold(w)[:5] in source for w in names)


def _parse_batch(text: str, n: int) -> dict[int, str]:
    """Map ref → summary from a batch response; anything malformed is dropped."""
    text = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL)
    start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
    if start < 0:
        return {}
    try:
        data, _ = json.JSONDecoder().raw_decode(text[start:])
    except ValueError:
        return {}
    if isinstance(data, dict):
        data = data.get("summaries", [])
    out: dict[int, str] = {}
    for entry in data if isinstance(data, list) else []:
        if not isinstance(entry, dict):
            continue
        try:
            ref = int(entry.get("ref"))
        except (TypeError, ValueError):
            continue
        summary = str(entry.get("summary") or "").strip()
        if 1 <= ref <= n and summary and ref not in out:
            out[ref] = summary
    return out


def _needs_summary(r: dict) -> bool:
    """No summary yet, or an anonymized one ("a politician …") from the first
    batched runs that hasn't been redone yet."""
    summary = (r.get("summary") or "").strip()
    if not summary:
        return True
    return bool(_VAGUE_ACTOR_RE.search(summary)) and not r.get("resummarized")


def _summary_batches(records: list[dict]) -> list[list[dict]]:
    """Eligible records grouped into batches of one category and one title
    language (newest first), with previously rejected records as singles,
    then interleaved round-robin across categories so a capped run spreads
    its requests over every country instead of starting with the first."""
    by_cat: dict[str, list[dict]] = {}
    for r in records:
        if (
            r.get("extract_status") == "ok"
            and r.get("extract")
            and _needs_summary(r)
            and r.get("summary_attempts", 0) < MAX_SUMMARY_ATTEMPTS
        ):
            by_cat.setdefault(r.get("category", ""), []).append(r)

    per_cat: list[list[list[dict]]] = []
    for recs in by_cat.values():
        recs.sort(key=get_sort_time, reverse=True)
        fresh = [r for r in recs if not r.get("summary_attempts")]
        groups: list[list[dict]] = [[], []]
        for r in fresh:
            groups[0 if _looks_english(r.get("title", "")) else 1].append(r)
        batches = [
            g[i:i + SUMMARY_BATCH_SIZE]
            for g in groups
            for i in range(0, len(g), SUMMARY_BATCH_SIZE)
        ]
        batches.sort(key=lambda b: get_sort_time(b[0]), reverse=True)
        batches += [[r] for r in recs if r.get("summary_attempts")]
        per_cat.append(batches)

    ordered = []
    for i in range(max((len(b) for b in per_cat), default=0)):
        ordered += [b[i] for b in per_cat if i < len(b)]
    return ordered


def _request_summaries(batch, circuit, has_mistral, has_gemini, label):
    """One LLM request for a batch. Returns ref → summary text (refs are
    1-based positions in the batch), or None if no provider answered."""
    json_mode = len(batch) > 1
    if json_mode:
        user_prompt = "\n\n".join(
            f"[ref {k}]\nTitle: {r['title']}\n{' '.join(r['extract'].split()[:200])}"
            for k, r in enumerate(batch, 1)
        )
    else:
        r = batch[0]
        user_prompt = f"Title: {r['title']}\n\n{' '.join(r['extract'].split()[:200])}"

    text = None
    if has_mistral and not circuit.is_open("mistral"):
        try:
            text = _call_mistral_batch(user_prompt, json_mode=json_mode)
            circuit.record_success("mistral")
        except Exception as e:
            if is_rate_limit_error(e):
                if circuit.record_rate_limit("mistral"):
                    print("    ⚠ Mistral rate-limited; disabling for the rest of the run")
            elif not has_gemini or circuit.is_open("gemini"):
                print(f"    [{label}] Mistral failed: {e}")

    if text is None and has_gemini and not circuit.is_open("gemini"):
        system = BATCH_SYSTEM_PROMPT if json_mode else SUMMARY_SYSTEM_PROMPT
        try:
            text = _call_gemini_batch(f"{system}\n\n{user_prompt}", json_mode=json_mode)
            circuit.record_success("gemini")
        except Exception as e:
            if is_rate_limit_error(e):
                if circuit.record_rate_limit("gemini"):
                    print("    ⚠ Gemini rate-limited; disabling for the rest of the run")
            else:
                print(f"    [{label}] All providers failed: {e}")

    if text is None:
        return None
    if json_mode:
        return _parse_batch(text, len(batch))
    summary = _extract_summary(text)
    return {1: summary} if summary else {}


def generate_summaries(
    records: list[dict], max_requests: int | None = None, deadline: float | None = None
) -> int:
    """Generate one-sentence English summaries for extracted records that lack
    one, at most `max_requests` LLM requests (None = until providers run out)
    and none started after `deadline` (a time.monotonic() value).
    Records are updated in place: `summary` on success, `summary_attempts`
    when a provider answered but the summary was missing or failed the
    checks (rate-limit skips don't count). Returns the number generated."""
    has_gemini = _ensure_gemini()
    has_mistral = _ensure_mistral()

    if not has_gemini and not has_mistral:
        print("  Warning: neither GEMINI_API_KEY nor MISTRAL_API_KEY set, skipping summaries")
        return 0

    batches = _summary_batches(records)
    if max_requests is not None:
        batches = batches[:max_requests]
    if not batches:
        return 0

    total = sum(len(b) for b in batches)
    print(f"\n  Generating summaries for {total} articles in {len(batches)} request(s)...")
    generated = rejected = 0
    circuit = ProviderCircuit(threshold=3)

    for i, batch in enumerate(batches, 1):
        if deadline is not None and time.monotonic() > deadline:
            print(f"    ⏱ Time budget reached; {len(batches) - i + 1} request(s) left for the next run")
            break
        if circuit.is_open("mistral") and circuit.is_open("gemini"):
            print(
                f"    ⚠ All providers rate-limited; skipping remaining "
                f"{len(batches) - i + 1} request(s)"
            )
            break

        results = _request_summaries(batch, circuit, has_mistral, has_gemini, i)
        if results is not None:
            for k, rec in enumerate(batch, 1):
                summary = results.get(k)
                redo = bool((rec.get("summary") or "").strip())
                if (
                    summary
                    and len(summary.split()) <= 45
                    and _looks_english(summary)
                    and (len(batch) == 1 or _is_grounded(summary, rec))
                ):
                    rec["summary"] = summary
                    if redo:
                        rec["resummarized"] = True
                    generated += 1
                else:  # a redo keeps its old summary meanwhile
                    rec["summary_attempts"] = rec.get("summary_attempts", 0) + 1
                    rejected += 1

        if i % 10 == 0:
            print(f"    Progress: {i}/{len(batches)} requests ({generated} summaries)")

        if i < len(batches):
            time.sleep(REQUEST_DELAY)

    skipped_providers = [p for p in ("mistral", "gemini") if circuit.is_open(p)]
    skipped_note = (
        f" (rate-limited: {', '.join(skipped_providers)})"
        if skipped_providers
        else ""
    )
    rejected_note = f", {rejected} rejected for retry" if rejected else ""
    print(f"  Summaries generated: {generated}/{total}{rejected_note}{skipped_note}")
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

    write_json_atomic(STATE_FILE, state)

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

    started = time.monotonic()
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

    # Recent by publication date OR by when the monitor first saw it, so an
    # article indexed late by Google News still gets enriched.
    candidates = []
    for cat_id, items in state.items():
        if category and cat_id != category:
            continue
        for item in items:
            if get_sort_time(item) >= cutoff or _added_time(item) >= cutoff:
                candidates.append({**item, "_category": cat_id})

    if not candidates:
        print(f"No articles found in the last {hours} hours.")
        return

    candidate_dates = {article_date_slug(c) for c in candidates}
    already_processed_urls = set()
    retry_records: dict[str, dict] = {}
    titles_by_date: dict[str, set] = {}
    existing_by_date = {}
    for ds in candidate_dates:
        data = load_existing_enriched(outdir, ds)
        if data:
            existing_by_date[ds] = data
            for a in data.get("articles", []):
                url = a.get("google_url", "")
                if _is_retryable(a):
                    retry_records[url] = a
                else:
                    already_processed_urls.add(url)
                titles_by_date.setdefault(ds, set()).add(
                    normalize_title_for_dedup(a.get("title", ""))
                )

    # Skip articles whose title is already in their day's file or earlier in
    # this batch (the same story under another category or URL): the merge
    # below would drop them anyway, and since their URL is never recorded
    # they'd be re-scraped every run.
    articles = []
    for c in candidates:
        url = c.get("url", "")
        if url in already_processed_urls:
            continue
        if url not in retry_records:
            seen_titles = titles_by_date.setdefault(article_date_slug(c), set())
            title_key = normalize_title_for_dedup(c.get("title", ""))
            if title_key in seen_titles:
                continue
            seen_titles.add(title_key)
        articles.append(c)

    # New articles first (newest first), then retries of earlier failures, so
    # a run that hits its time budget has spent it on fresh coverage.
    articles.sort(key=get_sort_time, reverse=True)
    articles.sort(key=lambda c: c.get("url", "") in retry_records)

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    if not articles:
        # Nothing new to scrape, but the summary backlog below may still need work.
        print(f"[{timestamp}] All {len(candidates)} articles already processed.")
    else:
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

    domain_failures: dict[str, int] = {}
    processed = 0
    for i, item in enumerate(articles, 1):
        if time.monotonic() - started > MAX_SCRAPE_MINUTES * 60:
            print(
                f"\n  ⏱ Scrape budget ({MAX_SCRAPE_MINUTES:g} min) reached; "
                f"{len(articles) - i + 1} article(s) left for the next run"
            )
            break
        processed += 1
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
            "enrich_attempts": retry_records[google_url].get("enrich_attempts", 1) + 1
            if google_url in retry_records
            else 1,
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

        if domain_failures.get(domain, 0) >= DOMAIN_FAILURE_LIMIT:
            record["extract_status"] = "fetch_failed"
            stats["failed_extract"] += 1
            print(f"    ✗ fetch_failed ({domain}, skipped: failing this run)")
            ds = article_date_slug(record)
            new_by_date.setdefault(ds, []).append(record)
            continue

        extract, word_count, extract_status = extract_article(resolved_url)
        if extract_status == "fetch_failed":
            domain_failures[domain] = domain_failures.get(domain, 0) + 1
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

    stats["total"] = processed

    # ── Merge into per-day files ──────────────────────────────────────────

    merged_by_date: dict[str, list[dict]] = {}
    for ds in sorted(set(new_by_date) | set(existing_by_date)):
        new_articles = new_by_date.get(ds, [])
        existing = existing_by_date.get(ds)
        merged = list(existing.get("articles", [])) if existing else []
        index_by_url = {a["google_url"]: i for i, a in enumerate(merged)}
        existing_titles_in_file = {
            normalize_title_for_dedup(a.get("title", "")) for a in merged
        }
        for a in new_articles:
            if a["google_url"] in index_by_url:  # a retried failure: replace it
                merged[index_by_url[a["google_url"]]] = a
                continue
            title_key = normalize_title_for_dedup(a.get("title", ""))
            if title_key not in existing_titles_in_file:
                merged.append(a)
                existing_titles_in_file.add(title_key)
        merged_by_date[ds] = merged

    # ── Step 3: Generate AI summaries ────────────────────────────────────
    # Over everything in these day files that still lacks a summary, not just
    # this run's new articles, so a run cut short by rate limits leaves a
    # backlog for the next run instead of losing those summaries for good.

    all_records = [a for merged in merged_by_date.values() for a in merged]
    before = {id(a): (a.get("summary"), a.get("summary_attempts")) for a in all_records}
    summary_count = generate_summaries(
        all_records,
        max_requests=SUMMARY_MAX_REQUESTS,
        deadline=started + MAX_TOTAL_MINUTES * 60,
    )
    touched = {
        id(a) for a in all_records
        if (a.get("summary"), a.get("summary_attempts")) != before[id(a)]
    }
    summarized_records = [a for a in all_records if id(a) in touched and a.get("summary")]

    # ── Write the day files that changed ─────────────────────────────────

    written_files = []
    for ds, merged in merged_by_date.items():
        new_count = len(new_by_date.get(ds, []))
        if not new_count and not any(id(a) in touched for a in merged):
            continue
        existing = existing_by_date.get(ds)
        generated_at = existing.get("generated_at", timestamp) if existing else timestamp

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
        write_json_atomic(outpath, output)

        written_files.append((outpath, new_count, len(merged)))

    # ── Write AI summaries back to monitor_state.json + rebuild feeds ────

    if summary_count:
        state_updated = _write_summaries_to_state(summarized_records)
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
