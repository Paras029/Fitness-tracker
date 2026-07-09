"""SQLite access layer, shared by the Telegram bot and the Flask API.

Both processes open the same file with WAL journaling enabled, which lets
one write while the other reads without locking errors under this app's
light, single-user load.
"""

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta

from core import config

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
    nutrients_json TEXT NOT NULL,
    use_count INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_food_cache_match_text ON food_cache(match_text);

CREATE TABLE IF NOT EXISTS log_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    logged_at TEXT NOT NULL,
    log_date TEXT NOT NULL,
    meal_slot TEXT NOT NULL,
    name TEXT NOT NULL,
    nutrients_json TEXT NOT NULL,
    source TEXT NOT NULL,
    confidence TEXT NOT NULL,             -- 'database' | 'estimated' | 'user_corrected'
    raw_input TEXT,
    food_cache_id INTEGER REFERENCES food_cache(id)
);
CREATE INDEX IF NOT EXISTS idx_log_entries_date ON log_entries(log_date);

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


def init_db():
    with get_conn() as conn:
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


# ---------------- food cache ----------------

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


def upsert_food_cache(match_text, label, source, nutrients, cache_id=None):
    payload = json.dumps(nutrients)
    ts = now_iso()
    with get_conn() as conn:
        if cache_id:
            conn.execute(
                "UPDATE food_cache SET label=?, source=?, nutrients_json=?, "
                "use_count=use_count+1, updated_at=? WHERE id=?",
                (label, source, payload, ts, cache_id),
            )
            return cache_id
        cur = conn.execute(
            "INSERT INTO food_cache (match_text, label, source, nutrients_json, "
            "use_count, created_at, updated_at) VALUES (?,?,?,?,1,?,?)",
            (match_text, label, source, payload, ts, ts),
        )
        return cur.lastrowid


# ---------------- log entries ----------------

def insert_log_entry(name, nutrients, meal_slot, source, confidence, raw_input=None,
                      food_cache_id=None, log_date=None):
    ts = now_iso()
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO log_entries (logged_at, log_date, meal_slot, name, nutrients_json, "
            "source, confidence, raw_input, food_cache_id) VALUES (?,?,?,?,?,?,?,?,?)",
            (ts, log_date or today_str(), meal_slot, name, json.dumps(nutrients),
             source, confidence, raw_input, food_cache_id),
        )
        return cur.lastrowid


def delete_log_entry(entry_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM log_entries WHERE id=?", (entry_id,))


def get_day_entries(log_date):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM log_entries WHERE log_date=? ORDER BY logged_at", (log_date,)
        ).fetchall()
        out = []
        for row in rows:
            d = dict(row)
            d["nutrients"] = json.loads(d.pop("nutrients_json"))
            out.append(d)
        return out


def get_range_entries(start_date, end_date):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM log_entries WHERE log_date BETWEEN ? AND ? ORDER BY log_date, logged_at",
            (start_date, end_date),
        ).fetchall()
        out = []
        for row in rows:
            d = dict(row)
            d["nutrients"] = json.loads(d.pop("nutrients_json"))
            out.append(d)
        return out


def day_totals(log_date):
    entries = get_day_entries(log_date)
    totals = {}
    for e in entries:
        for k, v in e["nutrients"].items():
            totals[k] = totals.get(k, 0) + (v or 0)
    return totals, entries


# ---------------- saved meals ----------------

def save_meal(name, items):
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
