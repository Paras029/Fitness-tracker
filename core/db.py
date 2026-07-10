"""SQLite access layer, shared by the Telegram bot and the Flask API.

Both processes open the same file with WAL journaling enabled, which lets
one write while the other reads without locking errors under this app's
light, single-user load.

Data model: a `meal` is one logged eating event (e.g. "lunch, 1:15pm").
Each meal has `meal_items` -- its ingredients. Every item stores nutrients
PER 100G plus a `grams` quantity, never a pre-multiplied absolute value.
That's what makes "edit the weight" a trivial recompute instead of a
special case: absolute nutrients for an item are always
`nutrients_per_100g * grams / 100`, computed on read, never stored
redundantly. The food cache follows the same convention so a cached hit
and a fresh API hit are interchangeable.
"""

import json
import math
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta

from core import config


def safe_num(v, default=0):
    """Coerces a stored/incoming nutrient value to a finite float, or
    `default` if it isn't one (None, a stray string, NaN, etc.)."""
    try:
        f = float(v)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def sanitize_nutrients(nutrients):
    """Drops (doesn't zero out) any non-numeric value before it's written,
    so bad data from any source can't corrupt storage or crash aggregate
    endpoints later. Missing stays missing rather than becoming a
    misleading 0."""
    out = {}
    for k, v in (nutrients or {}).items():
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(f):
            out[k] = f
    return out


SCHEMA = """
CREATE TABLE IF NOT EXISTS nutrient_defs (
    key TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    unit TEXT NOT NULL,
    category TEXT NOT NULL,              -- 'macro' | 'micro' | 'other'
    direction TEXT NOT NULL DEFAULT 'higher_better',  -- 'higher_better' | 'lower_better'
    target_mode TEXT NOT NULL DEFAULT 'flat',         -- 'flat' | 'pct_kcal' | 'per_kg_bodyweight'
    target_value REAL,
    enabled INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS profile (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS food_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_text TEXT NOT NULL,
    label TEXT NOT NULL,
    source TEXT NOT NULL,
    nutrients_per_100g_json TEXT NOT NULL,
    use_count INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_food_cache_match_text ON food_cache(match_text);

CREATE TABLE IF NOT EXISTS meals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    log_date TEXT NOT NULL,
    logged_at TEXT NOT NULL,
    meal_slot TEXT NOT NULL,              -- breakfast | lunch | dinner | snack | water
    label TEXT NOT NULL,
    raw_input TEXT,
    extraction_confidence TEXT,           -- high | medium | low | null
    rating_score INTEGER,
    rating_label TEXT,
    rating_note TEXT
);
CREATE INDEX IF NOT EXISTS idx_meals_date ON meals(log_date);

CREATE TABLE IF NOT EXISTS meal_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meal_id INTEGER NOT NULL REFERENCES meals(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    grams REAL NOT NULL DEFAULT 100,
    nutrients_per_100g_json TEXT NOT NULL,
    source TEXT NOT NULL,                 -- cache | calorieninjas | usda | openfoodfacts | gemini_estimate
    confidence TEXT NOT NULL,             -- database | llm_filled | llm_estimated | user_corrected
    sort_order INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_meal_items_meal ON meal_items(meal_id);

CREATE TABLE IF NOT EXISTS saved_meals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    items_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_confirms (
    token TEXT PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    items_json TEXT NOT NULL,
    meal_slot TEXT,
    created_at TEXT NOT NULL
);
"""

DEFAULT_NUTRIENTS = [
    # key, label, unit, category, direction, target_mode, target_value, sort_order
    ("kcal", "Calories", "kcal", "macro", "higher_better", "flat", 2200, 0),
    ("protein", "Protein", "g", "macro", "higher_better", "per_kg_bodyweight", 1.6, 1),
    ("carbs", "Carbs", "g", "macro", "higher_better", "pct_kcal", 45, 2),
    ("fat", "Fat", "g", "macro", "higher_better", "pct_kcal", 30, 3),
    ("fiber", "Fiber", "g", "macro", "higher_better", "flat", 30, 4),
    ("sugar", "Sugar", "g", "micro", "lower_better", "flat", 50, 5),
    ("sodium", "Sodium", "mg", "micro", "lower_better", "flat", 2300, 6),
    ("potassium", "Potassium", "mg", "micro", "higher_better", "flat", 3400, 7),
    ("vitamin_c", "Vitamin C", "mg", "micro", "higher_better", "flat", 90, 8),
    ("iron", "Iron", "mg", "micro", "higher_better", "flat", 18, 9),
    ("calcium", "Calcium", "mg", "micro", "higher_better", "flat", 1000, 10),
    ("vitamin_d", "Vitamin D", "mcg", "micro", "higher_better", "flat", 20, 11),
]

DEFAULT_PROFILE = {
    "bodyweight_kg": "70",
    "timezone_offset_hours": str(config.LOCAL_UTC_OFFSET_HOURS),
}


@contextmanager
def get_conn():
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _migrate_legacy_schema(conn):
    """One-time upgrade from the old flat log_entries table (absolute
    nutrients, no grams, no meal grouping) to meals/meal_items. Each old
    row becomes its own single-item meal. We don't know what portion size
    the old absolute values represented, so they're carried over as a
    best-effort "grams=100" record -- historical totals still display
    correctly; editing the weight on a migrated item is approximate until
    it's re-logged through the new pipeline.

    food_cache/saved_meals/pending_confirms stored absolute-serving data
    under the old schema, which can't be safely reinterpreted as per-100g
    -- they're reset instead of migrated, and rebuild automatically
    through normal use.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS meals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            log_date TEXT NOT NULL,
            logged_at TEXT NOT NULL,
            meal_slot TEXT NOT NULL,
            label TEXT NOT NULL,
            raw_input TEXT,
            extraction_confidence TEXT,
            rating_score INTEGER,
            rating_label TEXT,
            rating_note TEXT
        );
        CREATE TABLE IF NOT EXISTS meal_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            meal_id INTEGER NOT NULL REFERENCES meals(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            grams REAL NOT NULL DEFAULT 100,
            nutrients_per_100g_json TEXT NOT NULL,
            source TEXT NOT NULL,
            confidence TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0
        );
    """)

    rows = conn.execute("SELECT * FROM log_entries ORDER BY logged_at").fetchall()
    migrated = 0
    for row in rows:
        nutrients = sanitize_nutrients(json.loads(row["nutrients_json"]))
        cur = conn.execute(
            "INSERT INTO meals (log_date, logged_at, meal_slot, label, raw_input, extraction_confidence) "
            "VALUES (?,?,?,?,?,?)",
            (row["log_date"], row["logged_at"], row["meal_slot"], row["name"],
             row["raw_input"], "migrated"),
        )
        conn.execute(
            "INSERT INTO meal_items (meal_id, name, grams, nutrients_per_100g_json, source, confidence) "
            "VALUES (?,?,100,?,?,?)",
            (cur.lastrowid, row["name"], json.dumps(nutrients), row["source"], row["confidence"]),
        )
        migrated += 1

    conn.execute("DROP TABLE log_entries")
    for legacy_table in ("food_cache", "saved_meals", "pending_confirms"):
        conn.execute(f"DROP TABLE IF EXISTS {legacy_table}")

    print(f"[db migration] moved {migrated} old log entries into the new meals/meal_items schema; "
          f"food_cache/saved_meals/pending_confirms were reset (they rebuild automatically).")


def init_db():
    with get_conn() as conn:
        tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "log_entries" in tables and "meals" not in tables:
            _migrate_legacy_schema(conn)

        conn.executescript(SCHEMA)
        existing = {row["key"] for row in conn.execute("SELECT key FROM nutrient_defs")}
        for key, label, unit, category, direction, mode, value, order_ in DEFAULT_NUTRIENTS:
            if key in existing:
                continue
            conn.execute(
                "INSERT INTO nutrient_defs (key, label, unit, category, direction, "
                "target_mode, target_value, enabled, sort_order) VALUES (?,?,?,?,?,?,?,1,?)",
                (key, label, unit, category, direction, mode, value, order_),
            )
        existing_profile = {row["key"] for row in conn.execute("SELECT key FROM profile")}
        for key, value in DEFAULT_PROFILE.items():
            if key in existing_profile:
                continue
            conn.execute("INSERT INTO profile (key, value) VALUES (?, ?)", (key, value))


def now_iso():
    return datetime.utcnow().isoformat(timespec="seconds")


def today_str():
    offset = get_profile_float("timezone_offset_hours", config.LOCAL_UTC_OFFSET_HOURS)
    local_now = datetime.utcnow() + timedelta(hours=offset)
    return local_now.strftime("%Y-%m-%d")


# ---------------- profile ----------------

def get_profile(key, default=None):
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM profile WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def get_profile_float(key, default=0.0):
    val = get_profile(key)
    try:
        return float(val) if val is not None else default
    except (TypeError, ValueError):
        return default


def set_profile(key, value):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO profile (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )


# ---------------- nutrient defs ----------------

def list_nutrient_defs(enabled_only=False):
    with get_conn() as conn:
        q = "SELECT * FROM nutrient_defs"
        if enabled_only:
            q += " WHERE enabled=1"
        q += " ORDER BY sort_order"
        return [dict(row) for row in conn.execute(q)]


def create_nutrient_def(key, label, unit, category="other", direction="higher_better",
                         target_mode="flat", target_value=0, sort_order=None):
    """Adds a custom nutrient (e.g. "Omega-3", "Caffeine"). Once created it
    behaves exactly like a built-in one -- shows up in day summaries, the
    dashboard, and weekly reports as soon as anything logs a value for it."""
    key = key.strip().lower().replace(" ", "_")
    if not key:
        raise ValueError("nutrient key cannot be empty")
    with get_conn() as conn:
        if sort_order is None:
            row = conn.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 AS n FROM nutrient_defs").fetchone()
            sort_order = row["n"]
        conn.execute(
            "INSERT INTO nutrient_defs (key, label, unit, category, direction, "
            "target_mode, target_value, enabled, sort_order) VALUES (?,?,?,?,?,?,?,1,?)",
            (key, label, unit, category, direction, target_mode, target_value, sort_order),
        )
    return key


def update_nutrient_def(key, **fields):
    if not fields:
        return
    allowed = {"label", "unit", "category", "direction", "target_mode", "target_value", "enabled", "sort_order"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    with get_conn() as conn:
        cols = ", ".join(f"{k}=?" for k in sets)
        conn.execute(f"UPDATE nutrient_defs SET {cols} WHERE key=?", (*sets.values(), key))


def resolve_target(nutrient_def):
    """Resolve a nutrient_def row (dict) to an effective numeric target for today."""
    mode = nutrient_def["target_mode"]
    value = nutrient_def["target_value"] or 0
    if mode == "flat":
        return value
    if mode == "per_kg_bodyweight":
        return round(value * get_profile_float("bodyweight_kg", 70), 1)
    if mode == "pct_kcal":
        kcal_def = next((d for d in list_nutrient_defs() if d["key"] == "kcal"), None)
        kcal_target = kcal_def["target_value"] if kcal_def else 2200
        grams_per_kcal = 4 if nutrient_def["key"] != "fat" else 9
        return round((kcal_target * (value / 100)) / grams_per_kcal, 1)
    return value


# ---------------- food cache (always per-100g) ----------------

def search_food_cache_exact(match_text):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM food_cache WHERE match_text=? ORDER BY use_count DESC LIMIT 1",
            (match_text,),
        ).fetchone()
        return dict(row) if row else None


def all_food_cache():
    with get_conn() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM food_cache")]


def upsert_food_cache(match_text, label, source, nutrients_per_100g, cache_id=None):
    payload = json.dumps(sanitize_nutrients(nutrients_per_100g))
    ts = now_iso()
    with get_conn() as conn:
        if cache_id:
            conn.execute(
                "UPDATE food_cache SET label=?, source=?, nutrients_per_100g_json=?, "
                "use_count=use_count+1, updated_at=? WHERE id=?",
                (label, source, payload, ts, cache_id),
            )
            return cache_id
        cur = conn.execute(
            "INSERT INTO food_cache (match_text, label, source, nutrients_per_100g_json, "
            "use_count, created_at, updated_at) VALUES (?,?,?,?,1,?,?)",
            (match_text, label, source, payload, ts, ts),
        )
        return cur.lastrowid


# ---------------- meals & items ----------------

def item_absolute_nutrients(item):
    """item: dict with 'grams' and 'nutrients_per_100g'. Returns the
    nutrients for the actual logged quantity."""
    factor = safe_num(item.get("grams"), 100) / 100.0
    return {k: round(safe_num(v) * factor, 2) for k, v in (item.get("nutrients_per_100g") or {}).items()}


def _row_to_item(row):
    d = dict(row)
    d["nutrients_per_100g"] = json.loads(d.pop("nutrients_per_100g_json"))
    d["nutrients"] = item_absolute_nutrients(d)
    return d


def _row_to_meal(row, items):
    d = dict(row)
    d["items"] = items
    totals = {}
    for it in items:
        for k, v in it["nutrients"].items():
            totals[k] = round(totals.get(k, 0) + v, 2)
    d["totals"] = totals
    return d


def create_meal(meal_slot, items, label=None, raw_input=None, extraction_confidence=None, log_date=None):
    """items: list of {name, grams, nutrients_per_100g, source, confidence}.
    Returns the created meal (full dict, with computed totals)."""
    ts = now_iso()
    date = log_date or today_str()
    display_label = label or (items[0]["name"] if items else meal_slot.title())
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO meals (log_date, logged_at, meal_slot, label, raw_input, extraction_confidence) "
            "VALUES (?,?,?,?,?,?)",
            (date, ts, meal_slot, display_label, raw_input, extraction_confidence),
        )
        meal_id = cur.lastrowid
        for i, it in enumerate(items):
            conn.execute(
                "INSERT INTO meal_items (meal_id, name, grams, nutrients_per_100g_json, source, confidence, sort_order) "
                "VALUES (?,?,?,?,?,?,?)",
                (meal_id, it["name"], safe_num(it.get("grams"), 100),
                 json.dumps(sanitize_nutrients(it.get("nutrients_per_100g"))),
                 it.get("source", "user"), it.get("confidence", "estimated"), i),
            )
    return get_meal(meal_id)


def get_meal(meal_id):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM meals WHERE id=?", (meal_id,)).fetchone()
        if not row:
            return None
        item_rows = conn.execute(
            "SELECT * FROM meal_items WHERE meal_id=? ORDER BY sort_order, id", (meal_id,)
        ).fetchall()
    items = [_row_to_item(r) for r in item_rows]
    return _row_to_meal(row, items)


def get_day_meals(log_date):
    with get_conn() as conn:
        meal_rows = conn.execute(
            "SELECT * FROM meals WHERE log_date=? ORDER BY logged_at", (log_date,)
        ).fetchall()
        meals = []
        for mrow in meal_rows:
            item_rows = conn.execute(
                "SELECT * FROM meal_items WHERE meal_id=? ORDER BY sort_order, id", (mrow["id"],)
            ).fetchall()
            meals.append(_row_to_meal(mrow, [_row_to_item(r) for r in item_rows]))
    return meals


def get_range_meals(start_date, end_date):
    with get_conn() as conn:
        meal_rows = conn.execute(
            "SELECT * FROM meals WHERE log_date BETWEEN ? AND ? ORDER BY log_date, logged_at",
            (start_date, end_date),
        ).fetchall()
        meals = []
        for mrow in meal_rows:
            item_rows = conn.execute(
                "SELECT * FROM meal_items WHERE meal_id=? ORDER BY sort_order, id", (mrow["id"],)
            ).fetchall()
            meals.append(_row_to_meal(mrow, [_row_to_item(r) for r in item_rows]))
    return meals


def day_totals(log_date):
    meals = get_day_meals(log_date)
    totals = {}
    for m in meals:
        for k, v in m["totals"].items():
            totals[k] = round(totals.get(k, 0) + v, 2)
    return totals, meals


def update_meal_item(item_id, grams=None, name=None, nutrients_per_100g=None, confidence=None):
    """Editing grams alone rescales every nutrient for that item automatically
    (nothing else needs to change -- absolute values are always computed,
    never stored)."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM meal_items WHERE id=?", (item_id,)).fetchone()
        if not row:
            return None
        new_grams = safe_num(grams, row["grams"]) if grams is not None else row["grams"]
        new_name = name if name is not None else row["name"]
        if nutrients_per_100g is not None:
            new_nutrients = json.dumps(sanitize_nutrients(nutrients_per_100g))
        else:
            new_nutrients = row["nutrients_per_100g_json"]
        new_confidence = confidence or "user_corrected"
        conn.execute(
            "UPDATE meal_items SET grams=?, name=?, nutrients_per_100g_json=?, confidence=? WHERE id=?",
            (new_grams, new_name, new_nutrients, new_confidence, item_id),
        )
        meal_id = row["meal_id"]
    return meal_id


def delete_meal_item(item_id):
    """Deletes an item; if it was the last one in its meal, deletes the
    (now-empty) meal too. Returns the meal_id (deleted or still-alive)."""
    with get_conn() as conn:
        row = conn.execute("SELECT meal_id FROM meal_items WHERE id=?", (item_id,)).fetchone()
        if not row:
            return None
        meal_id = row["meal_id"]
        conn.execute("DELETE FROM meal_items WHERE id=?", (item_id,))
        remaining = conn.execute("SELECT COUNT(*) AS n FROM meal_items WHERE meal_id=?", (meal_id,)).fetchone()["n"]
        if remaining == 0:
            conn.execute("DELETE FROM meals WHERE id=?", (meal_id,))
    return meal_id


def add_meal_item(meal_id, name, grams, nutrients_per_100g, source="user", confidence="estimated"):
    with get_conn() as conn:
        next_order = conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM meal_items WHERE meal_id=?", (meal_id,)
        ).fetchone()["n"]
        cur = conn.execute(
            "INSERT INTO meal_items (meal_id, name, grams, nutrients_per_100g_json, source, confidence, sort_order) "
            "VALUES (?,?,?,?,?,?,?)",
            (meal_id, name, safe_num(grams, 100), json.dumps(sanitize_nutrients(nutrients_per_100g)),
             source, confidence, next_order),
        )
        return cur.lastrowid


def delete_meal(meal_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM meals WHERE id=?", (meal_id,))  # cascades to meal_items


def set_meal_rating(meal_id, score, label, note):
    with get_conn() as conn:
        conn.execute(
            "UPDATE meals SET rating_score=?, rating_label=?, rating_note=? WHERE id=?",
            (score, label, note, meal_id),
        )


# ---------------- saved meals ----------------

def save_meal(name, items):
    """items: [{name, grams, nutrients_per_100g}, ...]"""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO saved_meals (name, items_json, created_at) VALUES (?,?,?)",
            (name, json.dumps(items), now_iso()),
        )
        return cur.lastrowid


def list_saved_meals():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM saved_meals ORDER BY created_at DESC").fetchall()
        out = []
        for row in rows:
            d = dict(row)
            d["items"] = json.loads(d.pop("items_json"))
            out.append(d)
        return out


# ---------------- pending confirms (Telegram inline-keyboard handoff) ----------------

def create_pending_confirm(chat_id, items, meal_slot=None):
    token = uuid.uuid4().hex[:10]
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO pending_confirms (token, chat_id, items_json, meal_slot, created_at) "
            "VALUES (?,?,?,?,?)",
            (token, chat_id, json.dumps(items), meal_slot, now_iso()),
        )
    return token


def pop_pending_confirm(token):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM pending_confirms WHERE token=?", (token,)).fetchone()
        if not row:
            return None
        conn.execute("DELETE FROM pending_confirms WHERE token=?", (token,))
        d = dict(row)
        d["items"] = json.loads(d.pop("items_json"))
        return d


def peek_pending_confirm(token):
    """Same shape as pop_pending_confirm but doesn't delete -- for the
    Double-check / reply-to-correct flow, which needs to read the pending
    draft without ending the confirmation (the user still has to tap a
    meal-slot button, or send another correction, afterward)."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM pending_confirms WHERE token=?", (token,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["items"] = json.loads(d.pop("items_json"))
        return d


def update_pending_confirm(token, items):
    """Overwrites a pending draft's contents in place -- used after a
    conversational refine, so the eventual meal-slot tap confirms the
    corrected draft, not the stale one the message was first sent with."""
    with get_conn() as conn:
        conn.execute("UPDATE pending_confirms SET items_json=? WHERE token=?", (json.dumps(items), token))
