"""Maps extracted test names onto this install's ACTUAL lab categories
(built-ins plus whatever the user has added/renamed) -- a dedicated call
rather than folded into extraction, for two reasons: extraction doesn't
know the real category list (it only guesses a free-text category_hint
per chunk, and a long report is extracted in multiple chunks that each
only see part of the document), and this call runs once, after
consolidation, with the full picture -- the actual category list AND
every test in the report at once, which is a much easier and more
consistent categorization job than guessing chunk-by-chunk.

Input:  tests (list of {test_name, category_hint}), categories (list of
        {key, label} from health_db.list_lab_categories()).
Output: {"assignments": [{"test_name": str, "category_key": str}, ...]}
        -- category_key is always one of the given categories' keys.
"""

import json

from core.gemini.client import call

_INSTRUCTIONS = (
    "You are categorizing medical test names into a fixed set of panels for a "
    "health-tracking app. For each test below, pick the single best-fitting "
    "category from the AVAILABLE CATEGORIES list -- use the test's own name, "
    "its rough category_hint, and general medical knowledge of what panel it "
    "belongs to. You MUST use one of the given category keys for every test "
    "(pick the closest fit, or the \"other\"-labeled one if truly nothing fits) "
    "-- never invent a new key.\n\n"
    "Available categories: {categories_json}\n\n"
    "Tests to categorize: {tests_json}\n\n"
    "Respond with JSON: {\"assignments\": [{\"test_name\": str, \"category_key\": str}, ...]} "
    "-- one entry per test, in any order, test_name copied exactly as given."
)

_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "assignments": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "test_name": {"type": "STRING"},
                    "category_key": {"type": "STRING"},
                },
                "required": ["test_name", "category_key"],
            },
        },
    },
    "required": ["assignments"],
}


def categorize_lab_tests(tests, categories):
    """tests: [{"test_name":str, "category_hint":str}, ...]
    categories: [{"key":str, "label":str}, ...]
    Returns {test_name: category_key} for every test that got a valid
    assignment (missing/invalid ones are just absent -- caller should
    fall back to local fuzzy matching for those), or {} on total failure."""
    if not tests or not categories:
        return {}
    valid_keys = {c["key"] for c in categories}
    prompt = (_INSTRUCTIONS
              .replace("{categories_json}", json.dumps([{"key": c["key"], "label": c["label"]} for c in categories]))
              .replace("{tests_json}", json.dumps(tests)))
    result = call([{"text": prompt}], response_schema=_RESPONSE_SCHEMA, max_output_tokens=4096)
    if not isinstance(result, dict):
        return {}
    out = {}
    for a in result.get("assignments", []):
        key = a.get("category_key")
        name = a.get("test_name")
        if name and key in valid_keys:
            out[name] = key
    return out
