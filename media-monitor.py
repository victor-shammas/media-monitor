#!/usr/bin/env python3
"""
Transatlantic Right-Wing Media Monitor — Stateful Edition
Maintains a database of seen articles, prevents duplicates, 
and regenerates a cleanly grouped text file on each run.
"""

import json
import os
import re
import sys
import argparse
import html as html_mod
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.request import urlopen, Request
from urllib.parse import quote
from email.utils import parsedate_to_datetime

# ── Configuration ──────────────────────────────────────────────────────────

STATE_FILE = "monitor_state.json"
MAX_NEW_PER_RUN = 15  # The universal limit of new articles to add per run

FEEDS = [
    {
        "id": "maga",
        "name": "🇺🇸 MAGA/Trump",
        "q": '"Donald Trump" OR "Trump administration" OR MAGA OR "JD Vance" OR "Stephen Miller" OR "America First" OR "Steve Bannon"',
        "lang": "en",
        "country": "US",
    },
    {
        "id": "frp",
        "name": "🇳🇴 Fremskrittspartiet",
        "q": 'Fremskrittspartiet OR FrP OR "Sylvi Listhaug" OR "Norwegian Progress Party" OR "Per-Willy Amundsen" OR "Hans Andreas Limi" OR "Simen Velle"',
        "lang": "no",
        "country": "NO",
    },
    {
        "id": "sd",
        "name": "🇸🇪 Sverigedemokraterna",
        "q": 'Sverigedemokraterna OR "Jimmie Åkesson" OR "Sweden Democrats"',
        "lang": "sv",
        "country": "SE",
    },
    {
        "id": "rn",
        "name": "🇫🇷 Rassemblement National",
        "q": '"Rassemblement National" OR "Marine Le Pen" OR "Jordan Bardella" OR "National Rally" OR "Marion Maréchal"',
        "lang": "fr",
        "country": "FR",
    },
    {
        "id": "fdi",
        "name": "🇮🇹 Fratelli d'Italia / Lega",
        "q": "Meloni OR Salvini OR \"Fratelli d'Italia\" OR \"Brothers of Italy\" OR Lega",
        "lang": "it",
        "country": "IT",
    },
    {
        "id": "reform",
        "name": "🇬🇧 Reform UK",
        "q": '"Reform UK" OR "Nigel Farage" OR "Richard Tice"',
        "lang": "en",
        "country": "GB",
    },
    {
        "id": "general",
        "name": "🌍 General Right-Wing News",
        "q": '"far right" OR "alt-right" OR "techno-fascism" OR "manosphere" OR "right-wing extremist" OR "national conservatism" OR "illiberal democracy" OR "fascism" OR "ethnonationalism" OR "white nationalism" OR "Christian nationalism"',
        "lang": "en",
        "country": "US",
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
    },
    {
        "id": "hungary",
        "name": "🇭🇺 Hungary (Fidesz / Tisza)",
        "q": '"Viktor Orban" OR "Peter Magyar" OR "Fidesz" OR "Tisza"',
        "lang": "en",
        "country": "US",
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
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; RWMonitor/3.0)"})
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

        items.append({
            "title": title,
            "link": link,
            "description": desc,
            "pubDate": pub_dt.isoformat() if pub_dt else pub_date_str,
            "pub_dt": pub_dt,
        })
    return items

def strip_html(text: str) -> str:
    if not text: return ""
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
    if not summary or not source: return summary
    esc = re.escape(source)
    return re.sub(r"[\s\-–—]*" + esc + r"\s*$", "", summary, flags=re.IGNORECASE).strip()

def fmt_date(iso: str) -> str:
    if not iso: return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%b %d")
    except Exception:
        return iso[:10]

# ── Core Logic ─────────────────────────────────────────────────────────────

def fetch_feed(feed: dict, seen_urls: set, timestamp: str) -> list[dict]:
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
                
                if not link or link in seen_urls:
                    continue
                seen_urls.add(link)
                
                pub_dt = item.get("pub_dt")
                if pub_dt and pub_dt < cutoff:
                    continue
                    
                source = extract_source(item.get("title", ""))
                summary = strip_html(item.get("description", ""))
                summary = strip_trailing_source(summary, source)
                
                new_items.append({
                    "title": clean_title(item.get("title", "")),
                    "url": link,
                    "source": source,
                    "date": item.get("pubDate", ""),
                    "added_at": timestamp,
                    "summary": summary,
                })
        except Exception as e:
            errors.append(f"{q[:40]}…: {e}")

    if errors:
        for err in errors[:2]:
            print(f"  ⚠ {err}", file=sys.stderr)

    # Universal cap of 15 new items per run
    return new_items[:MAX_NEW_PER_RUN]

# ── Formatter ──────────────────────────────────────────────────────────────

SEPARATOR = "═" * 72

def format_text(state: dict, active_feeds: list[dict], last_updated: str) -> str:
    lines = []
    lines.append(SEPARATOR)
    lines.append("  TRANSATLANTIC RIGHT-WING MEDIA MONITOR")
    lines.append(f"  Last Updated: {last_updated}")
    lines.append(SEPARATOR)

    for feed in active_feeds:
        fid = feed["id"]
        items = state.get(fid, [])
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
                lines.append(f"   Published: {meta}")
            
            lines.append(f"   Added:     {item.get('added_at', 'Unknown')}")
            
            if item["summary"]:
                lines.append(f"   {item['summary']}")
            lines.append(f"   {item['url']}")
            lines.append("")

    return "\n".join(lines)

# ── Main ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Stateful media monitor")
    parser.add_argument("-o", "--output", default="monitor_output.txt", help="Text output file")
    parser.add_argument("--feeds", nargs="*", default=None, help="Specific feeds to run")
    args = parser.parse_args()

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

    # Build a giant list of every URL we've ever seen to prevent duplicates
    seen_urls = set()
    for items in state.values():
        for item in items:
            seen_urls.add(item["url"])

    print(f"[{timestamp}] Found {len(seen_urls)} previously saved articles.", file=sys.stderr)
    print(f"[{timestamp}] Fetching {len(active_feeds)} feeds for new articles…", file=sys.stderr)
    
    for feed in active_feeds:
        fid = feed["id"]
        print(f"  → {feed['name']}…", file=sys.stderr)
        
        new_items = fetch_feed(feed, seen_urls, timestamp)
        
        if new_items:
            print(f"    + Found {len(new_items)} new articles!", file=sys.stderr)
            # Add new items to the top of the category's list
            state[fid] = new_items + state[fid]

    # Save the updated database back to JSON
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

    # Completely rewrite the readable Text file using the updated database
    text = format_text(state, active_feeds, timestamp)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(text + "\n")
        
    print(f"\n[Updated {args.output} and {STATE_FILE}]", file=sys.stderr)

if __name__ == "__main__":
    main()
