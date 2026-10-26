#!/usr/bin/env python3
"""
Cross-National Radar — Identifies themes that surface in multiple country
categories simultaneously.

Follows the same pattern as hotspots_generator.py: loads recent articles with
summaries, asks the LLM to identify cross-national themes (stories, framings,
or policy patterns appearing in 2+ country categories), and writes a JSON
payload for the front-end to render.

Previous-run themes are passed back into the prompt for continuity so that
ongoing cross-national patterns keep consistent titles across runs.

Usage:
  python radar_generator.py
  python radar_generator.py --hours 48 --top-n 5 --min-categories 2
  python radar_generator.py --enriched-dir data-private
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
RADAR_FILENAME = "radar.json"


def format_previous(prev: list[dict]) -> str:
    if not prev:
        return "(none — this is the first run)"
    out = []
    for i, t in enumerate(prev, start=1):
        countries = ", ".join(c.get("name", "") for c in (t.get("countries") or []))
        out.append(
            f"{i}. \"{t.get('title', '')}\" "
            f"(convergence {t.get('convergence', '?')}, "
            f"countries: {countries})"
        )
    return "\n".join(out)


# ── LLM response parsing ───────────────────────────────────────────────────


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text or "theme"


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


def normalize_theme(raw: dict, ref_map: dict, prev_by_id: dict, now_iso: str) -> dict | None:
    title = (raw.get("title") or "").strip()
    if not title:
        return None
    blurb = (raw.get("blurb") or "").strip()

    try:
        convergence = int(raw.get("convergence", 3))
    except (TypeError, ValueError):
        convergence = 3
    convergence = max(1, min(5, convergence))

    countries = raw.get("countries") or []
    if not isinstance(countries, list):
        countries = []
    normalized_countries = []
    for c in countries:
        if isinstance(c, dict):
            normalized_countries.append({
                "name": str(c.get("name", "")).strip(),
                "note": str(c.get("note", "")).strip(),
            })
        elif isinstance(c, str):
            normalized_countries.append({"name": c.strip(), "note": ""})

    signals = raw.get("signals") or []
    if not isinstance(signals, list):
        signals = [str(signals)]
    signals = [str(s).strip() for s in signals if str(s).strip()][:6]

    refs = resolve_refs(raw.get("refs"), ref_map)

    try:
        article_count = int(raw.get("article_count", len(refs)))
    except (TypeError, ValueError):
        article_count = len(refs)

    tid = slugify(title)
    prev = prev_by_id.get(tid)
    first_seen = prev["first_seen"] if prev and prev.get("first_seen") else now_iso

    return {
        "id": tid,
        "title": title,
        "blurb": blurb,
        "convergence": convergence,
        "countries": normalized_countries,
        "signals": signals,
        "article_count": article_count,
        "refs": refs,
        "first_seen": first_seen,
        "last_seen": now_iso,
    }


# ── Main ───────────────────────────────────────────────────────────────────


def load_previous_themes(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("themes", []) or []
    except Exception as e:
        print(f"  ⚠ Could not read previous themes from {path}: {e}")
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
        "themes": [],
    }
    write_payload(payload, path)
    print(f"  (status: {reason})")


def main() -> int:
    cfg = CONFIG.get("radar", {})
    parser = argparse.ArgumentParser(description="Generate cross-national radar")
    parser.add_argument("--hours", type=int, default=cfg.get("lookback_hours", 48))
    parser.add_argument("--top-n", type=int, default=cfg.get("top_n", 5))
    parser.add_argument("--min-categories", type=int, default=cfg.get("min_categories", 2))
    parser.add_argument("--min-articles", type=int, default=cfg.get("min_articles", 3))
    parser.add_argument("--enriched-dir", default="data-private")
    parser.add_argument("--archive-dir", default=None,
                        help="Also save a dated copy of a successful run under <dir>/radar/")
    parser.add_argument("--outdir", default=DEFAULT_OUTDIR,
                        help="Output dir served to the frontend")
    args = parser.parse_args()

    out_path = os.path.join(args.outdir, RADAR_FILENAME)
    continuity_path = out_path

    print(f"→ Loading articles from last {args.hours}h...")
    articles = load_recent_articles(
        args.enriched_dir, args.hours, cfg.get("max_per_category", 40)
    )

    if len(articles) < args.min_articles:
        write_empty_output(out_path, "insufficient-data", args.hours)
        return 0

    context, ref_map = build_context(articles)

    previous = load_previous_themes(continuity_path)
    prev_by_id = {t["id"]: t for t in previous if t.get("id")}

    prompt_template = cfg.get("prompt")
    if not prompt_template:
        print("Error: [radar].prompt is missing from config.toml")
        return 1

    prompt = prompt_template.format(
        top_n=args.top_n,
        min_categories=args.min_categories,
        hours=args.hours,
        previous=format_previous(previous),
        context=context,
    )

    print(f"  Prompt size: {len(prompt):,} chars")
    print(f"→ Calling LLM (continuity from {len(previous)} previous theme(s))...")
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

    raw_themes = parsed.get("themes") or []
    if not isinstance(raw_themes, list):
        print("Error: 'themes' key in LLM response is not a list")
        return 1

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    themes = []
    for raw in raw_themes[: args.top_n]:
        if not isinstance(raw, dict):
            continue
        cleaned = normalize_theme(raw, ref_map, prev_by_id, now_iso)
        if not cleaned:
            continue
        if len(cleaned["countries"]) < args.min_categories:
            continue
        if cleaned["article_count"] < args.min_articles:
            continue
        themes.append(cleaned)

    if not themes:
        write_empty_output(out_path, "no-qualifying-themes", args.hours)
        return 0

    payload = {
        "updated_at": now_iso,
        "lookback_hours": args.hours,
        "model": model_label,
        "status": "ok",
        "themes": themes,
    }
    write_payload(payload, out_path)
    if args.archive_dir:
        archive_payload(payload, args.archive_dir, "radar")
    print(f"✓ {len(themes)} theme(s) generated via {model_label}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
