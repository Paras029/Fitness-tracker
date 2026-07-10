"""One-time repair for nutrient values that got stored as something other
than a clean number (a stray string, NaN, etc.) -- this is what makes
/api/trends and similar aggregate views crash with a TypeError.

Every write path (create_meal, edit_item, add_item, the food cache)
sanitizes on the way in, so this should normally find nothing -- it's a
diagnostic/repair tool for anything that slipped through before that
existed, or from a future write path that forgets to.

    python -m scripts.fix_corrupt_nutrients          # dry run, reports only
    python -m scripts.fix_corrupt_nutrients --apply   # actually fixes them
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import config, db


def scan_and_fix(apply: bool):
    fixed_items = 0
    fixed_cache = 0

    with db.get_conn() as conn:
        rows = conn.execute("SELECT id, name, nutrients_per_100g_json FROM meal_items").fetchall()
        for row in rows:
            raw = json.loads(row["nutrients_per_100g_json"])
            clean = db.sanitize_nutrients(raw)
            if clean != raw:
                dropped = {k: v for k, v in raw.items() if k not in clean or clean[k] != v}
                print(f"  meal_items #{row['id']} ({row['name']!r}): bad values {dropped}")
                fixed_items += 1
                if apply:
                    conn.execute(
                        "UPDATE meal_items SET nutrients_per_100g_json=? WHERE id=?",
                        (json.dumps(clean), row["id"]),
                    )

        rows = conn.execute("SELECT id, label, nutrients_per_100g_json FROM food_cache").fetchall()
        for row in rows:
            raw = json.loads(row["nutrients_per_100g_json"])
            clean = db.sanitize_nutrients(raw)
            if clean != raw:
                dropped = {k: v for k, v in raw.items() if k not in clean or clean[k] != v}
                print(f"  food_cache #{row['id']} ({row['label']!r}): bad values {dropped}")
                fixed_cache += 1
                if apply:
                    conn.execute(
                        "UPDATE food_cache SET nutrients_per_100g_json=? WHERE id=?",
                        (json.dumps(clean), row["id"]),
                    )

    return fixed_items, fixed_cache


if __name__ == "__main__":
    apply = "--apply" in sys.argv
    print(f"Scanning {config.DB_PATH} for bad nutrient values...\n")
    items, cache = scan_and_fix(apply)
    print()
    if items == 0 and cache == 0:
        print("Nothing to fix.")
    elif apply:
        print(f"Fixed {items} meal items and {cache} food_cache rows.")
    else:
        print(f"Found {items} meal items and {cache} food_cache rows with bad values.")
        print("Re-run with --apply to fix them.")
