"""On-demand only, like review.py's sanity_check_meal -- never runs
automatically, costs one Gemini call per invocation. Turns a body
composition entry (plus recent history for trend context) into a
0-100 score and a short narrative, the same "coach summarizing already-
computed data" role rating.py's generate_daily_report plays for a day of
eating.

Input:  entry (a body_comp_entries row, dict), history (recent entries,
        oldest-relevant-first, for trend framing -- may be empty).
Output: {"score": 0-100, "summary": str, "highlights": [str,...], "watch": str|null}
"""

import json

from core.gemini.client import call

_INSTRUCTIONS = (
    "You are a body-composition coach summarizing one scan/weigh-in for a "
    "fitness-tracking app, scored out of 100. This is NOT a medical "
    "diagnosis -- score general body composition balance (body fat level, "
    "muscle mass relative to weight, visceral fat if present) and, if "
    "history is given, trend direction (improving/stable/declining).\n\n"
    "This entry: {entry_json}\n\n"
    "Recent history (oldest first, may be empty): {history_json}\n\n"
    "Respond with JSON: {\"score\": integer 0-100 (100 = excellent balance "
    "and/or clearly improving trend), \"summary\": 2-3 sentence narrative "
    "about this entry and, if history exists, how it's trending, "
    "\"highlights\": array of 1-3 short positive-or-neutral observations, "
    "\"watch\": one short sentence flagging the single most notable "
    "concern (e.g. high visceral fat, declining muscle mass), or null if "
    "nothing stands out}"
)

_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "score": {"type": "INTEGER"},
        "summary": {"type": "STRING"},
        "highlights": {"type": "ARRAY", "items": {"type": "STRING"}},
        "watch": {"type": "STRING", "nullable": True},
    },
    "required": ["score", "summary", "highlights"],
}


def generate_body_comp_summary(entry, history=None):
    prompt = (_INSTRUCTIONS
              .replace("{entry_json}", json.dumps(entry))
              .replace("{history_json}", json.dumps(history or [])))
    result = call([{"text": prompt}], response_schema=_RESPONSE_SCHEMA, max_output_tokens=500)
    if not isinstance(result, dict) or "score" not in result:
        return None
    result.setdefault("watch", None)
    return result
