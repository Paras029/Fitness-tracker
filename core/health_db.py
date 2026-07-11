"""SQLite access layer for the Health & Body domain (body composition,
lab results, other documents, water) -- kept separate from db.py so the
nutrition schema/queries stay uncluttered. Shares the same connection
helper and time helpers as db.py rather than reimplementing them; both
modules read/write the same SQLite file.
"""

import json

from core import db

HEALTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS body_comp_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    log_date TEXT NOT NULL,
    logged_at TEXT NOT NULL,
    weight_kg REAL,
    body_fat_pct REAL,
    skeletal_muscle_kg REAL,
    visceral_fat REAL,
    bmr REAL,
    body_water_pct REAL,
    source TEXT NOT NULL DEFAULT 'manual',   -- manual | photo | pdf
    note TEXT,
    segments_json TEXT,    -- {"right_arm":{"lean_kg":..,"fat_kg":..}, "left_arm":.., "trunk":.., "right_leg":.., "left_leg":..}
    ai_score INTEGER,
    ai_summary TEXT,
    ai_highlights_json TEXT,
    ai_watch TEXT,
    ai_generated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_body_comp_date ON body_comp_entries(log_date);

CREATE TABLE IF NOT EXISTS lab_categories (
    key TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS lab_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uploaded_at TEXT NOT NULL,
    log_date TEXT NOT NULL,
    file_path TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    label TEXT,
    raw_extraction_json TEXT
);

CREATE TABLE IF NOT EXISTS lab_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL REFERENCES lab_reports(id) ON DELETE CASCADE,
    category_key TEXT NOT NULL REFERENCES lab_categories(key),
    test_name TEXT NOT NULL,
    value REAL,
    unit TEXT,
    ref_low REAL,
    ref_high REAL,
    ref_text TEXT,
    flag TEXT NOT NULL DEFAULT 'normal',   -- normal | low | high
    test_date TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lab_results_test ON lab_results(test_name);
CREATE INDEX IF NOT EXISTS idx_lab_results_report ON lab_results(report_id);

CREATE TABLE IF NOT EXISTS other_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uploaded_at TEXT NOT NULL,
    log_date TEXT NOT NULL,
    file_path TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    label TEXT NOT NULL,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS water_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    log_date TEXT NOT NULL,
    logged_at TEXT NOT NULL,
    ml REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_water_logs_date ON water_logs(log_date);

CREATE TABLE IF NOT EXISTS supplements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    dose_amount REAL,
    dose_unit TEXT,
    category TEXT NOT NULL DEFAULT 'other',   -- vitamin | mineral | medicine | other
    linked_nutrient_key TEXT,                 -- optional: nutrient_defs.key this dose counts toward
    enabled INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS supplement_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    supplement_id INTEGER NOT NULL REFERENCES supplements(id) ON DELETE CASCADE,
    log_date TEXT NOT NULL,
    logged_at TEXT NOT NULL,
    dose_amount REAL,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_supplement_logs_date ON supplement_logs(log_date);
CREATE INDEX IF NOT EXISTS idx_supplement_logs_supplement ON supplement_logs(supplement_id);
"""

DEFAULT_LAB_CATEGORIES = [
    # key, label, sort_order
    ("heart", "Heart health", 0),
    ("kidney", "Kidney health", 1),
    ("liver", "Liver health", 2),
    ("cbc", "CBC", 3),
    ("urine", "Urine", 4),
    ("other", "Other", 5),
]


# Columns added after the table's first release -- CREATE TABLE IF NOT
# EXISTS won't backfill these onto an already-created table, so any
# install that ran init_health_db() before segments/AI-summary support
# existed needs this one-time ALTER TABLE ADD COLUMN pass. Safe to run
# every startup: it only adds what's actually missing.
_BODY_COMP_NEW_COLUMNS = [
    ("segments_json", "TEXT"), ("ai_score", "INTEGER"), ("ai_summary", "TEXT"),
    ("ai_highlights_json", "TEXT"), ("ai_watch", "TEXT"), ("ai_generated_at", "TEXT"),
]


def _migrate_body_comp_columns(conn):
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "body_comp_entries" not in tables:
        return
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(body_comp_entries)")}
    for col, col_type in _BODY_COMP_NEW_COLUMNS:
        if col not in existing:
            conn.execute(f"ALTER TABLE body_comp_entries ADD COLUMN {col} {col_type}")


def init_health_db():
    with db.get_conn() as conn:
        _migrate_body_comp_columns(conn)
        conn.executescript(HEALTH_SCHEMA)
        existing = {row["key"] for row in conn.execute("SELECT key FROM lab_categories")}
        for key, label, order_ in DEFAULT_LAB_CATEGORIES:
            if key in existing:
                continue
            conn.execute(
                "INSERT INTO lab_categories (key, label, enabled, sort_order) VALUES (?,?,1,?)",
                (key, label, order_),
            )


# ---------------- body composition ----------------

def create_body_comp_entry(weight_kg=None, body_fat_pct=None, skeletal_muscle_kg=None,
                            visceral_fat=None, bmr=None, body_water_pct=None,
                            source="manual", note=None, log_date=None, segments=None):
    date = log_date or db.today_str()
    with db.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO body_comp_entries (log_date, logged_at, weight_kg, body_fat_pct, "
            "skeletal_muscle_kg, visceral_fat, bmr, body_water_pct, source, note, segments_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (date, db.now_iso(), weight_kg, body_fat_pct, skeletal_muscle_kg,
             visceral_fat, bmr, body_water_pct, source, note,
             json.dumps(segments) if segments else None),
        )
        return cur.lastrowid


def _row_to_body_comp(row):
    d = dict(row)
    segments_json = d.pop("segments_json")
    d["segments"] = json.loads(segments_json) if segments_json else None
    highlights_json = d.pop("ai_highlights_json")
    d["ai_highlights"] = json.loads(highlights_json) if highlights_json else None
    return d


def list_body_comp_entries(limit=None):
    q = "SELECT * FROM body_comp_entries ORDER BY log_date DESC, logged_at DESC"
    if limit:
        q += f" LIMIT {int(limit)}"
    with db.get_conn() as conn:
        return [_row_to_body_comp(row) for row in conn.execute(q)]


def get_body_comp_entry(entry_id):
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM body_comp_entries WHERE id=?", (entry_id,)).fetchone()
        return _row_to_body_comp(row) if row else None


def set_body_comp_summary(entry_id, score, summary, highlights, watch):
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE body_comp_entries SET ai_score=?, ai_summary=?, ai_highlights_json=?, "
            "ai_watch=?, ai_generated_at=? WHERE id=?",
            (score, summary, json.dumps(highlights or []), watch, db.now_iso(), entry_id),
        )


_BODY_COMP_EDITABLE = {
    "weight_kg", "body_fat_pct", "skeletal_muscle_kg", "visceral_fat",
    "bmr", "body_water_pct", "note", "log_date", "segments",
}


def update_body_comp_entry(entry_id, **fields):
    sets = {k: v for k, v in fields.items() if k in _BODY_COMP_EDITABLE}
    if not sets:
        return
    if "segments" in sets:
        sets["segments_json"] = json.dumps(sets.pop("segments")) if sets["segments"] else None
    with db.get_conn() as conn:
        cols = ", ".join(f"{k}=?" for k in sets)
        conn.execute(f"UPDATE body_comp_entries SET {cols} WHERE id=?", (*sets.values(), entry_id))


def delete_body_comp_entry(entry_id):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM body_comp_entries WHERE id=?", (entry_id,))


# ---------------- lab categories (mirrors db.py's nutrient_defs) ----------------

def list_lab_categories(enabled_only=False):
    q = "SELECT * FROM lab_categories"
    if enabled_only:
        q += " WHERE enabled=1"
    q += " ORDER BY sort_order"
    with db.get_conn() as conn:
        return [dict(row) for row in conn.execute(q)]


def create_lab_category(key, label, sort_order=None):
    key = key.strip().lower().replace(" ", "_")
    if not key:
        raise ValueError("lab category key cannot be empty")
    with db.get_conn() as conn:
        if sort_order is None:
            row = conn.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 AS n FROM lab_categories").fetchone()
            sort_order = row["n"]
        conn.execute(
            "INSERT INTO lab_categories (key, label, enabled, sort_order) VALUES (?,?,1,?)",
            (key, label, sort_order),
        )
    return key


def update_lab_category(key, **fields):
    if not fields:
        return
    allowed = {"label", "enabled", "sort_order"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    with db.get_conn() as conn:
        cols = ", ".join(f"{k}=?" for k in sets)
        conn.execute(f"UPDATE lab_categories SET {cols} WHERE key=?", (*sets.values(), key))


# ---------------- lab reports & results ----------------

def create_lab_report(file_path, mime_type, label=None, raw_extraction=None, log_date=None):
    date = log_date or db.today_str()
    with db.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO lab_reports (uploaded_at, log_date, file_path, mime_type, label, raw_extraction_json) "
            "VALUES (?,?,?,?,?,?)",
            (db.now_iso(), date, file_path, mime_type, label,
             json.dumps(raw_extraction) if raw_extraction is not None else None),
        )
        return cur.lastrowid


def add_lab_result(report_id, category_key, test_name, value=None, unit=None,
                    ref_low=None, ref_high=None, ref_text=None, flag="normal", test_date=None):
    with db.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO lab_results (report_id, category_key, test_name, value, unit, "
            "ref_low, ref_high, ref_text, flag, test_date) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (report_id, category_key, test_name, value, unit, ref_low, ref_high,
             ref_text, flag, test_date or db.today_str()),
        )
        return cur.lastrowid


def list_lab_reports():
    with db.get_conn() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM lab_reports ORDER BY uploaded_at DESC")]


def delete_lab_report(report_id):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM lab_reports WHERE id=?", (report_id,))  # cascades to lab_results


def list_lab_results(test_name=None, category_key=None):
    q = "SELECT * FROM lab_results"
    clauses, params = [], []
    if test_name:
        clauses.append("test_name=?")
        params.append(test_name)
    if category_key:
        clauses.append("category_key=?")
        params.append(category_key)
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY test_date DESC, id DESC"
    with db.get_conn() as conn:
        return [dict(row) for row in conn.execute(q, params)]


_LAB_RESULT_EDITABLE = {"test_name", "category_key", "value", "unit", "ref_low", "ref_high", "ref_text", "flag", "test_date"}


def update_lab_result(result_id, **fields):
    sets = {k: v for k, v in fields.items() if k in _LAB_RESULT_EDITABLE}
    if not sets:
        return
    with db.get_conn() as conn:
        cols = ", ".join(f"{k}=?" for k in sets)
        conn.execute(f"UPDATE lab_results SET {cols} WHERE id=?", (*sets.values(), result_id))


def delete_lab_result(result_id):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM lab_results WHERE id=?", (result_id,))


# ---------------- other documents ----------------

def create_other_document(file_path, mime_type, label, notes=None, log_date=None):
    date = log_date or db.today_str()
    with db.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO other_documents (uploaded_at, log_date, file_path, mime_type, label, notes) "
            "VALUES (?,?,?,?,?,?)",
            (db.now_iso(), date, file_path, mime_type, label, notes),
        )
        return cur.lastrowid


def list_other_documents():
    with db.get_conn() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM other_documents ORDER BY uploaded_at DESC")]


# ---------------- water ----------------

def create_water_log(ml, log_date=None):
    date = log_date or db.today_str()
    with db.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO water_logs (log_date, logged_at, ml) VALUES (?,?,?)",
            (date, db.now_iso(), ml),
        )
        return cur.lastrowid


def list_water_logs(log_date=None):
    log_date = log_date or db.today_str()
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM water_logs WHERE log_date=? ORDER BY logged_at", (log_date,)
        ).fetchall()
        return [dict(row) for row in rows]


def update_water_log(log_id, ml):
    with db.get_conn() as conn:
        conn.execute("UPDATE water_logs SET ml=? WHERE id=?", (ml, log_id))


def delete_water_log(log_id):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM water_logs WHERE id=?", (log_id,))


def day_water_total(log_date=None):
    log_date = log_date or db.today_str()
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(ml), 0) AS total FROM water_logs WHERE log_date=?", (log_date,)
        ).fetchone()
        return row["total"]


def water_trend(days=7, end_date=None):
    """Returns [{date, ml}] for the last `days` days ending at end_date
    (inclusive), oldest first -- zero-filled for days with no logs."""
    from datetime import datetime, timedelta
    end = end_date or db.today_str()
    end_dt = datetime.strptime(end, "%Y-%m-%d")
    start_dt = end_dt - timedelta(days=days - 1)
    start = start_dt.strftime("%Y-%m-%d")
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT log_date, COALESCE(SUM(ml), 0) AS total FROM water_logs "
            "WHERE log_date BETWEEN ? AND ? GROUP BY log_date", (start, end),
        ).fetchall()
    by_date = {r["log_date"]: r["total"] for r in rows}
    return [
        {"date": (start_dt + timedelta(days=i)).strftime("%Y-%m-%d"),
         "ml": by_date.get((start_dt + timedelta(days=i)).strftime("%Y-%m-%d"), 0)}
        for i in range(days)
    ]


# ---------------- supplements & medicine (mirrors nutrient_defs for the
# definitions; supplement_logs mirrors water_logs for the intake events) ----

def list_supplements(enabled_only=False):
    q = "SELECT * FROM supplements"
    if enabled_only:
        q += " WHERE enabled=1"
    q += " ORDER BY sort_order"
    with db.get_conn() as conn:
        return [dict(row) for row in conn.execute(q)]


def create_supplement(name, dose_amount=None, dose_unit=None, category="other",
                       linked_nutrient_key=None, sort_order=None):
    name = name.strip()
    if not name:
        raise ValueError("supplement name cannot be empty")
    with db.get_conn() as conn:
        if sort_order is None:
            row = conn.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 AS n FROM supplements").fetchone()
            sort_order = row["n"]
        cur = conn.execute(
            "INSERT INTO supplements (name, dose_amount, dose_unit, category, linked_nutrient_key, "
            "enabled, sort_order) VALUES (?,?,?,?,?,1,?)",
            (name, dose_amount, dose_unit, category, linked_nutrient_key, sort_order),
        )
        return cur.lastrowid


_SUPPLEMENT_EDITABLE = {"name", "dose_amount", "dose_unit", "category", "linked_nutrient_key", "enabled", "sort_order"}


def update_supplement(supplement_id, **fields):
    sets = {k: v for k, v in fields.items() if k in _SUPPLEMENT_EDITABLE}
    if not sets:
        return
    with db.get_conn() as conn:
        cols = ", ".join(f"{k}=?" for k in sets)
        conn.execute(f"UPDATE supplements SET {cols} WHERE id=?", (*sets.values(), supplement_id))


def delete_supplement(supplement_id):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM supplements WHERE id=?", (supplement_id,))  # cascades to supplement_logs


def log_supplement_dose(supplement_id, dose_amount=None, log_date=None, note=None):
    """Snapshots the supplement's current default dose onto the log row if
    no explicit amount is given, rather than leaving it null and resolving
    it at read time -- so a later change to the definition's default dose
    doesn't retroactively change what past logs are recorded as."""
    date = log_date or db.today_str()
    with db.get_conn() as conn:
        if dose_amount is None:
            row = conn.execute("SELECT dose_amount FROM supplements WHERE id=?", (supplement_id,)).fetchone()
            dose_amount = row["dose_amount"] if row else None
        cur = conn.execute(
            "INSERT INTO supplement_logs (supplement_id, log_date, logged_at, dose_amount, note) "
            "VALUES (?,?,?,?,?)",
            (supplement_id, date, db.now_iso(), dose_amount, note),
        )
        return cur.lastrowid


def list_supplement_logs(log_date=None, supplement_id=None):
    q = "SELECT * FROM supplement_logs"
    clauses, params = [], []
    if log_date:
        clauses.append("log_date=?")
        params.append(log_date)
    if supplement_id:
        clauses.append("supplement_id=?")
        params.append(supplement_id)
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY logged_at DESC"
    with db.get_conn() as conn:
        return [dict(row) for row in conn.execute(q, params)]


def update_supplement_log(log_id, dose_amount):
    with db.get_conn() as conn:
        conn.execute("UPDATE supplement_logs SET dose_amount=? WHERE id=?", (dose_amount, log_id))


def delete_supplement_log(log_id):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM supplement_logs WHERE id=?", (log_id,))


def day_supplement_nutrient_totals(log_date=None):
    """Sums logged supplement doses for that day, per linked nutrient key --
    e.g. a vitamin_d-linked supplement logged twice today with dose_amount=25
    each contributes 50 toward the vitamin_d total. Assumes dose_unit matches
    the nutrient's unit (no conversion attempted -- if they don't match, the
    supplement's contribution will be wrong until the user fixes the unit,
    same as any other manual-entry mismatch in this app)."""
    date = log_date or db.today_str()
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT s.linked_nutrient_key AS key, SUM(COALESCE(l.dose_amount, s.dose_amount, 0)) AS total "
            "FROM supplement_logs l JOIN supplements s ON s.id = l.supplement_id "
            "WHERE l.log_date=? AND s.linked_nutrient_key IS NOT NULL "
            "GROUP BY s.linked_nutrient_key", (date,),
        ).fetchall()
        return {r["key"]: r["total"] for r in rows}
