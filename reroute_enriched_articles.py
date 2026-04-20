"""One-off script to re-route enriched articles into files matching their publication date."""

import json
import glob
import os
from datetime import datetime

ENRICHED_DIR = "enriched"


def publication_date_slug(item: dict) -> str:
    date_str = item.get("date", "")
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return None


def main():
    files = sorted(glob.glob(os.path.join(ENRICHED_DIR, "enriched_*.json")))
    if not files:
        print("No enriched files found.")
        return

    # Load all articles, grouped by their file's date slug
    articles_by_file_date: dict[str, list[dict]] = {}
    for path in files:
        slug = os.path.basename(path).replace("enriched_", "").replace(".json", "")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        articles_by_file_date[slug] = data.get("articles", [])

    # Re-route by publication date
    articles_by_pub_date: dict[str, list[dict]] = {}
    moved = 0
    for file_slug, articles in articles_by_file_date.items():
        for a in articles:
            pub_slug = publication_date_slug(a) or file_slug
            articles_by_pub_date.setdefault(pub_slug, [])
            articles_by_pub_date[pub_slug].append(a)
            if pub_slug != file_slug:
                moved += 1

    # Write updated files
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for slug, articles in sorted(articles_by_pub_date.items()):
        outpath = os.path.join(ENRICHED_DIR, f"enriched_{slug}.json")
        output = {
            "date": slug,
            "last_updated": timestamp,
            "stats": {
                "total_articles": len(articles),
            },
            "articles": articles,
        }
        with open(outpath, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, ensure_ascii=False)
        existed = slug in articles_by_file_date
        print(f"  enriched_{slug}.json: {len(articles)} articles {'(updated)' if existed else '(new)'}")

    print(f"\nDone. Moved {moved} articles to their correct date files.")


if __name__ == "__main__":
    main()
