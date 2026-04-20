"""One-off script to rename category IDs in monitor_state.json and enriched JSONs."""

import json
import glob
import os

RENAMES = {
    "maga": "usa",
    "frp": "norway",
    "sd": "sweden",
    "rn": "france",
    "fdi": "italy",
    "reform": "uk",
    "afd": "germany",
    "nodes": "networks",
}


def migrate_state(path="monitor_state.json"):
    if not os.path.exists(path):
        print(f"  {path}: not found, skipping")
        return

    with open(path, "r", encoding="utf-8") as f:
        state = json.load(f)

    changed = 0
    for old_id, new_id in RENAMES.items():
        if old_id in state:
            state[new_id] = state.pop(old_id)
            changed += 1

    if changed:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        print(f"  {path}: renamed {changed} category keys")
    else:
        print(f"  {path}: no changes needed")


def migrate_enriched(enriched_dir="enriched"):
    files = sorted(glob.glob(os.path.join(enriched_dir, "enriched_*.json")))
    if not files:
        print("  No enriched files found")
        return

    for path in files:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        changed = 0
        for article in data.get("articles", []):
            old_cat = article.get("category", "")
            if old_cat in RENAMES:
                article["category"] = RENAMES[old_cat]
                changed += 1

        if changed:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        print(f"  {os.path.basename(path)}: {changed} articles updated")


def main():
    print("Migrating category IDs...")
    print()
    print("State file:")
    migrate_state()
    print()
    print("Enriched files:")
    migrate_enriched()
    print()
    print("Done.")


if __name__ == "__main__":
    main()
