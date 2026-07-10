"""STAGE 2 (post API lookup): turns an ingredient list (plus whatever
partial data the nutrition APIs already found) into a complete per-100g
macro/micro profile per item. API data is treated as ground truth; this
call only fills the gaps API left, or estimates from scratch when API
found nothing.

Input:  [{"name": str, "grams": number, "api_data": {partial per-100g dict}|{},
          "api_source": str|null}, ...]
Output: {"items": [
    {"name": str,
     "nutrients_per_100g": {kcal, protein, carbs, fat, ...whatever else is
                             currently tracked, see nutrient_keys below},
     "confidence": "database"|"llm_filled"|"llm_estimated",
     "notes": str|null},
    ...
]}
"""

import json

from core.gemini.client import call
from core.gemini.common import CORE_MACRO_KEYS, NUTRIENT_KEYS, nutrient_props

_INSTRUCTIONS = (
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


def _response_schema(keys):
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
                            "properties": nutrient_props(keys),
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
    prompt = (_INSTRUCTIONS
              .replace("{count}", str(len(items)))
              .replace("{extra_fields}", extra_fields)
              .replace("{items_json}", json.dumps(payload)))
    # ~150 tokens/item is generous for a 12-field nutrient object; floor of
    # 400 covers the fixed overhead for a single-item call.
    max_tokens = min(max(400, 150 * len(items)), 8192)
    result = call([{"text": prompt}], response_schema=_response_schema(keys), max_output_tokens=max_tokens)
    if not isinstance(result, dict) or "items" not in result:
        return None
    return result
