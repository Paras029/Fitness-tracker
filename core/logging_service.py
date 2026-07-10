"""Orchestrates the meal-logging pipeline. This is the one module the bot
and the Flask API both call into -- neither talks to the nutrition APIs
or Gemini directly.

    build_meal_draft()  Input (text/photo/voice-transcript) -> a draft
                         meal: itemized ingredients, each with a per-100g
                         nutrient profile and a confidence tag, ready for
                         the user to confirm.

                         Internally: extraction LLM call -> per-ingredient
                         API lookups (cache -> CalorieNinjas -> USDA ->
                         Open Food Facts) -> nutrition-fill LLM call for
                         whatever the APIs didn't cover.

    confirm_meal()       Persists a confirmed draft as a meal + items,
                         teaches the food cache, and requests a
                         healthiness rating.

    edit_item() / delete_item() / add_item_to_meal() / delete_meal()
                         Post-log corrections. Editing grams rescales
                         automatically -- see db.py's per-100g model.

Every nutrient value flowing through this module is per 100g until the
very last step (display/aggregation), where it's multiplied by grams.
"""

import math
import re

from core import db, food_match, gemini, nutrition_apis

NUTRIENT_KEYS = [
    "kcal", "protein", "carbs", "fat", "fiber", "sugar",
    "sodium", "potassium", "vitamin_c", "iron", "calcium", "vitamin_d",
]

_CORE_MACROS = ("kcal", "protein", "carbs", "fat")


def _norm_name(name):
    return re.sub(r"[^a-z0-9\s]", "", (name or "").lower()).strip()


def _clean_numeric(d):
    out = {}
    for k, v in (d or {}).items():
        if k not in NUTRIENT_KEYS:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(f):
            out[k] = f
    return out


def _has_core_macros(d):
    return all(k in d for k in _CORE_MACROS)


# ==================== stage 2+3: resolve ingredients to nutrients ====================

def _lookup_one(name, barcode=None):
    """Deterministic per-ingredient lookup: cache first, then whichever
    API answers. Returns (nutrients_per_100g_clean, source, cache_row)."""
    cached = food_match.find_cached_match(name)
    if cached:
        import json
        raw = cached["nutrients_per_100g_json"]
        nutrients = json.loads(raw) if isinstance(raw, str) else raw
        return _clean_numeric(nutrients), "cache", cached

    if barcode:
        off = nutrition_apis.lookup_openfoodfacts_barcode(barcode)
        if off:
            return _clean_numeric(off), "openfoodfacts", None

    ninja = nutrition_apis.parse_calorieninjas(name)
    if ninja:
        return _clean_numeric(ninja), "calorieninjas", None

    usda = nutrition_apis.parse_usda(name)
    if usda:
        return _clean_numeric(usda), "usda", None

    return {}, None, None


def _resolve_ingredients(extracted_items):
    """extracted_items: [{name, grams, grams_confidence, is_packaged,
    barcode}, ...] -- the shape gemini.extract_ingredients() produces.

    Returns resolved items ready for db.create_meal/add_meal_item:
    [{name, grams, nutrients_per_100g, source, confidence}, ...]
    """
    looked_up = []
    for it in extracted_items:
        name = it["name"]
        grams = db.safe_num(it.get("grams"), 100)
        api_data, source, cache_row = _lookup_one(name, it.get("barcode"))
        looked_up.append({
            "name": name, "grams": grams, "api_data": api_data,
            "source": source, "cache_row": cache_row,
        })

    # Cache hits are already-confirmed data (previously logged/corrected) --
    # no need to spend an LLM call re-deriving them.
    need_fill = [l for l in looked_up if l["source"] != "cache"]
    filled_by_name = {}
    if need_fill:
        fill_result = gemini.fill_nutrition([
            {"name": l["name"], "api_data": l["api_data"], "api_source": l["source"]}
            for l in need_fill
        ])
        if fill_result:
            for fit in fill_result.get("items", []):
                filled_by_name[_norm_name(fit.get("name", ""))] = fit

    resolved = []
    for l in looked_up:
        if l["source"] == "cache":
            resolved.append({
                "name": l["name"], "grams": l["grams"],
                "nutrients_per_100g": l["api_data"], "source": "cache", "confidence": "database",
            })
            continue

        fit = filled_by_name.get(_norm_name(l["name"]))
        if fit:
            merged = _clean_numeric(fit.get("nutrients_per_100g"))
            merged.update(l["api_data"])  # API always wins over the LLM on overlapping fields
            confidence = fit.get("confidence") or ("database" if _has_core_macros(l["api_data"]) else "llm_estimated")
        else:
            merged = l["api_data"]
            confidence = "database" if _has_core_macros(merged) else "estimated"

        resolved.append({
            "name": l["name"], "grams": l["grams"], "nutrients_per_100g": merged,
            "source": l["source"] or "gemini_estimate", "confidence": confidence,
        })
    return resolved


# ==================== stage 1 + draft assembly ====================

def build_meal_draft(text=None, image_bytes=None, mime_type=None, caption=None):
    """Returns a draft dict:
        {"items": [{name, grams, nutrients_per_100g, nutrients (absolute,
                    for display), source, confidence}, ...],
         "meal_label": str, "extraction_confidence": str, "notes": str|null,
         "raw_input": str}
    or {"items": [], "error": str} if extraction failed outright.
    """
    raw_input = text or caption or "[photo]"
    extraction = gemini.extract_ingredients(text=text, image_bytes=image_bytes,
                                             mime_type=mime_type, caption=caption)
    if not extraction or not extraction.get("items"):
        return {"items": [], "meal_label": None, "extraction_confidence": None,
                "notes": extraction.get("notes") if extraction else None,
                "raw_input": raw_input,
                "error": "Couldn't identify any food. Try rephrasing, or check "
                         "GEMINI_API_KEY with `python -m scripts.check_setup`."}

    resolved = _resolve_ingredients(extraction["items"])
    for item in resolved:
        item["nutrients"] = db.item_absolute_nutrients(item)

    return {
        "items": resolved,
        "meal_label": extraction.get("meal_label") or (resolved[0]["name"] if resolved else "Meal"),
        "extraction_confidence": extraction.get("extraction_confidence"),
        "notes": extraction.get("notes"),
        "raw_input": raw_input,
    }


def build_meal_draft_from_voice(audio_bytes, mime_type="audio/ogg"):
    """Returns (transcript, draft) -- transcript is shown back to the user
    so they can sanity-check what was heard before confirming. transcript
    is None if transcription itself failed."""
    transcript = gemini.transcribe_voice(audio_bytes, mime_type)
    if not transcript:
        return None, {"items": [], "error": "Couldn't make that out -- try again or send text instead."}
    return transcript, build_meal_draft(text=transcript)


# ==================== confirm / persist ====================

def confirm_meal(draft, meal_slot, log_date=None):
    """Persists a confirmed draft, teaches the food cache, and requests a
    healthiness rating (one more Gemini call, run synchronously here --
    the small extra latency is worth getting the rating back in the same
    response rather than needing a second round trip)."""
    items = draft["items"]
    meal = db.create_meal(
        meal_slot, items, label=draft.get("meal_label"),
        raw_input=draft.get("raw_input"),
        extraction_confidence=draft.get("extraction_confidence"),
        log_date=log_date,
    )
    for item in items:
        db.upsert_food_cache(_norm_name(item["name"]), item["name"], item["source"], item["nutrients_per_100g"])

    rating = gemini.rate_meal(meal["label"], meal_slot, items, meal["totals"])
    if rating and "score" in rating:
        db.set_meal_rating(meal["id"], rating.get("score"), rating.get("label"), rating.get("note"))
        meal = db.get_meal(meal["id"])
    return meal


# ==================== post-log editing ====================

def edit_item(item_id, grams=None, name=None, nutrients_per_100g=None):
    """Editing grams alone rescales every nutrient for that item -- no
    other field needs to change. User-edited values become the new source
    of truth for that food in the cache."""
    meal_id = db.update_meal_item(item_id, grams=grams, name=name, nutrients_per_100g=nutrients_per_100g)
    if meal_id is None:
        return None
    meal = db.get_meal(meal_id)
    edited = next((it for it in meal["items"] if it["id"] == item_id), None)
    if edited and (name is not None or nutrients_per_100g is not None):
        db.upsert_food_cache(_norm_name(edited["name"]), edited["name"], "user", edited["nutrients_per_100g"])
    return meal


def delete_item(item_id):
    """Returns the meal_id the item belonged to (meal is auto-deleted by
    db.delete_meal_item if that was its last item), or None if not found."""
    return db.delete_meal_item(item_id)


def add_item_to_meal(meal_id, name, grams=None):
    """Runs the same cache -> API -> LLM-fill resolution as a fresh draft,
    for just this one ingredient, then appends it to an existing meal."""
    resolved = _resolve_ingredients([{"name": name, "grams": grams or 100}])
    item = resolved[0]
    item_id = db.add_meal_item(meal_id, item["name"], item["grams"], item["nutrients_per_100g"],
                                source=item["source"], confidence=item["confidence"])
    db.upsert_food_cache(_norm_name(item["name"]), item["name"], item["source"], item["nutrients_per_100g"])
    return db.get_meal(meal_id), item_id


def delete_meal(meal_id):
    db.delete_meal(meal_id)


# ==================== aggregation / reporting ====================

def nutrient_contribution(log_date, nutrient_key):
    """Ranked breakdown of which logged items contributed most to a given
    nutrient on a given day -- answers "what drove my fat today"."""
    meals = db.get_day_meals(log_date)
    rows = []
    total = 0
    for m in meals:
        for it in m["items"]:
            val = db.safe_num(it["nutrients"].get(nutrient_key))
            if val <= 0:
                continue
            total += val
            rows.append({"name": it["name"], "meal_slot": m["meal_slot"], "meal_label": m["label"], "value": val})
    for r in rows:
        r["value"] = round(r["value"], 1)
        r["pct"] = round(100 * r["value"] / total, 1) if total else 0
    rows.sort(key=lambda r: r["value"], reverse=True)
    return {"total": round(total, 1), "items": rows}


def build_day_summary(log_date):
    totals, meals = db.day_totals(log_date)
    defs = db.list_nutrient_defs(enabled_only=True)
    nutrients = []
    for d in defs:
        target = db.resolve_target(d)
        value = round(totals.get(d["key"], 0), 1)
        nutrients.append({
            "key": d["key"], "label": d["label"], "unit": d["unit"],
            "category": d["category"], "direction": d["direction"],
            "value": value, "target": target,
            "pct": round(100 * value / target, 1) if target else None,
        })
    return {"date": log_date, "nutrients": nutrients, "meal_count": len(meals), "meals": meals}


def build_week_context(end_date, days=7):
    from datetime import datetime, timedelta
    end = datetime.strptime(end_date, "%Y-%m-%d")
    start = end - timedelta(days=days - 1)
    meals = db.get_range_meals(start.strftime("%Y-%m-%d"), end_date)

    by_date = {}
    for m in meals:
        by_date.setdefault(m["log_date"], []).append(m)

    defs = {d["key"]: d for d in db.list_nutrient_defs(enabled_only=True)}
    daily = []
    for i in range(days):
        d = (start + timedelta(days=i)).strftime("%Y-%m-%d")
        day_meals = by_date.get(d, [])
        totals = {}
        for m in day_meals:
            for k, v in m["totals"].items():
                totals[k] = totals.get(k, 0) + db.safe_num(v)
        daily.append({"date": d, "totals": {k: round(v, 1) for k, v in totals.items()}})

    avg = {}
    for key in defs:
        vals = [d["totals"].get(key, 0) for d in daily]
        avg[key] = round(sum(vals) / len(vals), 1) if vals else 0

    gaps = []
    for key, d in defs.items():
        target = db.resolve_target(d)
        if not target:
            continue
        pct = round(100 * avg.get(key, 0) / target, 1)
        gaps.append({"key": key, "label": d["label"], "avg": avg.get(key, 0), "target": target,
                      "pct_of_target": pct, "direction": d["direction"]})

    ratings = [m["rating_score"] for m in meals if m.get("rating_score") is not None]

    return {"start_date": start.strftime("%Y-%m-%d"), "end_date": end_date,
            "daily": daily, "averages": avg, "targets_vs_actual": gaps,
            "avg_meal_rating": round(sum(ratings) / len(ratings), 1) if ratings else None}
