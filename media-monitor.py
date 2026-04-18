#!/usr/bin/env python3
"""
Transatlantic Right-Wing Media Monitor — Fast/Basic Edition
Fetches Google News RSS directly and appends results to a text file.
Leaves URLs as raw Google News links for maximum speed. No dependencies.

Usage:
    python3 monitor_cron.py                     # prints to stdout + appends to monitor_output.txt
    python3 monitor_cron.py -o /path/to/file    # custom output path
    python3 monitor_cron.py --json              # also write a JSON snapshot
    python3 monitor_cron.py --feeds hungary frp # only fetch specific feeds
"""

import argparse
import html as html_mod
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote
from urllib.request import Request, urlopen

# ── Configuration ──────────────────────────────────────────────────────────

FEEDS = [
    {
        "id": "maga",
        "name": "🇺🇸 MAGA/Trump",
        "q": '"Donald Trump" OR "Trump administration" OR MAGA OR "JD Vance" OR "Stephen Miller" OR "America First" OR "Steve Bannon"',
        "lang": "en",
        "country": "US",
        "window": "7d",
        "max": 8,
    },
    {
        "id": "frp",
        "name": "🇳🇴 Fremskrittspartiet",
        "q": 'Fremskrittspartiet OR FrP OR "Sylvi Listhaug" OR "Norwegian Progress Party" OR "Per-Willy Amundsen" OR "Hans Andreas Limi" OR "Simen Velle"',
        "lang": "no",
        "country": "NO",
        "window": "7d",
        "max": 8,
    },
    {
        "id": "sd",
        "name": "🇸🇪 Sverigedemokraterna",
        "q": 'Sverigedemokraterna OR "Jimmie Åkesson" OR "Sweden Democrats"',
        "lang": "sv",
        "country": "SE",
        "window": "7d",
        "max": 8,
    },
    {
        "id": "rn",
        "name": "🇫🇷 Rassemblement National",
        "q": '"Rassemblement National" OR "Marine Le Pen" OR "Jordan Bardella" OR "National Rally" OR "Marion Maréchal"',
        "lang": "fr",
        "country": "FR",
        "window": "7d",
        "max": 8,
    },
    {
        "id": "fdi",
        "name": "🇮🇹 Fratelli d'Italia / Lega",
        "q": 'Meloni OR Salvini OR "Fratelli d\'Italia" OR "Brothers of Italy" OR Lega',
        "lang": "it",
        "country": "IT",
        "window": "7d",
        "max": 8,
    },
    {
        "id": "reform",
        "name": "🇬🇧 Reform UK",
        "q": '"Reform UK" OR "Nigel Farage" OR "Richard Tice"',
        "lang": "en",
        "country": "GB",
        "window": "7d",
        "max": 8,
    },
    {
        "id": "general",
        "name": "🌍 General Right-Wing News",
        "q": '"far right" OR "alt-right" OR "techno-fascism" OR "manosphere" OR "right-wing extremist" OR "national conservatism" OR "illiberal democracy" OR "fascism" OR "ethnonationalism" OR "white nationalism" OR "Christian nationalism"',
        "lang": "en",
        "country": "US",
        "window": "7d",
        "max": 12,
    },
    {
        "id": "nodes",
        "name": "🕸️ Transnational Network Infrastructure",
        "queries": [
            '"Heritage Foundation" OR "Project 2025" OR "American Enterprise Institute" OR "Claremont Institute" OR "Edmund Burke Foundation"',
            '"Danube Institute" OR "Mathias Corvinus Collegium" OR "MCC Budapest" OR "MCC Brussels"',
            '"National Conservatism" OR "NatCon" OR "Conservative Political Action Conference" OR "CPAC" OR "Turning Point USA" OR "Turning Point UK"',
            '"Institute of Economic Affairs" OR "Policy Exchange" OR "Centre for Policy Studies" OR "Alliance for Responsible Citizenship"',
        ],
        "lang": "en",
        "country": "US",
        "window": "30d",
        "max": 12,
    },
    {
        "id": "hungary",
        "name": "🇭🇺 Hungary (Fidesz / Tisza)",
        "q": '"Viktor Orban" OR "Peter Magyar" OR "Fidesz" OR "Tisza"',
        "lang": "en",
        "country": "US",
        "window": "7d",
        "max": 9,
    },
]

# ── Helpers ────────────────────────────────────────────────────────────────


def build_gnews_url(query: str, feed: dict) -> str:
    """Build a Google News RSS URL."""
    win = feed.get("window", "7d")
    lang = feed["lang"]
    country = feed["country"]
    return (
        f"https://news.google.com/rss/search?"
        f"q={quote(query + ' when:' + win)}"
        f"&hl={lang}&gl={country}&ceid={country}:{lang}"
    )


def fetch_rss(url: str, timeout: int = 15) -> list[dict]:
    """Fetch a Google News RSS feed and parse items (no URL decoding)."""
    req = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; RWMonitor/2.0)",
        },
    )
    with urlopen(req, timeout=timeout) as resp:
        raw = resp.read()

    root = ET.fromstring(raw)
    items = []

    for item_el in root.iter("item"):
        title = (item_el.findtext("title") or "").strip()
        link = (item_el.findtext("link") or "").strip()
        desc = (item_el.findtext("description") or "").strip()
        pub_date_str = (item_el.findtext("pubDate") or "").strip()

        # Parse date
        pub_dt = None
        if pub_date_str:
            try:
                pub_dt = parsedate_to_datetime(pub_date_str)
                if pub_dt.tzinfo is None:
                    pub_dt = pub_dt.replace(tzinfo=timezone.utc)
            except Exception:
                pass

        items.append(
            {
                "title": title,
                "link": link,
                "description": desc,
                "pubDate": pub_dt.isoformat() if pub_dt else pub_date_str,
                "pub_dt": pub_dt,
            }
        )
    return items


def strip_html(text: str) -> str:
    if not text:
        return ""
    t = re.sub(r"<[^>]+>", " ", text)
    t = html_mod.unescape(t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:220] + "…" if len(t) > 220 else t


def extract_source(title: str) -> str:
    m = re.search(r"\s-\s([^-]+)$", title)
    return m.group(1).strip() if m else ""


def clean_title(title: str) -> str:
    return re.sub(r"\s-\s[^-]+$", "", title).strip()


def strip_trailing_source(summary: str, source: str) -> str:
    if not summary or not source:
        return summary
    esc = re.escape(source)
    return re.sub(
        r"[\s\-–—]*" + esc + r"\s*$", "", summary, flags=re.IGNORECASE
    ).strip()


def fmt_date(iso: str) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%b %d")
    except Exception:
        return iso[:10]


# ── Core fetch ─────────────────────────────────────────────────────────────


def fetch_feed(feed: dict) -> list[dict]:
    queries = feed.get("queries", [feed.get("q", "")])
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    all_items = []
    seen = set()
    errors = []

    for q in queries:
        url = build_gnews_url(q, feed)
        try:
            raw_items = fetch_rss(url)
            for item in raw_items:
                link = item.get("link", "")
                if not link or link in seen:
                    continue
                seen.add(link)
                pub_dt = item.get("pub_dt")
                if pub_dt and pub_dt < cutoff:
                    continue

                source = extract_source(item.get("title", ""))
                summary = strip_html(item.get("description", ""))
                summary = strip_trailing_source(summary, source)

                all_items.append(
                    {
                        "title": clean_title(item.get("title", "")),
                        "url": link,
                        "source": source,
                        "date": item.get("pubDate", ""),
                        "date_dt": pub_dt,
                        "summary": summary,
                    }
                )
        except Exception as e:
            errors.append(f"{q[:40]}…: {e}")

    # Sort newest first, cap to max
    all_items.sort(
        key=lambda x: x.get("date_dt") or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    all_items = all_items[: feed.get("max", 8)]

    if errors:
        for err in errors[:2]:
            print(f"  ⚠ {err}", file=sys.stderr)

    return all_items


# ── Formatters ─────────────────────────────────────────────────────────────

SEPARATOR = "═" * 72


def format_text(
    results: dict[str, list[dict]], timestamp: str, active_feeds: list[dict] = None
) -> str:
    feeds_to_show = active_feeds or FEEDS
    lines = []
    lines.append("")
    lines.append(SEPARATOR)
    lines.append("  TRANSATLANTIC RIGHT-WING MEDIA MONITOR")
    lines.append(f"  {timestamp}")
    lines.append(SEPARATOR)

    for feed in feeds_to_show:
        fid = feed["id"]
        items = results.get(fid, [])
        lines.append("")
        lines.append(f"=== {feed['name']}  ({len(items)} items) ===")
        lines.append("")

        if not items:
            lines.append("  (no results)")

        for i, item in enumerate(items, 1):
            meta_parts = filter(None, [item.get("source", ""), fmt_date(item["date"])])
            meta = " · ".join(meta_parts)

            lines.append(f"{i}. {item['title']}")
            if meta:
                lines.append(f"   {meta}")
            if item["summary"]:
                lines.append(f"   {item['summary']}")
            lines.append(f"   {item['url']}")

            # Add a blank line between items for readability
            lines.append("")

    return "\n".join(lines)


def format_json(
    results: dict[str, list[dict]], timestamp: str, active_feeds: list[dict] = None
) -> str:
    feeds_to_show = active_feeds or FEEDS
    out = {"timestamp": timestamp, "feeds": {}}
    for feed in feeds_to_show:
        fid = feed["id"]
        items = results.get(fid, [])
        out["feeds"][fid] = {
            "name": feed["name"],
            "count": len(items),
            "items": [{k: v for k, v in it.items() if k != "date_dt"} for it in items],
        }
    return json.dumps(out, ensure_ascii=False, indent=2)


# ── Main ───────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Right-wing media monitor (Fast Edition)"
    )
    parser.add_argument(
        "-o",
        "--output",
        default="monitor_output.txt",
        help="Text output file (appended to). Default: monitor_output.txt",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Also write a JSON snapshot to <output>.json",
    )
    parser.add_argument(
        "--feeds",
        nargs="*",
        default=None,
        help="Only fetch specific feed IDs (e.g., --feeds hungary frp maga)",
    )
    args = parser.parse_args()

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    active_feeds = FEEDS

    if args.feeds:
        active_feeds = [f for f in FEEDS if f["id"] in args.feeds]
        if not active_feeds:
            print(f"No matching feeds for: {args.feeds}", file=sys.stderr)
            sys.exit(1)

    print(f"[{timestamp}] Fetching {len(active_feeds)} feeds…", file=sys.stderr)
    results = {}

    for feed in active_feeds:
        print(f"  → {feed['name']}…", file=sys.stderr)
        results[feed["id"]] = fetch_feed(feed)

    text = format_text(results, timestamp, active_feeds)

    # Print to stdout
    print(text)

    # Append to file
    with open(args.output, "a", encoding="utf-8") as f:
        f.write(text + "\n")
    print(f"\n[appended to {args.output}]", file=sys.stderr)

    if args.json:
        json_path = os.path.splitext(args.output)[0] + ".json"
        with open(json_path, "w", encoding="utf-8") as f:
            f.write(format_json(results, timestamp, active_feeds))
        print(f"[wrote {json_path}]", file=sys.stderr)


if __name__ == "__main__":
    main()
