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

_CORE_MACROS = ("kcal", "protein", "carbs", "fat")


def _norm_name(name):
    return re.sub(r"[^a-z0-9\s]", "", (name or "").lower()).strip()


def _clean_numeric(d):
    """Keeps any key with a finite numeric value -- deliberately NOT
    filtered to a fixed nutrient list, so a custom nutrient added via
    Settings (core/db.py's nutrient_defs table) flows through untouched
    instead of being silently dropped here before it ever reaches
    storage. db.sanitize_nutrients() does the same numeric validation
    again at the write boundary; this earlier pass just keeps bad data
    out of in-memory merging."""
    out = {}
    for k, v in (d or {}).items():
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(f):
            out[k] = f
    return out


def _has_core_macros(d):
    return all(k in d for k in _CORE_MACROS)


# Atwater: kcal = 4*protein + 4*carbs + 9*fat (fiber contributes ~2kcal/g,
# not 4, so high-fiber foods legitimately run a bit under this -- the
# tolerance below already covers that along with ordinary rounding).
_KCAL_RECONCILE_TOLERANCE = 0.15
# Only nutrients nobody has actually vetted (pure LLM guesses) are worth
# silently overriding -- a real API's kcal is measured, not derived, and
# a user's explicit correction is deliberate; neither should be
# second-guessed by an arithmetic heuristic behind their back.
_RECONCILABLE_CONFIDENCE = ("llm_filled", "llm_estimated")


def _reconcile_kcal(nutrients, confidence):
    """Deterministic sanity check -- no LLM call, runs on every item, every
    time. An LLM-only kcal estimate that doesn't reconcile with its own
    protein/carbs/fat within ~15% is provably wrong regardless of how
    confident the model sounded, so this just fixes it with arithmetic
    instead of hoping a future prompt gets it right. This is exactly the
    class of inconsistency ("cal should equal 4p+4c+9f") a human would
    catch at a glance -- no reason to spend a Gemini call catching it."""
    if confidence not in _RECONCILABLE_CONFIDENCE:
        return nutrients
    if not all(k in nutrients for k in _CORE_MACROS):
        return nutrients
    p, c, f = nutrients["protein"], nutrients["carbs"], nutrients["fat"]
    computed = 4 * p + 4 * c + 9 * f
    if computed <= 0:
        return nutrients
    actual = nutrients["kcal"]
    if actual <= 0 or abs(actual - computed) / computed > _KCAL_RECONCILE_TOLERANCE:
        nutrients = dict(nutrients)
        nutrients["kcal"] = round(computed, 1)
    return nutrients


def _current_nutrient_keys():
    """The nutrient fields Gemini should be asked to estimate right now --
    every enabled nutrient_defs key (built-ins plus whatever the user has
    added via Settings), not a hardcoded list. This is what makes a
    newly-added custom nutrient actually get populated by the LLM fill
    step instead of staying permanently empty."""
    return [d["key"] for d in db.list_nutrient_defs(enabled_only=True)]


# ==================== stage 2+3: resolve ingredients to nutrients ====================

def _lookup_one(name, barcode=None):
    """Deterministic per-ingredient lookup: cache first, then whichever API
    actually returns usable numbers. Returns (nutrients_per_100g_clean,
    source, cache_row).

    USDA is tried before CalorieNinjas -- CalorieNinjas' free tier now
    gates core fields (calories etc.) behind a premium subscription and
    returns a 200 with a message string instead of a number, which is
    truthy but useless; checking for *any actual nutrient key* (not just
    "the API responded") is what keeps that from silently blocking the
    fallback to a source that still works."""
    cached = food_match.find_cached_match(name)
    if cached:
        import json
        raw = cached["nutrients_per_100g_json"]
        nutrients = json.loads(raw) if isinstance(raw, str) else raw
        return _clean_numeric(nutrients), "cache", cached

    if barcode:
        off = nutrition_apis.lookup_openfoodfacts_barcode(barcode)
        cleaned = _clean_numeric(off) if off else {}
        if cleaned:
            return cleaned, "openfoodfacts", None

    usda = nutrition_apis.parse_usda(name)
    cleaned = _clean_numeric(usda) if usda else {}
    if cleaned:
        return cleaned, "usda", None

    ninja = nutrition_apis.parse_calorieninjas(name)
    cleaned = _clean_numeric(ninja) if ninja else {}
    if cleaned:
        return cleaned, "calorieninjas", None

    return {}, None, None


def _resolve_ingredients(extracted_items):
    """extracted_items: [{name, grams, grams_confidence, is_packaged,
    barcode}, ...] -- the shape gemini.extract_ingredients() produces.

    Returns resolved items ready for db.create_meal/add_meal_item:
    [{name, grams, nutrients_per_100g, source, confidence}, ...]

    The nutrition-fill LLM call runs as a batch (fill_nutrition's schema
    requires kcal/protein/carbs/fat on every item it returns, which is
    what actually prevents most drops -- constraining generation beats
    asking nicely in the prompt). If anything is still incomplete after
    that -- a genuinely dropped item, or the whole call failing -- there's
    exactly one batched retry, covering only the stragglers together.
    Never one call per ingredient: with daily LLM quotas this tight, a
    5-ingredient meal can't cost 5 extra requests.
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

    # Cache hits from a food already confirmed complete skip the batch
    # call -- no need to spend an LLM call re-deriving known-good data.
    # Incomplete cache hits (e.g. cached back when Gemini was
    # quota-exhausted) still go through, which self-heals them over time.
    need_fill = [l for l in looked_up if not (l["source"] == "cache" and _has_core_macros(l["api_data"]))]
    filled_by_name = _batch_fill(need_fill)

    resolved = []
    for l in looked_up:
        if l["source"] == "cache" and _has_core_macros(l["api_data"]):
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

    _retry_missing_macros_once(resolved)
    for item in resolved:
        item["nutrients_per_100g"] = _reconcile_kcal(item["nutrients_per_100g"], item["confidence"])
    return resolved


def _batch_fill(need_fill):
    """One fill_nutrition call covering every item in need_fill. Returns a
    dict of normalized-name -> filled item, empty if the call failed."""
    if not need_fill:
        return {}
    fill_result = gemini.fill_nutrition([
        {"name": l["name"], "api_data": l["api_data"], "api_source": l["source"]}
        for l in need_fill
    ], nutrient_keys=_current_nutrient_keys())
    if not fill_result:
        return {}
    return {_norm_name(fit.get("name", "")): fit for fit in fill_result.get("items", [])}


def _retry_missing_macros_once(resolved):
    """A single additional BATCHED fill_nutrition call -- not one per
    ingredient -- covering only whatever is still missing a core macro
    after the first pass. Mutates `resolved` in place."""
    stragglers = [it for it in resolved if not _has_core_macros(it["nutrients_per_100g"])]
    if not stragglers:
        return
    fill_result = gemini.fill_nutrition([
        {"name": it["name"], "api_data": it["nutrients_per_100g"], "api_source": it["source"]}
        for it in stragglers
    ], nutrient_keys=_current_nutrient_keys())
    if not fill_result:
        return
    filled_by_name = {_norm_name(f.get("name", "")): f for f in fill_result.get("items", [])}
    for item in stragglers:
        fit = filled_by_name.get(_norm_name(item["name"]))
        if not fit:
            continue
        merged = _clean_numeric(fit.get("nutrients_per_100g"))
        merged.update(item["nutrients_per_100g"])  # keep whatever real API data was already there
        item["nutrients_per_100g"] = merged
        if _has_core_macros(merged):
            item["confidence"] = fit.get("confidence") or "llm_estimated"
            if item["source"] is None:
                item["source"] = "gemini_estimate"


# ==================== stage 1 + draft assembly ====================
#
# Split into two calls -- extract_only() then resolve_draft() -- so a
# caller that wants to show progress in real time (which ingredients were
# found, then macros filling in) can render between them instead of
# waiting for one call that does both. build_meal_draft() is a thin
# wrapper of the two for callers that just want the end result (the
# Telegram bot's voice flow, tests, anything that doesn't drive a staged
# UI).

def extract_only(text=None, image_bytes=None, mime_type=None, caption=None):
    """Stage 1 only: WHAT was eaten and HOW MUCH, no nutrition yet.
    Returns {"items": [{name, grams, grams_confidence, is_packaged,
    barcode}, ...], "meal_label", "extraction_confidence", "notes",
    "raw_input"} or {"items": [], "error": str} if extraction failed."""
    raw_input = text or caption or "[photo]"
    extraction = gemini.extract_ingredients(text=text, image_bytes=image_bytes,
                                             mime_type=mime_type, caption=caption)
    if not extraction or not extraction.get("items"):
        return {"items": [], "meal_label": None, "extraction_confidence": None,
                "notes": extraction.get("notes") if extraction else None,
                "raw_input": raw_input,
                "error": "Couldn't identify any food. Try rephrasing, or check "
                         "GEMINI_API_KEY with `python -m scripts.check_setup`."}
    extraction["raw_input"] = raw_input
    return extraction


def resolve_draft(extraction):
    """Stage 2: turns an extract_only() result into a full draft --
    deterministic API lookups plus the batched nutrition-fill LLM call(s).
    Returns the same draft shape build_meal_draft() used to return:
        {"items": [{name, grams, nutrients_per_100g, nutrients (absolute,
                    for display), source, confidence}, ...],
         "meal_label": str, "extraction_confidence": str, "notes": str|null,
         "raw_input": str}
    Passing through an extraction that already errored is a no-op."""
    if extraction.get("error") or not extraction.get("items"):
        return extraction

    resolved = _resolve_ingredients(extraction["items"])
    for item in resolved:
        item["nutrients"] = db.item_absolute_nutrients(item)

    return {
        "items": resolved,
        "meal_label": extraction.get("meal_label") or (resolved[0]["name"] if resolved else "Meal"),
        "extraction_confidence": extraction.get("extraction_confidence"),
        "notes": extraction.get("notes"),
        "raw_input": extraction.get("raw_input"),
    }


def build_meal_draft(text=None, image_bytes=None, mime_type=None, caption=None):
    """Both stages back to back, for callers that don't need to show
    progress between them."""
    extraction = extract_only(text=text, image_bytes=image_bytes, mime_type=mime_type, caption=caption)
    return resolve_draft(extraction)


def build_meal_draft_from_voice(audio_bytes, mime_type="audio/ogg"):
    """Returns (transcript, draft) -- transcript is shown back to the user
    so they can sanity-check what was heard before confirming. transcript
    is None if transcription itself failed."""
    transcript = gemini.transcribe_voice(audio_bytes, mime_type)
    if not transcript:
        return None, {"items": [], "error": "Couldn't make that out -- try again or send text instead."}
    return transcript, build_meal_draft(text=transcript)


# ==================== on-demand: sanity check + conversational refine ====================
#
# Neither of these runs automatically -- each is one more Gemini call, and
# with a daily quota this tight the user decides when it's worth spending
# one, not the pipeline.

def _draft_totals(items):
    totals = {}
    for it in items:
        for k, v in (it.get("nutrients") or {}).items():
            totals[k] = round(totals.get(k, 0) + db.safe_num(v), 2)
    return totals


def sanity_check(draft):
    """On-demand consistency review of a resolved draft's ingredients,
    weights, and macros -- catches things like a per-item macro that's
    wildly off for that food/weight, before the user confirms it. Returns
    {"ok": bool, "flags": [{"item_name", "field", "concern"}, ...], "note":
    str} or None if the call failed."""
    items = draft.get("items") or []
    if not items:
        return None
    return gemini.sanity_check_meal(items, _draft_totals(items))


def refine_meal(draft, user_message, sanity=None):
    """Applies a user's free-text message to an already-resolved draft --
    one Gemini call, only fired when the user actually sends a follow-up,
    with the previous items/totals (and sanity result, if any) passed as
    context. The message doesn't have to be a hard override: it might be
    an explicit correction ("it was 150g", applied exactly) or a question/
    concern ("does this look right?"), which gemini.refine_draft treats
    with the same judgment as a sanity check -- only changing something it
    genuinely believes is wrong, rather than blindly editing on request.

    Returns (updated_draft, changed_names, note) -- changed_names lists
    exactly which items were added, removed, or modified (empty if the
    review concluded nothing needed changing), so the caller can highlight
    just those instead of re-rendering everything as if it were new. note
    is the model's own explanation, meant to be shown verbatim -- "looks
    fine as-is" is a normal, informative answer here, not a failure. On
    failure, returns (draft, [], None) unchanged."""
    items = draft.get("items") or []
    if not items or not user_message or not user_message.strip():
        return draft, [], None

    result = gemini.refine_draft(items, _draft_totals(items), user_message,
                                  sanity=sanity, nutrient_keys=_current_nutrient_keys())
    if not result or not result.get("items"):
        return draft, [], None

    by_name = {_norm_name(it["name"]): it for it in items}
    new_items = []
    changed = []
    for upd in result["items"]:
        name = upd.get("name") or "item"
        key = _norm_name(name)
        original = by_name.get(key)
        grams = db.safe_num(upd.get("grams"), original["grams"] if original else 100)
        nutrients = _clean_numeric(upd.get("nutrients_per_100g")) if upd.get("nutrients_per_100g") \
            else (original["nutrients_per_100g"] if original else {})
        is_new = original is None
        is_different = is_new or original["grams"] != grams or original["nutrients_per_100g"] != nutrients \
            or original["name"] != name
        item = {
            "name": name, "grams": grams, "nutrients_per_100g": nutrients,
            "source": (original or {}).get("source") or "user",
            "confidence": "user_corrected" if is_different else (original or {}).get("confidence", "estimated"),
        }
        item["nutrients"] = db.item_absolute_nutrients(item)
        if is_different:
            changed.append(name)
        new_items.append(item)

    new_draft = dict(draft)
    new_draft["items"] = new_items
    return new_draft, changed, result.get("note")


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


def _day_tracking_tier(kcal_value, kcal_target):
    """A day with nothing logged is NOT a 0-calorie day -- it's a day with
    no data, and averaging/scoring it as if the user ate zero silently
    wrecks every aggregate (a brand-new user with 6 unused days and 1 real
    day looks like they're starving). Classifies into:
      "untracked" -- effectively nothing logged (a few kcal of rounding
                     noise at most). Exclude entirely from analysis/scoring.
      "partial"   -- some real logging, but under half of target -- likely
                     an incomplete day (forgot to log dinner, etc). Still
                     worth analyzing, but call out the gap rather than
                     silently treating it as a genuinely light day.
      "tracked"   -- confident enough to analyze/score normally.
    """
    if kcal_value <= 5:
        return "untracked"
    if kcal_target and kcal_value < 0.5 * kcal_target:
        return "partial"
    return "tracked"


def build_day_summary(log_date):
    totals, meals = db.day_totals(log_date)
    # Logged supplement doses count toward whichever nutrient they're
    # linked to (e.g. a vitamin_d supplement adds to today's vitamin_d
    # total) -- see health_db.day_supplement_nutrient_totals for the
    # (deliberately unit-conversion-free) assumptions this makes. Kept as
    # a lazy import: logging_service is nutrition-domain and shouldn't
    # hard-depend on the health module importing cleanly to work at all.
    try:
        from core import health_db
        supplement_totals = health_db.day_supplement_nutrient_totals(log_date)
    except Exception:
        supplement_totals = {}

    defs = db.list_nutrient_defs(enabled_only=True)
    nutrients = []
    kcal_value, kcal_target = 0, 0
    for d in defs:
        target = db.resolve_target(d)
        from_food = round(totals.get(d["key"], 0), 1)
        from_supplements = round(supplement_totals.get(d["key"], 0), 1)
        value = round(from_food + from_supplements, 1)
        if d["key"] == "kcal":
            kcal_value, kcal_target = value, target
        nutrients.append({
            "key": d["key"], "label": d["label"], "unit": d["unit"],
            "category": d["category"], "direction": d["direction"],
            "value": value, "target": target,
            "pct": round(100 * value / target, 1) if target else None,
            "from_supplements": from_supplements if from_supplements else None,
        })
    return {
        "date": log_date, "nutrients": nutrients, "meal_count": len(meals), "meals": meals,
        "tracking_tier": _day_tracking_tier(kcal_value, kcal_target),
    }


def build_week_context(end_date, days=7):
    from datetime import datetime, timedelta
    end = datetime.strptime(end_date, "%Y-%m-%d")
    start = end - timedelta(days=days - 1)
    meals = db.get_range_meals(start.strftime("%Y-%m-%d"), end_date)

    by_date = {}
    for m in meals:
        by_date.setdefault(m["log_date"], []).append(m)

    defs = {d["key"]: d for d in db.list_nutrient_defs(enabled_only=True)}
    kcal_target = db.resolve_target(defs["kcal"]) if "kcal" in defs else 0

    daily = []
    for i in range(days):
        d = (start + timedelta(days=i)).strftime("%Y-%m-%d")
        day_meals = by_date.get(d, [])
        totals = {}
        for m in day_meals:
            for k, v in m["totals"].items():
                totals[k] = totals.get(k, 0) + db.safe_num(v)
        totals = {k: round(v, 1) for k, v in totals.items()}
        tier = _day_tracking_tier(totals.get("kcal", 0), kcal_target)
        daily.append({"date": d, "totals": totals, "tracking_tier": tier})

    # Only days with real data pull any weight in the averages -- an
    # untracked day contributes nothing (not a silent 0), which is what
    # actually fixes the "average is way below what I ate" symptom: that
    # number was averaging in days from before the app was even in use.
    scoring_days = [d for d in daily if d["tracking_tier"] != "untracked"]
    untracked_dates = [d["date"] for d in daily if d["tracking_tier"] == "untracked"]
    partial_dates = [d["date"] for d in daily if d["tracking_tier"] == "partial"]

    avg = {}
    for key in defs:
        vals = [d["totals"].get(key, 0) for d in scoring_days]
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
            "avg_meal_rating": round(sum(ratings) / len(ratings), 1) if ratings else None,
            "days_tracked": len(scoring_days), "days_untracked": len(untracked_dates),
            "untracked_dates": untracked_dates, "partial_dates": partial_dates}


def previous_week_context(end_date, days=7):
    """Same shape as build_week_context(), but for the week immediately
    before it -- pass this as generate_weekly_report()'s previous_context
    so the report can cite real week-on-week deltas instead of describing
    the current week in isolation."""
    from datetime import datetime, timedelta
    end = datetime.strptime(end_date, "%Y-%m-%d")
    prev_end = end - timedelta(days=days)
    return build_week_context(prev_end.strftime("%Y-%m-%d"), days=days)


def build_daily_report(log_date):
    """Returns {"score": int|null, "summary": str, "tips": [str,...],
    "tracking_note": str|null, "tracking_tier": str}.

    For an "untracked" day this never calls Gemini at all -- there's
    nothing to score and no reason to spend a request scoring silence.
    "partial" days still get scored (from whatever WAS logged), just with
    tracking_note flagging the gap. See _day_tracking_tier for the
    tracked/partial/untracked cutoffs."""
    summary = build_day_summary(log_date)
    tier = summary["tracking_tier"]
    if tier == "untracked":
        return {
            "score": None, "summary": "Nothing logged yet today.", "tips": [],
            "tracking_note": None, "tracking_tier": tier,
        }
    report = gemini.generate_daily_report(summary, tier) or {
        "score": None, "summary": "Report generation needs GEMINI_API_KEY set in .env.",
        "tips": [], "tracking_note": None,
    }
    report["tracking_tier"] = tier
    return report
