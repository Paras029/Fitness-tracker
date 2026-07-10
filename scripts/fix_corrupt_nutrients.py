"""One-time repair for nutrient values that got stored as something other
than a clean number (a stray string, NaN, etc.) -- this is what makes
/api/trends and similar aggregate views crash with a TypeError.

The code that writes new entries now sanitizes on the way in, so this is
only needed once, to clean up rows written before that fix.

    python -m scripts.fix_corrupt_nutrients          # dry run, reports only
    python -m scripts.fix_corrupt_nutrients --apply   # actually fixes them
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import config, db


def scan_and_fix(apply: bool):
    fixed_entries = 0
    fixed_cache = 0

    with db.get_conn() as conn:
        rows = conn.execute("SELECT id, name, nutrients_json FROM log_entries").fetchall()
        for row in rows:
            raw = json.loads(row["nutrients_json"])
            clean = db.sanitize_nutrients(raw)
            if clean != raw:
                dropped = {k: v for k, v in raw.items() if k not in clean or clean[k] != v}
                print(f"  log_entries #{row['id']} ({row['name']!r}): bad values {dropped}")
                fixed_entries += 1
                if apply:
                    conn.execute(
                        "UPDATE log_entries SET nutrients_json=? WHERE id=?",
                        (json.dumps(clean), row["id"]),
                    )

        rows = conn.execute("SELECT id, label, nutrients_json FROM food_cache").fetchall()
        for row in rows:
            raw = json.loads(row["nutrients_json"])
            clean = db.sanitize_nutrients(raw)
            if clean != raw:
                dropped = {k: v for k, v in raw.items() if k not in clean or clean[k] != v}
                print(f"  food_cache #{row['id']} ({row['label']!r}): bad values {dropped}")
                fixed_cache += 1
                if apply:
                    conn.execute(
                        "UPDATE food_cache SET nutrients_json=? WHERE id=?",
                        (json.dumps(clean), row["id"]),
                    )

    return fixed_entries, fixed_cache


if __name__ == "__main__":
    apply = "--apply" in sys.argv
    print(f"Scanning {config.DB_PATH} for bad nutrient values...\n")
    entries, cache = scan_and_fix(apply)
    print()
    if entries == 0 and cache == 0:
        print("Nothing to fix.")
    elif apply:
        print(f"Fixed {entries} log entries and {cache} food_cache rows.")
    else:
        print(f"Found {entries} log entries and {cache} food_cache rows with bad values.")
        print("Re-run with --apply to fix them.")
