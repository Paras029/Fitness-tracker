"""Gemini access via plain REST calls (no google-generativeai SDK -- see
note below), providing three distinct, single-purpose calls used by the
meal-logging pipeline:

  1. extract_ingredients()  -- WHAT was eaten and HOW MUCH. Never touches
     nutrition. Takes text and/or an image (plate photo, barcode photo,
     ingredient-label photo) and returns a structured ingredient list.

  2. fill_nutrition()       -- turns an ingredient list (plus whatever
     partial data the nutrition APIs already found) into a complete
     per-100g macro/micro profile per item. API data is treated as ground
     truth; this call only fills the gaps API left, or estimates from
     scratch when API found nothing.

  3. rate_meal()            -- a single healthiness rating for a
     finalized, confirmed meal.

Splitting extraction from nutrition estimation (rather than one blended
call, which is what this file used to do) is deliberate: it lets stage 2
(the deterministic API lookups in nutrition_apis.py) sit between them and
do the accuracy-critical work, with the LLM only ever filling gaps or
identifying food -- both things it's good at -- instead of being the sole
source of truth for numbers it's prone to guessing confidently and wrong.

The official google-generativeai SDK pulls in grpcio, which has no
prebuilt wheel for Termux's architecture and takes a very long time (and
often fails) to compile from source on tablet hardware. Talking to the
REST API directly with `requests` sidesteps that entirely.

If GEMINI_MODEL starts returning 404s, the model name has likely been
retired -- check https://ai.google.dev/gemini-api/docs/models for the
current free-tier model and update GEMINI_MODEL in .env, or run
`python -m scripts.check_setup` which cross-checks it automatically.
"""

import base64
import json
import logging
import re

import requests

from core import config

TIMEOUT = 30
_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
log = logging.getLogger("gemini")

NUTRIENT_FIELDS = (
    "kcal, protein, carbs, fat, fiber, sugar, sodium, potassium, "
    "vitamin_c, iron, calcium, vitamin_d"
)


def _call(parts, want_json=True):
    if not config.GEMINI_API_KEY:
        log.warning("GEMINI_API_KEY is not set -- skipping Gemini call.")
        return None
    url = f"{_BASE}/{config.GEMINI_MODEL}:generateContent"
    body = {"contents": [{"parts": parts}]}
    if want_json:
        body["generationConfig"] = {"response_mime_type": "application/json"}
    try:
        resp = requests.post(
            url, params={"key": config.GEMINI_API_KEY}, json=body, timeout=TIMEOUT
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        detail = e.response.text[:300] if getattr(e, "response", None) is not None else str(e)
        log.warning("Gemini call to model '%s' failed: %s", config.GEMINI_MODEL, detail)
        return None

    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError):
        log.warning("Gemini response had no usable candidate: %s", json.dumps(data)[:300])
        return None

    if not want_json:
        return text
    result = _extract_json(text)
    if result is None:
        log.warning("Gemini response wasn't valid JSON: %s", text[:300])
    return result


def _extract_json(text):
    cleaned = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"(\[.*\]|\{.*\})", cleaned, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                return None
    return None


# ==================== STAGE 1: extraction ====================
#
# Input:  raw text and/or an image, optionally a caption.
# Output: {
#   "items": [
#     {"name": str, "grams": number, "grams_confidence": "explicit"|"assumed",
#      "is_packaged": bool, "barcode": str|null},
#     ...
#   ],
#   "meal_label": str,               -- short 2-5 word description
#   "extraction_confidence": "high"|"medium"|"low",
#   "notes": str|null                -- anything ambiguous, for the user to see
# }

_EXTRACTION_INSTRUCTIONS = (
    "You are a food-logging extraction assistant. Your ONLY job is identifying "
    "WHAT foods are present and HOW MUCH of each -- never estimate calories, "
    "protein, or any other nutrient here; that happens in a separate step.\n\n"
    "For each distinct food item, determine:\n"
    "- name: a clear, specific food name (e.g. \"grilled chicken breast\" rather "
    "than just \"chicken\" if the description supports that specificity)\n"
    "- grams: your best estimate of the quantity in grams. Convert stated "
    "weights/volumes precisely (use standard densities for common foods, e.g. "
    "1 cup cooked rice ≈ 195g, 1 medium banana ≈ 118g). If no quantity is "
    "stated or visible, assume a typical single-serving portion.\n"
    "- grams_confidence: \"explicit\" if a weight/volume was stated or is clearly "
    "countable (e.g. \"2 eggs\"), \"assumed\" if you defaulted to a typical serving.\n"
    "- is_packaged: true if this is a branded/packaged product.\n"
    "- barcode: the digits of a visible barcode, or null.\n\n"
    "Respond with JSON only: {\"items\": [{\"name\":str, \"grams\":number, "
    "\"grams_confidence\":str, \"is_packaged\":bool, \"barcode\":str|null}, ...], "
    "\"meal_label\": a short 2-5 word description of the overall meal, "
    "\"extraction_confidence\": \"high\"|\"medium\"|\"low\", "
    "\"notes\": a short string flagging anything ambiguous, or null}"
)


def extract_ingredients(text=None, image_bytes=None, mime_type=None, caption=None):
    """Returns the parsed dict described above, or None on total failure
    (caller should treat that as "couldn't parse, ask the user to retry
    or rephrase")."""
    if not text and not image_bytes:
        return None

    parts = [{"text": _EXTRACTION_INSTRUCTIONS}]
    if image_bytes:
        parts[0]["text"] += (
            "\n\nYou are looking at a photo. Use visual reference cues (plate "
            "size, utensils, hands, packaging) to judge portions. If the photo "
            "shows an ingredient list or nutrition label rather than a plate of "
            "food, extract each labeled ingredient as its own item with "
            "is_packaged=true."
        )
        if caption:
            parts[0]["text"] += f'\n\nThe user captioned this photo: "{caption}" -- use it if it clarifies quantity.'
        parts.append({"inline_data": {"mime_type": mime_type, "data": base64.b64encode(image_bytes).decode("ascii")}})
    else:
        parts[0]["text"] += f'\n\nThe user\'s description: "{text}"'

    result = _call(parts)
    if not isinstance(result, dict) or "items" not in result:
        return None
    result.setdefault("meal_label", None)
    result.setdefault("extraction_confidence", "medium")
    result.setdefault("notes", None)
    return result


# ==================== STAGE 2 (post API lookup): nutrition fill ====================
#
# Input:  [{"name": str, "grams": number, "api_data": {partial per-100g dict}|{},
#           "api_source": str|null}, ...]
# Output: {"items": [
#     {"name": str,
#      "nutrients_per_100g": {kcal, protein, carbs, fat, fiber, sugar, sodium,
#                              potassium, vitamin_c, iron, calcium, vitamin_d},
#      "confidence": "database"|"llm_filled"|"llm_estimated",
#      "notes": str|null},
#     ...
# ]}

_NUTRITION_INSTRUCTIONS = (
    "You are a nutrition data assistant. For each food item below you're given "
    "its name and whatever per-100g nutrition data a food database API already "
    "found (possibly empty). Rules:\n\n"
    "1. Treat any API-provided field as ground truth -- do not change it unless "
    "it is clearly, obviously wrong for this food (e.g. a unit error), and if "
    "you do override it, explain why in \"notes\".\n"
    "2. Fill in every field the API didn't provide, per 100g of the named food, "
    "using your own nutrition knowledge.\n"
    "3. If the API gave nothing useful for an item, estimate its full profile "
    "yourself.\n\n"
    f"Required fields per item, all values per 100g: {NUTRIENT_FIELDS}.\n\n"
    "Set \"confidence\" per item to:\n"
    "- \"database\" if kcal/protein/carbs/fat all came from the API (you only "
    "filled minor gaps like fiber or micronutrients)\n"
    "- \"llm_filled\" if the API gave some but not all core macro fields\n"
    "- \"llm_estimated\" if the API gave nothing usable and this is entirely "
    "your estimate\n\n"
    "Items:\n{items_json}\n\n"
    "Respond with JSON only: {\"items\": [{\"name\":str, "
    "\"nutrients_per_100g\": {" + NUTRIENT_FIELDS + "}, "
    "\"confidence\":str, \"notes\":str|null}, ...]}"
)


def fill_nutrition(items):
    """items: [{"name", "grams", "api_data" (dict, per-100g, may be partial
    or empty), "api_source"}]. Returns {"items": [...]} in the shape above,
    or None if the call failed entirely (caller falls back to whatever
    partial API data it already has, tagged low-confidence)."""
    if not items:
        return {"items": []}
    payload = [{"name": it["name"], "known_data_per_100g": it.get("api_data") or {},
                "known_data_source": it.get("api_source")} for it in items]
    prompt = _NUTRITION_INSTRUCTIONS.replace("{items_json}", json.dumps(payload))
    result = _call([{"text": prompt}])
    if not isinstance(result, dict) or "items" not in result:
        return None
    return result


# ==================== STAGE 3: healthiness rating ====================
#
# Input:  meal label, meal_slot, items (name+grams), totals (whatever
#         nutrients are known).
# Output: {"score": 1-10, "label": str, "note": str}

_RATING_INSTRUCTIONS = (
    "You are a nutrition coach rating a single logged meal's overall "
    "healthiness. Consider protein adequacy, fiber, sugar/sodium load, and "
    "likely processing level -- not just calorie count.\n\n"
    "Meal: \"{label}\" ({meal_slot})\n"
    "Ingredients: {items}\n"
    "Totals: {totals}\n\n"
    "Respond with JSON only: {{\"score\": integer 1-10 (10 = excellent "
    "nutritional quality and balance, 1 = poor), \"label\": a short 1-3 word "
    "descriptor (e.g. \"Balanced\", \"Protein-rich\", \"Sugar-heavy\"), "
    "\"note\": one honest, specific, encouraging-but-not-preachy sentence}}"
)


def rate_meal(label, meal_slot, items, totals):
    items_str = ", ".join(f"{it['name']} ({it.get('grams', '?')}g)" for it in items)
    prompt = _RATING_INSTRUCTIONS.format(
        label=label, meal_slot=meal_slot, items=items_str, totals=json.dumps(totals)
    )
    result = _call([{"text": prompt}])
    if not isinstance(result, dict) or "score" not in result:
        return None
    return result


# ==================== supporting calls (unchanged role) ====================

def transcribe_voice(audio_bytes, mime_type="audio/ogg"):
    """Transcribes a Telegram voice note into plain text, which then feeds
    into extract_ingredients() as the `text` argument. Returns None on
    failure."""
    parts = [
        {"text": (
            "Transcribe this voice note. The speaker is describing a meal "
            "they ate for a food diary. Return only the transcribed text, "
            "cleaned up into a plain sentence -- no JSON, no extra commentary."
        )},
        {"inline_data": {"mime_type": mime_type, "data": base64.b64encode(audio_bytes).decode("ascii")}},
    ]
    text = _call(parts, want_json=False)
    return text.strip() if text else None


def generate_weekly_report(context):
    """Returns {"summary": str, "suggestions": [str, ...], "watch": str}
    or None if generation failed."""
    prompt = (
        "You are a nutrition coach producing a weekly report for a food-"
        "tracking app from this structured week of data:\n"
        + json.dumps(context) + "\n"
        "Respond with JSON: {\"summary\": a 2-3 sentence narrative summary, "
        "\"suggestions\": an array of 2-3 short concrete action items, "
        "\"watch\": one sentence flagging the single most notable "
        "nutrient gap or pattern}. Be specific, cite real numbers from the "
        "data, no generic advice, no filler."
    )
    result = _call([{"text": prompt}])
    return result if isinstance(result, dict) else None


def answer_question(question, context):
    """context should already be the resolved/aggregated answer data (e.g.
    a day's or week's totals) -- Gemini phrases the answer, it doesn't
    compute it, so the numbers stay trustworthy."""
    prompt = (
        f'A user of a food-tracking app asked: "{question}"\n'
        "Here is the relevant data already computed from their log:\n"
        + json.dumps(context) + "\n"
        "Answer in 1-3 sentences using only this data. If the data doesn't "
        "actually answer the question, say what data would be needed "
        "instead of guessing. Plain text only."
    )
    text = _call([{"text": prompt}], want_json=False)
    return text.strip() if text else "Sorry, I couldn't work that out right now."
