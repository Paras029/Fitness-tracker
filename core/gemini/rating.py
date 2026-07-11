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


# ==================== daily report ====================
#
# Input:  day_summary (logging_service.build_day_summary() output),
#         tracking_tier ("tracked"|"partial" -- callers should never call
#         this for "untracked", see logging_service.build_daily_report)
# Output: {"score": 0-100, "summary": str, "tips": [str,...], "tracking_note": str|null}

_DAILY_REPORT_INSTRUCTIONS = (
    "You are a nutrition coach scoring a single day's tracked food log for "
    "a food-tracking app, out of 100.\n\n"
    "This day's tracking completeness is \"{tier}\". {tier_instruction}\n\n"
    "Score what was actually eaten -- macro balance relative to target, "
    "protein adequacy, fiber, sugar/sodium load, likely food quality/"
    "processing level -- not calorie volume alone; a day under target "
    "isn't automatically a bad score if what was eaten was well-balanced.\n\n"
    "Day data: {day_json}\n\n"
    "Respond with JSON: {\"score\": integer 0-100 (100 = excellent day), "
    "\"summary\": 2-3 sentence narrative about what was actually eaten "
    "today, \"tips\": array of 1-3 short actionable suggestions, "
    "\"tracking_note\": one short sentence if incomplete tracking affects "
    "confidence in this score, else null}"
)

_DAILY_REPORT_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "score": {"type": "INTEGER"},
        "summary": {"type": "STRING"},
        "tips": {"type": "ARRAY", "items": {"type": "STRING"}},
        "tracking_note": {"type": "STRING", "nullable": True},
    },
    "required": ["score", "summary", "tips"],
}

_TIER_INSTRUCTIONS = {
    "tracked": "This looks like a complete day of tracking -- assess normally.",
    "partial": "Only part of the day appears logged (tracked calories are "
               "under half of today's target) -- likely a meal or two "
               "wasn't logged, not that the user actually ate this "
               "little. Still give your best score and analysis based on "
               "what WAS logged (don't penalize the score just because "
               "logging is incomplete), but set tracking_note to flag "
               "that this may not reflect the full day.",
}


def generate_daily_report(day_summary, tracking_tier):
    """Returns the shape above, or None if the call failed. Callers should
    not invoke this for tracking_tier == "untracked" -- there's nothing to
    score and no reason to spend a request on it; see
    logging_service.build_daily_report, which enforces that."""
    tier_instruction = _TIER_INSTRUCTIONS.get(tracking_tier, _TIER_INSTRUCTIONS["tracked"])
    prompt = (_DAILY_REPORT_INSTRUCTIONS
              .replace("{tier}", tracking_tier)
              .replace("{tier_instruction}", tier_instruction)
              .replace("{day_json}", json.dumps(day_summary)))
    result = call([{"text": prompt}], response_schema=_DAILY_REPORT_RESPONSE_SCHEMA, max_output_tokens=500)
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
    isolation.

    context["averages"] is already computed ONLY over days with real data
    (context["days_tracked"] of them) -- a day with nothing logged is
    excluded entirely, not averaged in as a 0-calorie day, so the numbers
    here are trustworthy as-is. context["untracked_dates"] /
    ["partial_dates"] are still worth surfacing to the user, though, so
    they understand what the average does and doesn't cover."""
    prompt = (
        "You are a nutrition coach producing a weekly report for a food-"
        "tracking app from this structured week of data:\n"
        + json.dumps(context) + "\n\n"
        "IMPORTANT: \"averages\" and \"targets_vs_actual\" are already "
        "computed only over the days with real logged data "
        "(days_tracked=" + str(context.get("days_tracked")) + " out of 7) "
        "-- days in untracked_dates had nothing logged at all and are "
        "already excluded from those numbers, NOT counted as 0-calorie "
        "days. Never describe an untracked day as if the user ate "
        "nothing, and never imply the week's average reflects all 7 days "
        "if untracked_dates is non-empty -- say how many days the average "
        "is actually based on. If partial_dates is non-empty, mention "
        "that those specific days look incompletely logged (under half "
        "of target) without treating them as bad eating days.\n\n"
    )
    if previous_context:
        prompt += (
            "Here is the PRIOR week's data, for comparison -- cite at least "
            "one specific week-on-week change (up/down, with the number) if "
            "the data supports it (only compare using days_tracked from "
            "each week, same rule as above), don't just describe the "
            "current week in isolation:\n" + json.dumps(previous_context) + "\n"
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
