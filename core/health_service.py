"""Orchestrates the Health & Body domain -- the one module the bot and web
API call into for body composition, lab reports, other documents, and
water. Neither talks to health_db.py or core.gemini.lab_extraction
directly, mirroring the nutrition side's logging_service.py split.
"""

import difflib
from datetime import datetime, timedelta

from core import db, health_db

# How often each kind of measurement should reasonably be redone. Weight
# is expected to be logged often (a quick check-in cadence); the full
# detailed BCA breakdown (body fat/muscle/visceral/BMR/water) happens far
# less often, hence its own longer interval. Labs get a shorter interval
# if the latest panel in that category had anything flagged out of range
# -- worth rechecking sooner than a routine "everything's normal" panel.
WEIGHT_FRESHNESS_DAYS = 14
BODY_COMP_DETAILED_FRESHNESS_DAYS = 30
LAB_FRESHNESS_DAYS = 90
LAB_FRESHNESS_DAYS_IF_FLAGGED = 30

_DETAILED_BODY_COMP_FIELDS = (
    "body_fat_pct", "skeletal_muscle_kg", "visceral_fat", "bmr", "body_water_pct",
)


def _freshness(last_date, interval_days):
    if last_date is None:
        return {"last_date": None, "days_since": None, "interval_days": interval_days,
                "stale": None, "next_due_date": None}
    days_since = (datetime.strptime(db.today_str(), "%Y-%m-%d")
                  - datetime.strptime(last_date, "%Y-%m-%d")).days
    next_due = (datetime.strptime(last_date, "%Y-%m-%d") + timedelta(days=interval_days)).strftime("%Y-%m-%d")
    return {
        "last_date": last_date, "days_since": days_since, "interval_days": interval_days,
        "stale": days_since >= interval_days, "next_due_date": next_due,
    }


def get_body_comp_freshness():
    """Tracked separately because they happen on very different cadences --
    a quick weigh-in doesn't mean the detailed breakdown is up to date, and
    vice versa (uploading a full scan should count as a fresh weigh-in
    too, since it always includes weight)."""
    entries = health_db.list_body_comp_entries()
    weight_date = next((e["log_date"] for e in entries if e["weight_kg"] is not None), None)
    detailed_date = next(
        (e["log_date"] for e in entries if any(e[f] is not None for f in _DETAILED_BODY_COMP_FIELDS)), None,
    )
    return {
        "weight": _freshness(weight_date, WEIGHT_FRESHNESS_DAYS),
        "detailed": _freshness(detailed_date, BODY_COMP_DETAILED_FRESHNESS_DAYS),
    }


def get_lab_freshness():
    """Per lab category, not one blanket number -- redoing just a lipid
    panel shouldn't make the kidney panel look freshly checked. Only
    categories with at least one result are included."""
    results = health_db.list_lab_results()
    by_category = {}
    for r in results:
        by_category.setdefault(r["category_key"], []).append(r)

    out = {}
    for category_key, rows in by_category.items():
        latest_date = max(r["test_date"] for r in rows)
        flagged = any(r["test_date"] == latest_date and r["flag"] != "normal" for r in rows)
        interval = LAB_FRESHNESS_DAYS_IF_FLAGGED if flagged else LAB_FRESHNESS_DAYS
        freshness = _freshness(latest_date, interval)
        freshness["flagged"] = flagged
        out[category_key] = freshness
    return out


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


def extract_lab_report(pdf_bytes=None, image_bytes=None, mime_type=None, caption=None):
    """Stage 1: parse the uploaded report into a draft. Never persists --
    caller reviews/edits the draft, then calls confirm_lab_report()."""
    from core import gemini

    extraction = gemini.extract_lab_results(
        pdf_bytes=pdf_bytes, image_bytes=image_bytes, mime_type=mime_type, caption=caption,
    )
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


def edit_lab_result(result_id, **fields):
    """Recomputes the normal/low/high flag deterministically whenever the
    value (or the reference range itself) changes, same rule as when a
    report is first confirmed -- editing a value shouldn't leave a stale
    flag behind."""
    if "value" in fields or "ref_low" in fields or "ref_high" in fields:
        current = next((r for r in health_db.list_lab_results() if r["id"] == result_id), None)
        if current:
            value = fields.get("value", current["value"])
            ref_low = fields.get("ref_low", current["ref_low"])
            ref_high = fields.get("ref_high", current["ref_high"])
            fields["flag"] = _compute_flag(value, ref_low, ref_high)
    health_db.update_lab_result(result_id, **fields)


def log_water(ml, log_date=None):
    return health_db.create_water_log(ml, log_date=log_date)


def log_body_comp(weight_kg=None, body_fat_pct=None, skeletal_muscle_kg=None,
                   visceral_fat=None, bmr=None, body_water_pct=None,
                   source="manual", note=None, log_date=None, segments=None):
    return health_db.create_body_comp_entry(
        weight_kg=weight_kg, body_fat_pct=body_fat_pct, skeletal_muscle_kg=skeletal_muscle_kg,
        visceral_fat=visceral_fat, bmr=bmr, body_water_pct=body_water_pct,
        source=source, note=note, log_date=log_date, segments=segments,
    )


def extract_body_comp_scan(pdf_bytes=None, image_bytes=None, mime_type=None, caption=None):
    """Parses an uploaded scan into fields the entry form can prefill --
    never persists. The user reviews/edits before hitting Save, same as
    the manual-entry path (log_body_comp), just pre-filled."""
    from core import gemini

    extraction = gemini.extract_body_comp_scan(
        pdf_bytes=pdf_bytes, image_bytes=image_bytes, mime_type=mime_type, caption=caption,
    )
    if extraction is None:
        return {"error": "Couldn't parse this scan -- check GEMINI_API_KEY / quota with "
                          "python -m scripts.check_setup, or try a clearer photo."}
    return extraction


def get_lab_summary():
    """On-demand narrative over the CURRENT set of lab results (latest
    value per test, across all categories) -- no persistence, regenerated
    fresh each time the user asks, since new reports may have been added
    since the last summary."""
    from core import gemini

    results = health_db.list_lab_results()
    if not results:
        return {"error": "No lab results yet -- upload a report first."}
    seen, latest = set(), []
    for r in results:
        if r["test_name"] in seen:
            continue
        seen.add(r["test_name"])
        latest.append(r)

    result = gemini.generate_lab_summary(latest)
    if result is None:
        return {"error": "Couldn't generate a summary -- check GEMINI_API_KEY / quota with "
                          "python -m scripts.check_setup."}
    return result


def get_body_comp_summary(entry_id, history_count=6):
    """On-demand AI score + narrative for one entry, using up to
    `history_count` prior entries (oldest first) for trend framing.
    Persists the result onto the entry so it doesn't need regenerating
    every time the entry list is viewed -- only when the user asks again."""
    from core import gemini

    entry = health_db.get_body_comp_entry(entry_id)
    if entry is None:
        return None
    all_entries = health_db.list_body_comp_entries()
    history = [e for e in all_entries if e["id"] != entry_id and e["log_date"] <= entry["log_date"]]
    history = list(reversed(history[:history_count]))

    result = gemini.generate_body_comp_summary(entry, history=history)
    if result is None:
        return {"error": "Couldn't generate a summary -- check GEMINI_API_KEY / quota with "
                          "python -m scripts.check_setup."}
    health_db.set_body_comp_summary(
        entry_id, score=result["score"], summary=result["summary"],
        highlights=result.get("highlights"), watch=result.get("watch"),
    )
    return result
