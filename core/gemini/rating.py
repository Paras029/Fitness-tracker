"""Everything downstream of a confirmed meal / a range of days: the
healthiness rating attached on confirm, the weekly report, voice
transcription, and free-form Q&A over already-computed data.
"""

import base64
import json

from core.gemini.client import call

# ==================== healthiness rating ====================
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
    result = call([{"text": prompt}], response_schema=_RATING_RESPONSE_SCHEMA, max_output_tokens=300)
    if not isinstance(result, dict) or "score" not in result:
        return None
    return result


# ==================== supporting calls ====================

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
    text = call(parts, want_json=False)
    return text.strip() if text else None


def generate_weekly_report(context, previous_context=None):
    """Returns {"summary": str, "suggestions": [str, ...], "watch": str}
    or None if generation failed.

    previous_context, if given, is the same shape context for the prior
    week -- lets the report cite real week-on-week deltas ("protein up
    12% vs last week") instead of describing the current week in
    isolation."""
    prompt = (
        "You are a nutrition coach producing a weekly report for a food-"
        "tracking app from this structured week of data:\n"
        + json.dumps(context) + "\n"
    )
    if previous_context:
        prompt += (
            "Here is the PRIOR week's data, for comparison -- cite at least "
            "one specific week-on-week change (up/down, with the number) if "
            "the data supports it, don't just describe the current week in "
            "isolation:\n" + json.dumps(previous_context) + "\n"
        )
    prompt += (
        "Respond with JSON: {\"summary\": a 2-3 sentence narrative summary, "
        "\"suggestions\": an array of 2-3 short concrete action items, "
        "\"watch\": one sentence flagging the single most notable "
        "nutrient gap or pattern}. Be specific, cite real numbers from the "
        "data, no generic advice, no filler."
    )
    result = call([{"text": prompt}])
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
    text = call([{"text": prompt}], want_json=False)
    return text.strip() if text else "Sorry, I couldn't work that out right now."
