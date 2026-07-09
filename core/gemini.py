"""Gemini access via plain REST calls (no google-generativeai SDK).

The official SDK pulls in grpcio, which has no prebuilt wheel for Termux's
architecture and takes a very long time (and often fails) to compile from
source on tablet hardware. Talking to the REST API directly with `requests`
sidesteps that entirely at the cost of writing a few more lines here.

If GEMINI_MODEL starts returning 404s, the model name has likely been
retired -- check https://ai.google.dev/gemini-api/docs/models for the
current free-tier model and update GEMINI_MODEL in .env.
"""

import base64
import json
import re

import requests

from core import config

TIMEOUT = 30
_BASE = "https://generativelanguage.googleapis.com/v1beta/models"


def _call(parts, want_json=True):
    if not config.GEMINI_API_KEY:
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
    except requests.RequestException:
        return None

    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError):
        return None

    if not want_json:
        return text
    return _extract_json(text)


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


_NUTRIENT_SCHEMA_HINT = (
    "Each item must be an object with these numeric fields (omit a field only "
    "if truly unknowable, use your best estimate otherwise): "
    "name (string), kcal, protein_g, carbs_g, fat_g, fiber_g, sugar_g, "
    "sodium_mg, potassium_mg, vitamin_c_mg, iron_mg, calcium_mg, vitamin_d_mcg."
)


def estimate_food_from_text(text):
    """Fallback when the nutrition APIs return nothing usable -- Gemini
    estimates macros/micros directly from the description."""
    prompt = (
        "You are a nutrition estimation assistant. A user logged this meal "
        f"in a food diary: \"{text}\".\n"
        "Break it into distinct food items and estimate nutrition for the "
        f"portion size implied (assume a typical serving if unstated). {_NUTRIENT_SCHEMA_HINT}\n"
        "Respond with a JSON array of items only, no prose."
    )
    result = _call([{"text": prompt}])
    return result if isinstance(result, list) else []


def estimate_food_from_image(image_bytes, mime_type, caption=""):
    """Vision estimate for a plate/packaging photo. Also asks Gemini to
    read any visible barcode/product name so the caller can try to upgrade
    the estimate via Open Food Facts."""
    prompt = (
        "You are a nutrition estimation assistant looking at a photo of food. "
        "Identify each distinct food item and estimate its portion size using "
        "visual reference cues (plate size, utensils, hands). "
        f"{_NUTRIENT_SCHEMA_HINT}\n"
    )
    if caption:
        prompt += f'The user added this caption/weight note: "{caption}". Use it if it specifies a weight.\n'
    prompt += (
        "If the photo clearly shows a packaged product with a visible barcode "
        "or brand name, also include a top-level field \"barcode\" (digits only, "
        "or null) and \"product_name\" (or null) alongside the item array.\n"
        'Respond with JSON only: either a bare array of items, or an object '
        '{"items": [...], "barcode": "...", "product_name": "..."}.'
    )
    b64 = base64.b64encode(image_bytes).decode("ascii")
    parts = [
        {"text": prompt},
        {"inline_data": {"mime_type": mime_type, "data": b64}},
    ]
    result = _call(parts)
    if isinstance(result, list):
        return {"items": result, "barcode": None, "product_name": None}
    if isinstance(result, dict) and "items" in result:
        result.setdefault("barcode", None)
        result.setdefault("product_name", None)
        return result
    return {"items": [], "barcode": None, "product_name": None}


def transcribe_voice(audio_bytes, mime_type="audio/ogg"):
    """Transcribes (and lightly cleans up) a Telegram voice note. Returns
    plain text describing what was eaten, or None on failure."""
    b64 = base64.b64encode(audio_bytes).decode("ascii")
    parts = [
        {"text": (
            "Transcribe this voice note. The speaker is describing a meal "
            "they ate for a food diary. Return only the transcribed text, "
            "cleaned up into a plain sentence -- no JSON, no extra commentary."
        )},
        {"inline_data": {"mime_type": mime_type, "data": b64}},
    ]
    text = _call(parts, want_json=False)
    return text.strip() if text else None


def generate_daily_insight(context):
    """context: dict of structured facts about the last N days. Returns a
    short plain-text insight, or None."""
    prompt = (
        "You are a nutrition coach writing a single short insight (2-3 "
        "sentences max) for a food-tracking app, based on this structured "
        "data:\n" + json.dumps(context) + "\n"
        "Be specific and concrete, cite real numbers, no generic advice, "
        "no apology, no disclaimers. Plain text only, no markdown."
    )
    return _call([{"text": prompt}], want_json=False)


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
    if isinstance(result, dict):
        return result
    return None


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
