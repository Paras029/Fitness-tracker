"""On-demand only, like body_comp_summary.py -- never runs automatically,
costs one Gemini call per invocation. Summarizes a person's current set of
lab results (across all categories/reports, latest value per test) into a
plain-language narrative, flagging what's out of range and any pattern
across tests. Deliberately NOT scored 0-100 like body_comp_summary --
lab values are diagnostic data with real reference ranges attached
already (the flag field), a synthetic score here would imply more
medical authority than this app has.

Input:  results (list of lab_results rows -- test_name, category_key,
        value, unit, ref_low, ref_high, ref_text, flag, test_date).
Output: {"summary": str, "highlights": [str,...], "watch": str|null}
"""

import json

from core.gemini.client import call

_INSTRUCTIONS = (
    "You are summarizing a person's current lab results for a health-tracking "
    "app. This is NOT a diagnosis -- describe what the numbers show and which "
    "are flagged outside their printed reference range, in plain language a "
    "non-clinician can follow. If the same test appears more than once "
    "(repeat testing over time), note the trend direction.\n\n"
    "Each result already has a \"flag\" computed from its own reference range "
    "(normal/low/high) -- trust that field, don't re-derive it.\n\n"
    "Results: {results_json}\n\n"
    "Respond with JSON: {\"summary\": 2-3 sentence overview of the current "
    "panel, \"highlights\": array of 1-4 short specific observations (cite "
    "actual test names and values), \"watch\": one sentence naming the single "
    "most notable out-of-range result and suggesting they discuss it with a "
    "doctor, or null if everything is in range}"
)

_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "summary": {"type": "STRING"},
        "highlights": {"type": "ARRAY", "items": {"type": "STRING"}},
        "watch": {"type": "STRING", "nullable": True},
    },
    "required": ["summary", "highlights"],
}


def generate_lab_summary(results):
    prompt = _INSTRUCTIONS.replace("{results_json}", json.dumps(results))
    result = call([{"text": prompt}], response_schema=_RESPONSE_SCHEMA, max_output_tokens=500)
    if not isinstance(result, dict) or "summary" not in result:
        return None
    result.setdefault("watch", None)
    return result
