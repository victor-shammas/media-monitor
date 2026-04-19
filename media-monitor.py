#!/usr/bin/env python3
"""
Transatlantic Right-Wing Media Monitor — Multi-File Edition
Maintains a database of seen articles, regenerates cleanly grouped individual
text files for each category, and ensures strict chronological sorting.
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

STATE_FILE = "monitor_state.json"
MAX_NEW_PER_RUN = 30  # The universal limit of new articles to add per run per category

FEEDS = [
    {
        "id": "maga",
        "filename": "usa.txt",
        "name": "🇺🇸 MAGA/Trump",
        "q": '"Donald Trump" OR "Trump administration" OR MAGA OR "JD Vance" OR "Stephen Miller" OR "America First" OR "Steve Bannon"',
        "lang": "en",
        "country": "US",
    },
    {
        "id": "frp",
        "filename": "norway.txt",
        "name": "🇳🇴 Fremskrittspartiet",
        "q": 'Fremskrittspartiet OR FrP OR "Sylvi Listhaug" OR "Norwegian Progress Party" OR "Per-Willy Amundsen" OR "Hans Andreas Limi" OR "Simen Velle"',
        "lang": "no",
        "country": "NO",
    },
    {
        "id": "sd",
        "filename": "sweden.txt",
        "name": "🇸🇪 Sverigedemokraterna",
        "q": 'Sverigedemokraterna OR "Jimmie Åkesson" OR "Sweden Democrats"',
        "lang": "sv",
        "country": "SE",
    },
    {
        "id": "rn",
        "filename": "france.txt",
        "name": "🇫🇷 Rassemblement National",
        "q": '"Rassemblement National" OR "Marine Le Pen" OR "Jordan Bardella" OR "National Rally" OR "Marion Maréchal"',
        "lang": "fr",
        "country": "FR",
    },
    {
        "id": "fdi",
        "filename": "italy.txt",
        "name": "🇮🇹 Fratelli d'Italia / Lega",
        "q": 'Meloni OR Salvini OR "Fratelli d\'Italia" OR "Brothers of Italy" OR Lega',
        "lang": "it",
        "country": "IT",
    },
    {
        "id": "reform",
        "filename": "uk.txt",
        "name": "🇬🇧 Reform UK",
        "q": '"Reform UK" OR "Nigel Farage" OR "Richard Tice"',
        "lang": "en",
        "country": "GB",
    },
    {
        "id": "afd",
        "filename": "germany.txt",
        "name": "🇩🇪 Alternative für Deutschland",
        "q": '"Alternative fur Deutschland" OR AfD OR "Tino Chrupalla" OR "Alice Weidel"',
        "lang": "de",
        "country": "DE",
    },
    {
        "id": "general",
        "filename": "general.txt",
        "name": "🌍 General Right-Wing News",
        "q": '"far right" OR "alt-right" OR "techno-fascism" OR "manosphere" OR "right-wing extremist" OR "national conservatism" OR "illiberal democracy" OR "fascism" OR "ethnonationalism" OR "white nationalism" OR "Christian nationalism"',
        "lang": "en",
        "country": "US",
    },
    {
        "id": "nodes",
        "filename": "networks.txt",
        "name": "🕸️ Transnational Network Infrastructure",
        "queries": [
            '"Heritage Foundation" OR "Project 2025" OR "American Enterprise Institute" OR "Claremont Institute" OR "Edmund Burke Foundation"',
            '"Danube Institute" OR "Mathias Corvinus Collegium" OR "MCC Budapest" OR "MCC Brussels"',
            '"National Conservatism" OR "NatCon" OR "Conservative Political Action Conference" OR "CPAC" OR "Turning Point USA" OR "Turning Point UK"',
            '"Institute of Economic Affairs" OR "Policy Exchange" OR "Centre for Policy Studies" OR "Alliance for Responsible Citizenship"',
        ],
        "lang": "en",
        "country": "US",
    },
    {
        "id": "hungary",
        "filename": "hungary.txt",
        "name": "🇭🇺 Hungary (Fidesz / Tisza)",
        "q": '"Viktor Orbán" OR "Magyar Péter" OR "Fidesz" OR "Tisza"',
        "lang": "hu",
        "country": "HU",
    },
    {
        "id": "poland",
        "filename": "poland.txt",
        "name": "🇵🇱 Prawo i Sprawiedliwość",
        "q": '"Prawo i Sprawiedliwość" OR "PiS" OR "Jarosław Kaczyński" OR "Mateusz Morawiecki" OR "Karol Nawrocki" OR "Law and Justice"',
        "lang": "pl",
        "country": "PL",
    },
    {
        "id": "spain",
        "filename": "spain.txt",
        "name": "🇪🇸 Vox",
        "q": '"Vox" OR "Santiago Abascal" OR "Ignacio Garriga" OR "Javier Ortega Smith" OR "Rocío Monasterio" OR "Kiko Méndez-Monasterio"',
        "lang": "es",
        "country": "ES",
    },
]

# ── Helpers ────────────────────────────────────────────────────────────────


def build_gnews_url(query: str, feed: dict) -> str:
    win = feed.get("window", "7d")
    lang = feed["lang"]
    country = feed["country"]
    return (
        f"https://news.google.com/rss/search?"
        f"q={quote(query + ' when:' + win)}"
        f"&hl={lang}&gl={country}&ceid={country}:{lang}"
    )


def fetch_rss(url: str, timeout: int = 15) -> list[dict]:
    req = Request(
        url, headers={"User-Agent": "Mozilla/5.0 (compatible; RWMonitor/4.2)"}
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


def get_sort_time(item: dict) -> datetime:
    """Helper to robustly sort items by publication date."""
    date_str = item.get("date", "")
    try:
        # Try to parse the ISO formatted string saved in the JSON
        return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except Exception:
        # Fallback to the exact time the script added it
        added_str = item.get("added_at", "")
        try:
            return datetime.strptime(added_str, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            )
        except Exception:
            return datetime.min.replace(tzinfo=timezone.utc)


# ── Core Logic ─────────────────────────────────────────────────────────────


def fetch_feed(feed: dict, category_seen_urls: set, timestamp: str) -> list[dict]:
    queries = feed.get("queries", [feed.get("q", "")])
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    new_items = []
    errors = []

    for q in queries:
        url = build_gnews_url(q, feed)
        try:
            raw_items = fetch_rss(url)
            for item in raw_items:
                link = item.get("link", "")

                # Check against the category's private memory
                if not link or link in category_seen_urls:
                    continue
                category_seen_urls.add(link)

                pub_dt = item.get("pub_dt")
                if pub_dt and pub_dt < cutoff:
                    continue

                source = extract_source(item.get("title", ""))
                summary = strip_html(item.get("description", ""))
                summary = strip_trailing_source(summary, source)

                new_items.append(
                    {
                        "title": clean_title(item.get("title", "")),
                        "url": link,
                        "source": source,
                        "date": item.get("pubDate", ""),
                        "added_at": timestamp,
                        "summary": summary,
                    }
                )
        except Exception as e:
            errors.append(f"{q[:40]}…: {e}")

    if errors:
        for err in errors[:2]:
            print(f"  ⚠ {err}", file=sys.stderr)

    # Universal cap of new items per run
    return new_items[:MAX_NEW_PER_RUN]


# ── Formatter ──────────────────────────────────────────────────────────────

SEPARATOR = "═" * 72


def format_single_feed(feed: dict, items: list[dict], last_updated: str) -> str:
    lines = []
    lines.append(SEPARATOR)
    lines.append("  TRANSATLANTIC RIGHT-WING MEDIA MONITOR")
    lines.append(f"  Category:     {feed['name']}")
    lines.append(f"  Total Items:  {len(items)}")
    lines.append(f"  Last Updated: {last_updated}")
    lines.append(SEPARATOR)
    lines.append("")

    if not items:
        lines.append("  (no results)")

    for i, item in enumerate(items, 1):
        meta_parts = filter(None, [item.get("source", ""), fmt_date(item["date"])])
        meta = " · ".join(meta_parts)

        lines.append(f"{i}. {item['title']}")
        if meta:
            lines.append(f"   Published: {meta}")

        lines.append(f"   Added:     {item.get('added_at', 'Unknown')}")

        if item["summary"]:
            lines.append(f"   {item['summary']}")
        lines.append(f"   {item['url']}")
        lines.append("")

    return "\n".join(lines)


# ── Main ───────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Stateful media monitor (Multi-File)")
    parser.add_argument(
        "-d",
        "--outdir",
        default="feeds",
        help="Directory to save the individual text files",
    )
    parser.add_argument(
        "--feeds", nargs="*", default=None, help="Specific feeds to run"
    )
    args = parser.parse_args()

    # Create output directory if it doesn't exist
    os.makedirs(args.outdir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    active_feeds = FEEDS
    if args.feeds:
        active_feeds = [f for f in FEEDS if f["id"] in args.feeds]

    # Load the database (if it exists)
    state = {}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
        except Exception:
            pass

    for feed in FEEDS:
        if feed["id"] not in state:
            state[feed["id"]] = []

    print(
        f"[{timestamp}] Fetching {len(active_feeds)} feeds for new articles…",
        file=sys.stderr,
    )

    for feed in active_feeds:
        fid = feed["id"]
        print(f"  → {feed['name']}…", file=sys.stderr)

        # Create a private memory pool just for this specific category
        category_seen_urls = {item["url"] for item in state.get(fid, [])}

        # Pass that private memory to the fetcher
        new_items = fetch_feed(feed, category_seen_urls, timestamp)

        if new_items:
            print(f"    + Found {len(new_items)} new articles!", file=sys.stderr)
            # Add new items to the category's list
            state[fid] = new_items + state[fid]

        # ---------------------------------------------------------
        # NEW FIX: Sort everything chronologically, newest first!
        # ---------------------------------------------------------
        state[fid].sort(key=get_sort_time, reverse=True)

    # Save the updated, strictly sorted database back to JSON
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

    # Rewrite each feed into its own text file
    print("\n[Writing files...]", file=sys.stderr)
    for feed in active_feeds:
        fid = feed["id"]
        items = state.get(fid, [])
        text = format_single_feed(feed, items, timestamp)

        filename = feed.get("filename", f"{fid}.txt")
        filepath = os.path.join(args.outdir, filename)

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"  ✓ Saved {filename}", file=sys.stderr)


if __name__ == "__main__":
    main()
