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
    note TEXT
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


def init_health_db():
    with db.get_conn() as conn:
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
                            source="manual", note=None, log_date=None):
    date = log_date or db.today_str()
    with db.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO body_comp_entries (log_date, logged_at, weight_kg, body_fat_pct, "
            "skeletal_muscle_kg, visceral_fat, bmr, body_water_pct, source, note) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (date, db.now_iso(), weight_kg, body_fat_pct, skeletal_muscle_kg,
             visceral_fat, bmr, body_water_pct, source, note),
        )
        return cur.lastrowid


def list_body_comp_entries(limit=None):
    q = "SELECT * FROM body_comp_entries ORDER BY log_date DESC, logged_at DESC"
    if limit:
        q += f" LIMIT {int(limit)}"
    with db.get_conn() as conn:
        return [dict(row) for row in conn.execute(q)]


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
