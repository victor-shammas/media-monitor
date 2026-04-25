#!/usr/bin/env python3
"""
Key Actors Generator — Identifies the most prominent political figures in
recent coverage.

Follows the same pattern as hotspots_generator.py: loads recent articles with
summaries, asks the LLM to identify and rank the top-N actors by prominence,
and writes a JSON payload for the front-end to render.

Previous-run actors are passed back into the prompt for continuity so that
ongoing coverage of the same person keeps a consistent entry across runs.

Usage:
  python actors_generator.py
  python actors_generator.py --hours 48 --top-n 8 --min-articles 3
  python actors_generator.py --enriched-dir data-private
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
ARCHIVE_SUBDIR = "actors"
ACTORS_FILENAME = "actors.json"
MAX_PROMPT_CHARS = 350_000


# ── Article loading ────────────────────────────────────────────────────────


def load_recent_articles(enriched_dir: str, hours: int) -> list[dict]:
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
    if not prev:
        return "(none — this is the first run)"
    out = []
    for i, a in enumerate(prev, start=1):
        out.append(
            f"{i}. \"{a.get('name', '')}\" — {a.get('role', '')} "
            f"(prominence {a.get('prominence', '?')}, "
            f"{a.get('article_count', 0)} articles)"
        )
    return "\n".join(out)


# ── LLM response parsing ───────────────────────────────────────────────────


def extract_json(text: str) -> dict:
    text = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, flags=re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text or "actor"


def resolve_refs(ref_nums, ref_map: dict) -> list[dict]:
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


def normalize_actor(raw: dict, ref_map: dict, prev_by_id: dict, now_iso: str) -> dict | None:
    name = (raw.get("name") or "").strip()
    if not name:
        return None
    role = (raw.get("role") or "").strip()
    blurb = (raw.get("blurb") or "").strip()

    try:
        prominence = int(raw.get("prominence", 3))
    except (TypeError, ValueError):
        prominence = 3
    prominence = max(1, min(5, prominence))

    signals = raw.get("signals") or []
    if not isinstance(signals, list):
        signals = [str(signals)]
    signals = [str(s).strip() for s in signals if str(s).strip()][:6]

    categories = raw.get("categories") or []
    if not isinstance(categories, list):
        categories = []
    categories = [str(c).strip() for c in categories if str(c).strip()]

    refs = resolve_refs(raw.get("refs"), ref_map)

    try:
        article_count = int(raw.get("article_count", len(refs)))
    except (TypeError, ValueError):
        article_count = len(refs)

    aid = slugify(name)
    prev = prev_by_id.get(aid)
    first_seen = prev["first_seen"] if prev and prev.get("first_seen") else now_iso

    return {
        "id": aid,
        "name": name,
        "role": role,
        "blurb": blurb,
        "prominence": prominence,
        "signals": signals,
        "categories": categories,
        "article_count": article_count,
        "refs": refs,
        "first_seen": first_seen,
        "last_seen": now_iso,
    }


# ── Main ───────────────────────────────────────────────────────────────────


def load_previous_actors(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("actors", []) or []
    except Exception as e:
        print(f"  ⚠ Could not read previous actors from {path}: {e}")
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
        "actors": [],
    }
    write_payload(payload, path)
    print(f"  (status: {reason})")


def main() -> int:
    cfg = CONFIG.get("actors", {})
    parser = argparse.ArgumentParser(description="Generate key actors clustering")
    parser.add_argument("--hours", type=int, default=cfg.get("lookback_hours", 48))
    parser.add_argument("--top-n", type=int, default=cfg.get("top_n", 8))
    parser.add_argument("--min-articles", type=int, default=cfg.get("min_articles", 3))
    parser.add_argument("--enriched-dir", default="data-private")
    parser.add_argument("--outdir", default=DEFAULT_OUTDIR,
                        help="Output dir served to the frontend")
    args = parser.parse_args()

    out_path = os.path.join(args.outdir, ACTORS_FILENAME)
    continuity_path = out_path

    print(f"→ Loading articles from last {args.hours}h...")
    articles = load_recent_articles(args.enriched_dir, args.hours)
    print(f"  Found {len(articles)} articles with summaries")

    if len(articles) < args.min_articles:
        write_empty_output(out_path, "insufficient-data", args.hours)
        return 0

    context, ref_map = build_context(articles)

    previous = load_previous_actors(continuity_path)
    prev_by_id = {a["id"]: a for a in previous if a.get("id")}

    prompt_template = cfg.get("prompt")
    if not prompt_template:
        print("Error: [actors].prompt is missing from config.toml")
        return 1

    prompt = prompt_template.format(
        top_n=args.top_n,
        min_articles=args.min_articles,
        hours=args.hours,
        previous=format_previous(previous),
        context=context,
    )

    print(f"  Prompt size: {len(prompt):,} chars")
    print(f"→ Calling LLM (continuity from {len(previous)} previous actor(s))...")
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

    raw_actors = parsed.get("actors") or []
    if not isinstance(raw_actors, list):
        print("Error: 'actors' key in LLM response is not a list")
        return 1

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    actors = []
    for raw in raw_actors[: args.top_n]:
        if not isinstance(raw, dict):
            continue
        cleaned = normalize_actor(raw, ref_map, prev_by_id, now_iso)
        if cleaned and cleaned["article_count"] >= args.min_articles:
            actors.append(cleaned)

    if not actors:
        write_empty_output(out_path, "no-qualifying-actors", args.hours)
        return 0

    payload = {
        "updated_at": now_iso,
        "lookback_hours": args.hours,
        "model": model_label,
        "status": "ok",
        "actors": actors,
    }
    write_payload(payload, out_path)
    archive_payload(payload, args.outdir)
    print(f"✓ {len(actors)} actor(s) generated via {model_label}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
