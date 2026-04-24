#!/usr/bin/env python3
"""
Hotspots Generator — Identifies the top-N "hotspots" in recent coverage.

A hotspot is an event or development that commands disproportionate attention
because of high affective investment (intensity of framing, controversy, moral
charge), not just article volume. The script trawls the last N hours of
articles (preferring enriched files with summaries, falling back to
monitor_state.json), asks the LLM to cluster and rank, and writes the result
to feeds/hotspots.json for the front-end to render.

The previous run's hotspots are passed back into the prompt so that ongoing
hotspots keep the same title across runs (continuity).

Usage:
  python hotspots_generator.py
  python hotspots_generator.py --hours 48 --top-n 5 --min-articles 3
  python hotspots_generator.py --enriched-dir data-private --outdir feeds \\
      --private-outdir data-private

Output is written to both --outdir (public, served to the frontend) and
--private-outdir (canonical artifact in the private data repo). The contents
are identical — a hotspot is derived from already-public summaries — but the
private copy provides an audit trail alongside the enriched JSON files.
"""

import argparse
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timedelta, timezone

from monitor_utils import CONFIG, CATEGORY_LABELS, get_sort_time, normalize_title_for_dedup
from ai_reporter import generate_with_fallback, load_enriched

STATE_FILE = "data/monitor_state.json"
DEFAULT_OUTDIR = "data"
ARCHIVE_SUBDIR = "hotspots"
HOTSPOTS_FILENAME = "hotspots.json"
MAX_PROMPT_CHARS = 350_000


# ── Article loading ────────────────────────────────────────────────────────


def load_recent_articles(enriched_dir: str, hours: int) -> list[dict]:
    """Load articles from the last `hours` hours.

    Prefers enriched data (which has real summaries). Falls back to
    monitor_state.json articles whose `summary` field is non-empty (the
    enriched pipeline writes summaries back to state).
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    enriched = load_enriched(enriched_dir, hours=hours)

    articles: list[dict] = []
    seen: set[str] = set()

    if enriched:
        for a in enriched:
            if get_sort_time(a) < cutoff:
                continue
            summary = (a.get("summary") or "").strip()
            if not summary:
                continue
            key = normalize_title_for_dedup(a.get("title", ""))
            if not key or key in seen:
                continue
            seen.add(key)
            articles.append({
                "title": a.get("title", ""),
                "source": a.get("source", ""),
                "url": a.get("resolved_url") or a.get("google_url", ""),
                "date": a.get("date", ""),
                "category": a.get("category", "unknown"),
                "summary": summary,
            })

    if articles:
        return articles

    # Fallback: state file (summaries are written back from enrichment)
    if not os.path.exists(STATE_FILE):
        return []
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception as e:
        print(f"  ⚠ Could not read {STATE_FILE}: {e}")
        return []

    for category, items in state.items():
        for item in items:
            if get_sort_time(item) < cutoff:
                continue
            summary = (item.get("summary") or "").strip()
            if not summary:
                continue
            key = normalize_title_for_dedup(item.get("title", ""))
            if not key or key in seen:
                continue
            seen.add(key)
            articles.append({
                "title": item.get("title", ""),
                "source": item.get("source", ""),
                "url": item.get("url", ""),
                "date": item.get("date", ""),
                "category": category,
                "summary": summary,
            })

    return articles


# ── Prompt assembly ────────────────────────────────────────────────────────


def build_context(articles: list[dict]) -> tuple[str, dict]:
    """Format articles into the LLM context block. Returns (text, ref_map).

    Articles are sorted newest-first and ref_map maps reference number → article.
    Truncates if the joined context exceeds MAX_PROMPT_CHARS.
    """
    articles = sorted(articles, key=get_sort_time, reverse=True)
    lines: list[str] = []
    ref_map: dict[int, dict] = {}
    chars_used = 0

    for i, a in enumerate(articles, start=1):
        cat_label = CATEGORY_LABELS.get(a["category"], a["category"])
        date_short = (a.get("date") or "")[:10]
        line = (
            f"[{i}] {a['title']} | {cat_label} | {date_short} | {a['source']}\n"
            f"    SUMMARY: {a['summary']}"
        )
        if lines and chars_used + len(line) > MAX_PROMPT_CHARS:
            print(f"  ⚠ Truncated at {i - 1} articles to fit context window")
            break
        lines.append(line)
        ref_map[i] = a
        chars_used += len(line)

    return "\n".join(lines), ref_map


def format_previous(prev: list[dict]) -> str:
    """Compact representation of the previous run's hotspots for continuity."""
    if not prev:
        return "(none — this is the first run)"
    out = []
    for i, h in enumerate(prev, start=1):
        out.append(
            f"{i}. \"{h.get('title', '')}\" "
            f"(intensity {h.get('intensity', '?')}, "
            f"{h.get('article_count', 0)} articles)"
        )
    return "\n".join(out)


# ── LLM response parsing ───────────────────────────────────────────────────


def extract_json(text: str) -> dict:
    """Pull a JSON object out of an LLM response, tolerating markdown fences."""
    text = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, flags=re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Last-ditch: find the first {...} block
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def slugify(text: str) -> str:
    """Stable id from a hotspot title."""
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text or "hotspot"


def resolve_refs(ref_nums, ref_map: dict) -> list[dict]:
    """Convert reference numbers from the LLM into article dicts for the UI."""
    out = []
    seen_urls = set()
    for n in ref_nums or []:
        if not isinstance(n, int):
            try:
                n = int(n)
            except (TypeError, ValueError):
                continue
        a = ref_map.get(n)
        if not a:
            continue
        url = a.get("url", "")
        if url and url in seen_urls:
            continue
        seen_urls.add(url)
        out.append({
            "title": a.get("title", ""),
            "source": a.get("source", ""),
            "url": url,
            "date": a.get("date", ""),
            "category": a.get("category", ""),
        })
    return out


def normalize_hotspot(raw: dict, ref_map: dict, prev_by_id: dict, now_iso: str) -> dict | None:
    """Validate and clean one hotspot dict from the LLM, merging persistence."""
    title = (raw.get("title") or "").strip()
    if not title:
        return None
    blurb = (raw.get("blurb") or "").strip()
    affect = (raw.get("affect") or "").strip()

    try:
        intensity = int(raw.get("intensity", 3))
    except (TypeError, ValueError):
        intensity = 3
    intensity = max(1, min(5, intensity))

    affect_signals = raw.get("affect_signals") or []
    if not isinstance(affect_signals, list):
        affect_signals = [str(affect_signals)]
    affect_signals = [str(s).strip() for s in affect_signals if str(s).strip()][:6]

    categories = raw.get("categories") or []
    if not isinstance(categories, list):
        categories = []
    categories = [str(c).strip() for c in categories if str(c).strip()]

    refs = resolve_refs(raw.get("refs"), ref_map)

    try:
        article_count = int(raw.get("article_count", len(refs)))
    except (TypeError, ValueError):
        article_count = len(refs)

    hid = slugify(title)
    prev = prev_by_id.get(hid)
    first_seen = prev["first_seen"] if prev and prev.get("first_seen") else now_iso

    return {
        "id": hid,
        "title": title,
        "blurb": blurb,
        "affect": affect,
        "intensity": intensity,
        "affect_signals": affect_signals,
        "categories": categories,
        "article_count": article_count,
        "refs": refs,
        "first_seen": first_seen,
        "last_seen": now_iso,
    }


# ── Main ───────────────────────────────────────────────────────────────────


def load_previous_hotspots(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("hotspots", []) or []
    except Exception as e:
        print(f"  ⚠ Could not read previous hotspots from {path}: {e}")
        return []


def write_payload(payload: dict, path: str) -> None:
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    print(f"  Wrote {path}")


def archive_payload(payload: dict, outdir: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M")
    archive_dir = os.path.join(outdir, ARCHIVE_SUBDIR)
    os.makedirs(archive_dir, exist_ok=True)
    archive_path = os.path.join(archive_dir, f"{ts}.json")
    with open(archive_path, "w", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"  Archived {archive_path}")


def write_empty_output(path: str, reason: str, hours: int) -> None:
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "lookback_hours": hours,
        "model": None,
        "status": reason,
        "hotspots": [],
    }
    write_payload(payload, path)
    print(f"  (status: {reason})")


def main() -> int:
    cfg = CONFIG.get("hotspots", {})
    parser = argparse.ArgumentParser(description="Generate hotspots clustering")
    parser.add_argument("--hours", type=int, default=cfg.get("lookback_hours", 48))
    parser.add_argument("--top-n", type=int, default=cfg.get("top_n", 5))
    parser.add_argument("--min-articles", type=int, default=cfg.get("min_articles", 3))
    parser.add_argument("--enriched-dir", default="data-private")
    parser.add_argument("--outdir", default=DEFAULT_OUTDIR,
                        help="Output dir served to the frontend")
    args = parser.parse_args()

    out_path = os.path.join(args.outdir, HOTSPOTS_FILENAME)
    continuity_path = out_path

    print(f"→ Loading articles from last {args.hours}h...")
    articles = load_recent_articles(args.enriched_dir, args.hours)
    print(f"  Found {len(articles)} articles with summaries")

    if len(articles) < args.min_articles:
        write_empty_output(out_path, "insufficient-data", args.hours)
        return 0

    context, ref_map = build_context(articles)

    previous = load_previous_hotspots(continuity_path)
    prev_by_id = {h["id"]: h for h in previous if h.get("id")}

    prompt_template = cfg.get("prompt")
    if not prompt_template:
        print("Error: [hotspots].prompt is missing from config.toml")
        return 1

    prompt = prompt_template.format(
        top_n=args.top_n,
        min_articles=args.min_articles,
        hours=args.hours,
        previous=format_previous(previous),
        context=context,
    )

    print(f"  Prompt size: {len(prompt):,} chars")
    print(f"→ Calling LLM (continuity from {len(previous)} previous hotspot(s))...")
    try:
        response_text, model_label = generate_with_fallback(prompt)
    except SystemExit:
        write_empty_output(out_path, "llm-unavailable", args.hours)
        return 1

    try:
        parsed = extract_json(response_text)
    except Exception as e:
        print(f"Error: failed to parse LLM JSON response: {e}")
        print(f"  First 500 chars: {response_text[:500]}")
        return 1

    raw_hotspots = parsed.get("hotspots") or []
    if not isinstance(raw_hotspots, list):
        print("Error: 'hotspots' key in LLM response is not a list")
        return 1

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    hotspots = []
    for raw in raw_hotspots[: args.top_n]:
        if not isinstance(raw, dict):
            continue
        cleaned = normalize_hotspot(raw, ref_map, prev_by_id, now_iso)
        if cleaned and cleaned["article_count"] >= args.min_articles:
            hotspots.append(cleaned)

    if not hotspots:
        write_empty_output(out_path, "no-qualifying-hotspots", args.hours)
        return 0

    payload = {
        "updated_at": now_iso,
        "lookback_hours": args.hours,
        "model": model_label,
        "status": "ok",
        "hotspots": hotspots,
    }
    write_payload(payload, out_path)
    archive_payload(payload, args.outdir)
    print(f"✓ {len(hotspots)} hotspot(s) generated via {model_label}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
