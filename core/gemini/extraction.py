"""STAGE 1: WHAT was eaten and HOW MUCH. Never touches nutrition -- that's
fill.py's job. Takes text and/or an image (plate photo, barcode photo,
ingredient-label photo) and returns a structured ingredient list.

Splitting extraction from nutrition estimation (rather than one blended
call) is deliberate: it lets the deterministic API lookups in
nutrition_apis.py sit between them and do the accuracy-critical work,
with the LLM only ever filling gaps or identifying food -- both things
it's good at -- instead of being the sole source of truth for numbers
it's prone to guessing confidently and wrong.

Input:  raw text and/or an image, optionally a caption.
Output: {
  "items": [
    {"name": str, "grams": number, "grams_confidence": "explicit"|"assumed",
     "is_packaged": bool, "barcode": str|null},
    ...
  ],
  "meal_label": str,               -- short 2-5 word description
  "extraction_confidence": "high"|"medium"|"low",
  "notes": str|null                -- anything ambiguous, for the user to see
}
"""

import base64

from core.gemini.client import call

_INSTRUCTIONS = (
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

_RESPONSE_SCHEMA = {
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

    parts = [{"text": _INSTRUCTIONS}]
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

    result = call(parts, response_schema=_RESPONSE_SCHEMA, max_output_tokens=2048)
    if not isinstance(result, dict) or "items" not in result:
        return None
    result.setdefault("meal_label", None)
    result.setdefault("extraction_confidence", "medium")
    result.setdefault("notes", None)
    return result
