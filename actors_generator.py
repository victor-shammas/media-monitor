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
  python actors_generator.py --hours 48 --top-n 5 --min-articles 3
  python actors_generator.py --enriched-dir data-private
"""

import argparse
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone

from monitor_utils import CONFIG
from ai_reporter import SHORT_PROMPT_CHAIN, extract_json, generate_with_fallback
from generator_common import archive_payload, build_context, load_recent_articles

DEFAULT_OUTDIR = "data"
ACTORS_FILENAME = "actors.json"


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
    parser.add_argument("--top-n", type=int, default=cfg.get("top_n", 5))
    parser.add_argument("--min-articles", type=int, default=cfg.get("min_articles", 3))
    parser.add_argument("--enriched-dir", default="data-private")
    parser.add_argument("--archive-dir", default=None,
                        help="Also save a dated copy of a successful run under <dir>/actors/")
    parser.add_argument("--outdir", default=DEFAULT_OUTDIR,
                        help="Output dir served to the frontend")
    args = parser.parse_args()

    out_path = os.path.join(args.outdir, ACTORS_FILENAME)
    continuity_path = out_path

    print(f"→ Loading articles from last {args.hours}h...")
    articles = load_recent_articles(
        args.enriched_dir, args.hours, cfg.get("max_per_category", 40)
    )

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
        response_text, model_label = generate_with_fallback(
            prompt, SHORT_PROMPT_CHAIN, validate=extract_json
        )
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
    if args.archive_dir:
        archive_payload(payload, args.archive_dir, "actors")
    print(f"✓ {len(actors)} actor(s) generated via {model_label}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
