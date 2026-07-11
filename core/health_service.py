"""Orchestrates the Health & Body domain -- the one module the bot and web
API call into for body composition, lab reports, other documents, and
water. Neither talks to health_db.py or core.gemini.lab_extraction
directly, mirroring the nutrition side's logging_service.py split.
"""

import difflib

from core import db, health_db


def _closest_category_key(hint, categories):
    """Maps a free-text category_hint from the LLM onto one of the
    existing (built-in or user-added) lab_categories keys, falling back to
    "other" -- same fuzzy-match idea as food_match.py, just against a much
    smaller, already-known set of keys/labels instead of a growing cache."""
    if not hint:
        return "other"
    hint_norm = hint.strip().lower()
    candidates = {c["key"]: c["key"] for c in categories}
    candidates.update({c["label"].lower(): c["key"] for c in categories})
    if hint_norm in candidates:
        return candidates[hint_norm]
    best_key, best_ratio = "other", 0.0
    for text, key in candidates.items():
        ratio = difflib.SequenceMatcher(None, hint_norm, text).ratio()
        if ratio > best_ratio:
            best_key, best_ratio = key, ratio
    return best_key if best_ratio >= 0.6 else "other"


def _compute_flag(value, ref_low, ref_high):
    if value is None:
        return "normal"
    if ref_low is not None and value < ref_low:
        return "low"
    if ref_high is not None and value > ref_high:
        return "high"
    return "normal"


def extract_lab_report(pdf_bytes=None, image_bytes=None, mime_type=None):
    """Stage 1: parse the uploaded report into a draft. Never persists --
    caller reviews/edits the draft, then calls confirm_lab_report()."""
    from core import gemini

    extraction = gemini.extract_lab_results(pdf_bytes=pdf_bytes, image_bytes=image_bytes, mime_type=mime_type)
    if extraction is None:
        return {"tests": [], "error": "Couldn't parse this report -- check GEMINI_API_KEY / quota with "
                                       "python -m scripts.check_setup, or try a clearer scan."}

    categories = health_db.list_lab_categories()
    tests = []
    for t in extraction.get("tests", []):
        category_key = _closest_category_key(t.get("category_hint"), categories)
        flag = _compute_flag(t.get("value"), t.get("ref_low"), t.get("ref_high"))
        tests.append({
            "test_name": t.get("test_name"), "category_key": category_key,
            "value": t.get("value"), "unit": t.get("unit"),
            "ref_low": t.get("ref_low"), "ref_high": t.get("ref_high"),
            "ref_text": t.get("ref_text"), "flag": flag,
        })

    return {
        "tests": tests,
        "report_date": extraction.get("report_date"),
        "notes": extraction.get("notes"),
    }


def confirm_lab_report(draft, file_path, mime_type, label=None):
    """Stage 2: persists a (possibly user-edited) draft."""
    test_date = draft.get("report_date") or db.today_str()
    report_id = health_db.create_lab_report(
        file_path=file_path, mime_type=mime_type, label=label, raw_extraction=draft,
    )
    for t in draft.get("tests", []):
        health_db.add_lab_result(
            report_id=report_id, category_key=t.get("category_key", "other"),
            test_name=t["test_name"], value=t.get("value"), unit=t.get("unit"),
            ref_low=t.get("ref_low"), ref_high=t.get("ref_high"), ref_text=t.get("ref_text"),
            flag=t.get("flag", "normal"), test_date=test_date,
        )
    return report_id


def log_water(ml, log_date=None):
    return health_db.create_water_log(ml, log_date=log_date)


def log_body_comp(weight_kg=None, body_fat_pct=None, skeletal_muscle_kg=None,
                   visceral_fat=None, bmr=None, body_water_pct=None,
                   source="manual", note=None, log_date=None):
    return health_db.create_body_comp_entry(
        weight_kg=weight_kg, body_fat_pct=body_fat_pct, skeletal_muscle_kg=skeletal_muscle_kg,
        visceral_fat=visceral_fat, bmr=bmr, body_water_pct=body_water_pct,
        source=source, note=note, log_date=log_date,
    )
