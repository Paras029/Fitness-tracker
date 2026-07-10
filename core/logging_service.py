"""Orchestrates a raw Telegram message (text/photo/voice) into logged
food-diary entries. This is the one module both the bot and the Flask API
call into -- neither talks to the nutrition APIs or Gemini directly.

Resolution order for text:
    1. local food cache (free, instant, tuned to what you actually eat)
    2. CalorieNinjas (fast multi-item NLP parse)
    3. USDA FoodData Central, as a single-item fallback if CalorieNinjas
       found nothing (e.g. no key configured, or it genuinely missed)
    4. Gemini estimate (last resort, when nothing above matched)
USDA is also used opportunistically to fill in missing fiber/micronutrient
fields on a CalorieNinjas hit, since CalorieNinjas' schema is macro-focused.
"""

import re

from core import db, food_match, gemini, nutrition_apis

NUTRIENT_KEYS = [
    "kcal", "protein", "carbs", "fat", "fiber", "sugar",
    "sodium", "potassium", "vitamin_c", "iron", "calcium", "vitamin_d",
]

_GEMINI_FIELD_MAP = {
    "kcal": "kcal", "protein_g": "protein", "carbs_g": "carbs", "fat_g": "fat",
    "fiber_g": "fiber", "sugar_g": "sugar", "sodium_mg": "sodium",
    "potassium_mg": "potassium", "vitamin_c_mg": "vitamin_c", "iron_mg": "iron",
    "calcium_mg": "calcium", "vitamin_d_mcg": "vitamin_d",
}


def _clean_nutrients(raw, field_map=None):
    out = {}
    src = raw
    keys = field_map or {k: k for k in NUTRIENT_KEYS}
    for src_key, our_key in keys.items():
        val = src.get(src_key)
        if isinstance(val, (int, float)):
            out[our_key] = round(float(val), 2)
    return out


def _from_cache_hit(row):
    import json
    return {
        "name": row["label"],
        "nutrients": json.loads(row["nutrients_json"]) if isinstance(row["nutrients_json"], str) else row["nutrients_json"],
        "source": "cache",
        "confidence": "database" if row["source"] != "gemini_estimate" else "estimated",
        "cache_id": row["id"],
    }


def _enrich_with_usda(item):
    """Fills in missing fiber/micronutrient fields on an existing item using
    USDA FoodData Central, without overwriting fields we already trust."""
    missing = [k for k in ("fiber", "sodium", "potassium", "vitamin_c", "iron", "calcium", "vitamin_d")
               if item["nutrients"].get(k) is None]
    if not missing:
        return item
    usda = nutrition_apis.parse_usda(item["name"])
    if not usda:
        return item
    for k in missing:
        if usda.get(k) is not None:
            item["nutrients"][k] = round(float(usda[k]), 2)
    return item


def _scale_per_100g(nutrients, grams):
    factor = grams / 100.0
    return {k: round(v * factor, 2) for k, v in nutrients.items() if isinstance(v, (int, float))}


def _norm_name(name):
    return re.sub(r"[^a-z0-9\s]", "", name.lower()).strip()


def _dedupe_items(items):
    """CalorieNinjas sometimes matches the same ingredient twice in one
    phrase -- e.g. "grilled chicken bowl - 200g chicken with 200g rice"
    matches "chicken" both generically (from "grilled chicken bowl") and
    with its quantity (from "200g chicken"). When one item's name is
    contained in another's, keep whichever has more populated nutrient
    fields (the quantified match is usually the more complete one)."""
    def completeness(it):
        return sum(1 for v in it["nutrients"].values() if v)

    kept = []
    for it in items:
        norm = _norm_name(it["name"])
        dup_at = None
        for i, existing in enumerate(kept):
            existing_norm = _norm_name(existing["name"])
            if norm and existing_norm and (norm in existing_norm or existing_norm in norm):
                dup_at = i
                break
        if dup_at is None:
            kept.append(it)
            continue
        existing = kept[dup_at]
        if completeness(it) > completeness(existing):
            kept[dup_at] = it
        elif completeness(it) == completeness(existing) and len(it["name"]) > len(existing["name"]):
            kept[dup_at] = it
    return kept


def parse_text_entry(text):
    """Returns a list of items: [{name, nutrients, source, confidence}]"""
    cached = food_match.find_cached_match(text)
    if cached:
        return [_from_cache_hit(cached)]

    ninja_items = nutrition_apis.parse_calorieninjas(text)
    if ninja_items:
        items = []
        for it in ninja_items:
            nutrients = {k: v for k, v in it.items() if k in NUTRIENT_KEYS and v is not None}
            # CalorieNinjas occasionally can't compute calories for a vague
            # quantity (e.g. an unquantified item name) -- flag it instead
            # of silently presenting a confident-looking but absent value.
            # USDA isn't used to backfill this: its data is per-100g and we
            # don't know what portion CalorieNinjas assumed, so blending
            # the two here would risk a plausible-looking wrong number.
            confidence = "database" if nutrients.get("kcal") is not None else "estimated"
            item = {"name": it["name"], "nutrients": nutrients, "source": "calorieninjas", "confidence": confidence}
            items.append(_enrich_with_usda(item))
        return _dedupe_items(items)

    usda_hit = nutrition_apis.parse_usda(text)
    if usda_hit:
        grams = _extract_grams(text) or 100
        nutrients = _scale_per_100g(
            {k: v for k, v in usda_hit.items() if k in NUTRIENT_KEYS}, grams
        )
        if nutrients.get("kcal"):
            return [{
                "name": usda_hit.get("name", text),
                "nutrients": nutrients,
                "source": "usda",
                "confidence": "database" if _extract_grams(text) else "estimated",
            }]

    gemini_items = gemini.estimate_food_from_text(text)
    if gemini_items:
        return [
            {
                "name": it.get("name", text),
                "nutrients": _clean_nutrients(it, _GEMINI_FIELD_MAP),
                "source": "gemini_estimate",
                "confidence": "estimated",
            }
            for it in gemini_items
        ]

    return []


def parse_photo_entry(image_bytes, mime_type, caption=""):
    """Returns a list of items, same shape as parse_text_entry."""
    result = gemini.estimate_food_from_image(image_bytes, mime_type, caption)
    items = []
    for it in result.get("items", []):
        cached = food_match.find_cached_match(it.get("name", ""))
        if cached:
            items.append(_from_cache_hit(cached))
            continue
        items.append({
            "name": it.get("name", "Photo item"),
            "nutrients": _clean_nutrients(it, _GEMINI_FIELD_MAP),
            "source": "gemini_vision",
            "confidence": "estimated",
        })

    barcode = result.get("barcode")
    if barcode and len(items) == 1:
        off = nutrition_apis.lookup_openfoodfacts_barcode(barcode)
        if off:
            grams = _extract_grams(caption) or 100
            nutrients = _scale_per_100g(
                {k: v for k, v in off.items() if k in NUTRIENT_KEYS}, grams
            )
            items = [{
                "name": off.get("name", items[0]["name"]),
                "nutrients": nutrients,
                "source": "openfoodfacts",
                "confidence": "database" if _extract_grams(caption) else "estimated",
            }]

    return items


def _extract_grams(text):
    import re
    match = re.search(r"(\d+(?:\.\d+)?)\s*g\b", (text or "").lower())
    return float(match.group(1)) if match else None


def parse_voice_entry(audio_bytes, mime_type="audio/ogg"):
    """Returns (transcript, items) -- transcript is shown back to the user
    so they can sanity-check what was heard before confirming."""
    transcript = gemini.transcribe_voice(audio_bytes, mime_type)
    if not transcript:
        return None, []
    return transcript, parse_text_entry(transcript)


def confirm_and_log(items, meal_slot, raw_input=None, log_date=None):
    """Writes confirmed items to the log and teaches the food cache. Returns
    the list of new log_entries ids."""
    entry_ids = []
    for item in items:
        cache_id = item.get("cache_id")
        norm = food_match.normalize(item["name"])
        cache_id = db.upsert_food_cache(
            norm, item["name"], item.get("source", "user"), item["nutrients"], cache_id=cache_id
        )
        entry_id = db.insert_log_entry(
            name=item["name"],
            nutrients=item["nutrients"],
            meal_slot=meal_slot,
            source=item.get("source", "user"),
            confidence=item.get("confidence", "estimated"),
            raw_input=raw_input,
            food_cache_id=cache_id,
            log_date=log_date,
        )
        entry_ids.append(entry_id)
    return entry_ids


def apply_correction(entry_id, new_nutrients, new_name=None):
    """User-edited values become the new source of truth for that food in
    the cache, so future logs of the same thing start out accurate."""
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM log_entries WHERE id=?", (entry_id,)).fetchone()
        if not row:
            return False
        name = new_name or row["name"]
        import json
        conn.execute(
            "UPDATE log_entries SET name=?, nutrients_json=?, confidence='user_corrected' WHERE id=?",
            (name, json.dumps(new_nutrients), entry_id),
        )
        cache_id = row["food_cache_id"]
    db.upsert_food_cache(food_match.normalize(name), name, "user", new_nutrients, cache_id=cache_id)
    return True


def nutrient_contribution(log_date, nutrient_key):
    """Ranked breakdown of which logged items contributed most to a given
    nutrient on a given day -- answers "what drove my fat today"."""
    entries = db.get_day_entries(log_date)
    total = sum(e["nutrients"].get(nutrient_key, 0) or 0 for e in entries)
    rows = []
    for e in entries:
        val = e["nutrients"].get(nutrient_key, 0) or 0
        if val <= 0:
            continue
        rows.append({
            "name": e["name"],
            "meal_slot": e["meal_slot"],
            "value": round(val, 1),
            "pct": round(100 * val / total, 1) if total else 0,
        })
    rows.sort(key=lambda r: r["value"], reverse=True)
    return {"total": round(total, 1), "items": rows}


def build_day_summary(log_date):
    totals, entries = db.day_totals(log_date)
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
    return {"date": log_date, "nutrients": nutrients, "entry_count": len(entries)}


def build_week_context(end_date, days=7):
    from datetime import datetime, timedelta
    end = datetime.strptime(end_date, "%Y-%m-%d")
    start = end - timedelta(days=days - 1)
    entries = db.get_range_entries(start.strftime("%Y-%m-%d"), end_date)

    by_date = {}
    for e in entries:
        by_date.setdefault(e["log_date"], []).append(e)

    defs = {d["key"]: d for d in db.list_nutrient_defs(enabled_only=True)}
    daily = []
    for i in range(days):
        d = (start + timedelta(days=i)).strftime("%Y-%m-%d")
        day_entries = by_date.get(d, [])
        totals = {}
        for e in day_entries:
            for k, v in e["nutrients"].items():
                totals[k] = totals.get(k, 0) + (v or 0)
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

    return {"start_date": start.strftime("%Y-%m-%d"), "end_date": end_date,
            "daily": daily, "averages": avg, "targets_vs_actual": gaps}
