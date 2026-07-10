"""Gemini access via plain REST calls (no google-generativeai SDK -- see
note below), providing distinct, single-purpose calls used by the
meal-logging pipeline:

  1. extract_ingredients()  -- WHAT was eaten and HOW MUCH. Never touches
     nutrition. Takes text and/or an image (plate photo, barcode photo,
     ingredient-label photo) and returns a structured ingredient list.

  2. fill_nutrition()       -- turns an ingredient list (plus whatever
     partial data the nutrition APIs already found) into a complete
     per-100g macro/micro profile per item. API data is treated as ground
     truth; this call only fills the gaps API left, or estimates from
     scratch when API found nothing.

  3. sanity_check_meal()    -- ON-DEMAND review of a resolved draft's
     ingredients/weights/macros for internal consistency (do the macros
     roughly add up to the calories, is a value implausible for that
     food). Never called automatically -- it costs one more request
     against a tight daily quota, so it only fires when the user actually
     asks for it.

  4. refine_draft()         -- applies a user's free-text correction or
     concern to an already-resolved draft (fix a value, change a weight,
     add/remove an ingredient), given the previous items/totals as
     context. Only fires when the user sends a follow-up message.

  5. rate_meal()            -- a single healthiness rating for a
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
import os
import re

import requests

from core import config

TIMEOUT = 30
_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
log = logging.getLogger("gemini")
DEBUG = os.environ.get("GEMINI_DEBUG") == "1"

NUTRIENT_KEYS = [
    "kcal", "protein", "carbs", "fat", "fiber", "sugar",
    "sodium", "potassium", "vitamin_c", "iron", "calcium", "vitamin_d",
]
CORE_MACRO_KEYS = ("kcal", "protein", "carbs", "fat")


def _call(parts, want_json=True, response_schema=None, max_output_tokens=None):
    if not config.GEMINI_API_KEY:
        log.warning("GEMINI_API_KEY is not set -- skipping Gemini call.")
        return None
    url = f"{_BASE}/{config.GEMINI_MODEL}:generateContent"
    body = {"contents": [{"parts": parts}]}
    if want_json:
        gen_config = {"responseMimeType": "application/json"}
        if response_schema:
            # A schema *constrains* generation -- required fields genuinely
            # cannot be omitted, which is a much stronger guarantee than
            # asking nicely in the prompt text and hoping it's followed.
            gen_config["responseSchema"] = response_schema
        if max_output_tokens:
            gen_config["maxOutputTokens"] = max_output_tokens
        body["generationConfig"] = gen_config
    if DEBUG:
        log.warning("Gemini request body: %s", json.dumps(body)[:2000])

    try:
        resp = requests.post(
            url, params={"key": config.GEMINI_API_KEY}, json=body, timeout=TIMEOUT
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        detail = e.response.text if getattr(e, "response", None) is not None else str(e)
        log.warning("Gemini call to model '%s' failed: %s", config.GEMINI_MODEL, detail if DEBUG else detail[:300])
        return None

    if DEBUG:
        log.warning("Gemini raw response: %s", json.dumps(data)[:4000])

    candidates = data.get("candidates") or []
    finish_reason = candidates[0].get("finishReason") if candidates else data.get("promptFeedback", {}).get("blockReason")
    if finish_reason and finish_reason not in ("STOP", None):
        # Common ones: MAX_TOKENS (response got cut off -- bump maxOutputTokens
        # or shorten the prompt), SAFETY / PROHIBITED_CONTENT (a food photo or
        # description tripped a safety filter), RECITATION.
        log.warning("Gemini finished with reason '%s' instead of a normal stop -- "
                    "this usually means the response was blocked or truncated, not a bug in the request.",
                    finish_reason)

    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError):
        log.warning("Gemini response had no usable candidate (finishReason=%s): %s",
                    finish_reason, json.dumps(data)[:300])
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


_EXTRACT_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "items": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "name": {"type": "STRING"},
                    "grams": {"type": "NUMBER"},
                    "grams_confidence": {"type": "STRING", "enum": ["explicit", "assumed"]},
                    "is_packaged": {"type": "BOOLEAN"},
                    "barcode": {"type": "STRING", "nullable": True},
                },
                "required": ["name", "grams", "grams_confidence"],
            },
        },
        "meal_label": {"type": "STRING"},
        "extraction_confidence": {"type": "STRING", "enum": ["high", "medium", "low"]},
        "notes": {"type": "STRING", "nullable": True},
    },
    "required": ["items", "extraction_confidence"],
}


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

    result = _call(parts, response_schema=_EXTRACT_RESPONSE_SCHEMA, max_output_tokens=2048)
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
    "You are a nutrition data assistant. You will receive a JSON array of "
    "{count} food item(s), each with its name and whatever per-100g nutrition "
    "data a food database API already found (possibly empty for some items).\n\n"
    "Rules:\n"
    "1. You MUST return exactly {count} item(s) in your response, in the same "
    "order given, one output object per input item -- never skip, merge, or "
    "drop an item, even if you are unsure of the values.\n"
    "2. kcal, protein, carbs, and fat are REQUIRED for every item -- always "
    "provide your best estimate for these four, even a rough one, rather than "
    "omitting them. The other tracked fields ({extra_fields}) should be "
    "included whenever you can reasonably estimate them, but may be omitted "
    "if truly unknown.\n"
    "3. Treat any API-provided field as ground truth -- do not change it "
    "unless it is clearly, obviously wrong for this food (e.g. a unit error), "
    "and if you do override it, explain why in \"notes\".\n"
    "4. Fill in every field the API didn't provide, per 100g of the named "
    "food, using your own nutrition knowledge. If the API gave nothing at "
    "all for an item, estimate its full profile yourself -- a reasonable "
    "estimate is always better than a missing value.\n\n"
    "Set \"confidence\" per item to:\n"
    "- \"database\" if kcal/protein/carbs/fat all came from the API (you only "
    "filled minor gaps like fiber or micronutrients)\n"
    "- \"llm_filled\" if the API gave some but not all core macro fields\n"
    "- \"llm_estimated\" if the API gave nothing usable and this is entirely "
    "your estimate\n\n"
    "Items:\n{items_json}"
)


def _nutrient_props(keys):
    return {k: {"type": "NUMBER"} for k in keys}


def _fill_response_schema(keys):
    return {
        "type": "OBJECT",
        "properties": {
            "items": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "name": {"type": "STRING"},
                        "nutrients_per_100g": {
                            "type": "OBJECT",
                            "properties": _nutrient_props(keys),
                            # Constrains generation, not just the prompt text --
                            # the model cannot emit an item missing these.
                            "required": list(CORE_MACRO_KEYS),
                        },
                        "confidence": {"type": "STRING", "enum": ["database", "llm_filled", "llm_estimated"]},
                        "notes": {"type": "STRING", "nullable": True},
                    },
                    "required": ["name", "nutrients_per_100g", "confidence"],
                },
            },
        },
        "required": ["items"],
    }


def fill_nutrition(items, nutrient_keys=None):
    """items: [{"name", "grams", "api_data" (dict, per-100g, may be partial
    or empty), "api_source"}]. Returns {"items": [...]} in the shape above,
    or None if the call failed entirely (caller falls back to whatever
    partial API data it already has, tagged low-confidence).

    nutrient_keys: which per-100g fields to ask Gemini for, beyond the
    always-required core four -- pass the caller's *current* enabled
    nutrient_defs keys (core/db.py) so a custom nutrient added via Settings
    is actually requested from the model, not just displayed as perpetually
    empty. Falls back to the built-in NUTRIENT_KEYS if not given.

    Called at most twice per meal regardless of ingredient count (an
    initial batch covering everything, and one batched retry for whatever
    that first call still left incomplete) -- never once per ingredient,
    to stay well inside tight daily quotas."""
    if not items:
        return {"items": []}
    keys = nutrient_keys or NUTRIENT_KEYS
    extra_fields = ", ".join(k for k in keys if k not in CORE_MACRO_KEYS) or "none"
    payload = [{"name": it["name"], "known_data_per_100g": it.get("api_data") or {},
                "known_data_source": it.get("api_source")} for it in items]
    prompt = (_NUTRITION_INSTRUCTIONS
              .replace("{count}", str(len(items)))
              .replace("{extra_fields}", extra_fields)
              .replace("{items_json}", json.dumps(payload)))
    # ~150 tokens/item is generous for a 12-field nutrient object; floor of
    # 400 covers the fixed overhead for a single-item call.
    max_tokens = min(max(400, 150 * len(items)), 8192)
    result = _call([{"text": prompt}], response_schema=_fill_response_schema(keys), max_output_tokens=max_tokens)
    if not isinstance(result, dict) or "items" not in result:
        return None
    return result


# ==================== STAGE 2.5 (on demand): sanity check ====================
#
# Never called automatically -- one more request against a tight daily
# quota, so it only runs when the user explicitly asks ("Double-check
# this"). Reviews a resolved draft's ingredients/weights/macros for
# internal consistency rather than re-estimating anything.
#
# Input:  items [{"name", "grams", "nutrients_per_100g"}, ...], totals dict
# Output: {"ok": bool, "flags": [{"item_name", "field", "concern"}, ...], "note": str}

_SANITY_INSTRUCTIONS = (
    "You are reviewing an already-logged meal's ingredient list, portion "
    "weights, and computed macros for internal consistency and plausibility "
    "-- a sanity check, not a re-estimate. Look for things that are clearly "
    "wrong: a macro that's an order of magnitude off for that food and "
    "weight, a weight that doesn't match the described portion, "
    "protein/carbs/fat that don't roughly add up to the stated calories "
    "(~4 kcal/g for protein and carbs, ~9 kcal/g for fat -- allow slack for "
    "fiber, alcohol, and rounding), or a value that's implausible for the "
    "named food (e.g. near-zero protein for a meat).\n\n"
    "Items (name, grams, nutrients per 100g): {items_json}\n"
    "Totals for the whole meal: {totals_json}\n\n"
    "Respond with JSON only: {\"ok\": true if nothing looks wrong, false if "
    "you found at least one real issue, \"flags\": [{\"item_name\": str, "
    "\"field\": str, \"concern\": one short specific sentence}, ...] (empty "
    "array if ok), \"note\": one short overall sentence -- reassuring if ok, "
    "specific about the worst issue if not}"
)

_SANITY_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "ok": {"type": "BOOLEAN"},
        "flags": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "item_name": {"type": "STRING"},
                    "field": {"type": "STRING"},
                    "concern": {"type": "STRING"},
                },
                "required": ["item_name", "field", "concern"],
            },
        },
        "note": {"type": "STRING"},
    },
    "required": ["ok", "flags", "note"],
}


def sanity_check_meal(items, totals):
    """items: resolved draft items (name, grams, nutrients_per_100g).
    Returns the shape above, or None if the call failed."""
    items_str = json.dumps([{"name": it["name"], "grams": it.get("grams"),
                              "nutrients_per_100g": it.get("nutrients_per_100g")} for it in items])
    prompt = _SANITY_INSTRUCTIONS.replace("{items_json}", items_str).replace("{totals_json}", json.dumps(totals))
    result = _call([{"text": prompt}], response_schema=_SANITY_RESPONSE_SCHEMA, max_output_tokens=600)
    if not isinstance(result, dict) or "ok" not in result:
        return None
    return result


# ==================== on demand: conversational refine ====================
#
# Only fires when the user sends a follow-up correction/concern about an
# already-resolved draft. Receives the previous items + totals (and the
# sanity-check result, if one was run) as context, so it understands what
# it's revising and why -- never a call per ingredient, one call per
# correction round the user actually asks for.
#
# Input:  items, totals, user_message (str), sanity (dict|None)
# Output: {"items": [the COMPLETE corrected item list], "changed": [names],
#          "note": str}

_REFINE_INSTRUCTIONS = (
    "You previously resolved this meal log. The user now has a correction "
    "or concern about it. Apply exactly what they're asking for -- fix a "
    "wrong value, adjust a weight, add or remove an ingredient, whatever "
    "they describe -- and leave everything else unchanged. If a sanity "
    "check already flagged something and the user's message references it, "
    "resolve it accordingly.\n\n"
    "Current items (name, grams, nutrients per 100g): {items_json}\n"
    "Current totals: {totals_json}\n"
    "{sanity_block}"
    "User's message: \"{message}\"\n\n"
    "Respond with JSON only: {\"items\": the COMPLETE corrected item list "
    "(same shape as the input items -- include every item that should "
    "still be in the meal, not only the ones you changed), \"changed\": "
    "names of items you added, removed, or modified, \"note\": one short "
    "sentence confirming what you changed, in plain language for the user}"
)


def _refine_response_schema(keys):
    return {
        "type": "OBJECT",
        "properties": {
            "items": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "name": {"type": "STRING"},
                        "grams": {"type": "NUMBER"},
                        "nutrients_per_100g": {
                            "type": "OBJECT",
                            "properties": _nutrient_props(keys),
                            "required": list(CORE_MACRO_KEYS),
                        },
                    },
                    "required": ["name", "grams", "nutrients_per_100g"],
                },
            },
            "changed": {"type": "ARRAY", "items": {"type": "STRING"}},
            "note": {"type": "STRING"},
        },
        "required": ["items", "note"],
    }


def refine_draft(items, totals, user_message, sanity=None, nutrient_keys=None):
    """Returns {"items": [...], "changed": [names], "note": str} or None if
    the call failed (caller should leave the draft untouched and tell the
    user to try rephrasing)."""
    if not user_message or not user_message.strip():
        return None
    keys = nutrient_keys or NUTRIENT_KEYS
    items_str = json.dumps([{"name": it["name"], "grams": it.get("grams"),
                              "nutrients_per_100g": it.get("nutrients_per_100g")} for it in items])
    sanity_block = ""
    if sanity and sanity.get("flags"):
        sanity_block = "A sanity check already flagged: " + json.dumps(sanity["flags"]) + "\n"
    prompt = (_REFINE_INSTRUCTIONS
              .replace("{items_json}", items_str)
              .replace("{totals_json}", json.dumps(totals))
              .replace("{sanity_block}", sanity_block)
              .replace("{message}", user_message.strip()))
    max_tokens = min(max(500, 150 * len(items) + 200), 8192)
    result = _call([{"text": prompt}], response_schema=_refine_response_schema(keys), max_output_tokens=max_tokens)
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


_RATING_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "score": {"type": "INTEGER"},
        "label": {"type": "STRING"},
        "note": {"type": "STRING"},
    },
    "required": ["score", "label", "note"],
}


def rate_meal(label, meal_slot, items, totals):
    items_str = ", ".join(f"{it['name']} ({it.get('grams', '?')}g)" for it in items)
    prompt = _RATING_INSTRUCTIONS.format(
        label=label, meal_slot=meal_slot, items=items_str, totals=json.dumps(totals)
    )
    result = _call([{"text": prompt}], response_schema=_RATING_RESPONSE_SCHEMA, max_output_tokens=300)
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
