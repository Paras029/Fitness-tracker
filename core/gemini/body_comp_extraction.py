"""Parses a body-composition scan (InBody-style printout, smart-scale app
screenshot, or similar) as a photo or PDF into structured fields.
Extraction only -- like lab_extraction.py, no scoring or judgment happens
here; that's body_comp_summary.py's job, on demand.

Input:  pdf_bytes or image_bytes (exactly one), plus its mime_type.
Output: {
  "weight_kg": number|null, "body_fat_pct": number|null,
  "skeletal_muscle_kg": number|null, "visceral_fat": number|null,
  "bmr": number|null, "body_water_pct": number|null,
  "scan_date": str|null,
  "segments": {
    "right_arm": {"lean_kg": number|null, "fat_kg": number|null},
    "left_arm": {...}, "trunk": {...}, "right_leg": {...}, "left_leg": {...}
  } | null,
  "notes": str|null
}
"""

import base64

from core.gemini.client import call

_INSTRUCTIONS = (
    "You are a body-composition scan extraction assistant (InBody-style printouts, "
    "smart scale app screenshots, or similar). Your ONLY job is reading the "
    "measurements off this document -- never judge whether a value is healthy, "
    "that happens elsewhere.\n\n"
    "Extract, if present:\n"
    "- weight_kg, body_fat_pct, skeletal_muscle_kg, visceral_fat (index number), "
    "bmr (kcal), body_water_pct -- the headline whole-body metrics.\n"
    "- scan_date: the date on the report (YYYY-MM-DD), else null.\n"
    "- segments: if the report has a \"Segmental Lean Analysis\" and/or "
    "\"Segmental Fat Analysis\" section (common on InBody scans), extract "
    "lean_kg and fat_kg for right_arm, left_arm, trunk, right_leg, left_leg. "
    "If segmental data isn't on this report, set segments to null entirely "
    "rather than guessing values.\n\n"
    "Respond with JSON only: {\"weight_kg\":number|null, \"body_fat_pct\":number|null, "
    "\"skeletal_muscle_kg\":number|null, \"visceral_fat\":number|null, \"bmr\":number|null, "
    "\"body_water_pct\":number|null, \"scan_date\":str|null, "
    "\"segments\": {\"right_arm\":{\"lean_kg\":number|null,\"fat_kg\":number|null}, "
    "\"left_arm\":{...}, \"trunk\":{...}, \"right_leg\":{...}, \"left_leg\":{...}} | null, "
    "\"notes\": a short string flagging anything illegible or ambiguous, or null}"
)

_SEGMENT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "lean_kg": {"type": "NUMBER", "nullable": True},
        "fat_kg": {"type": "NUMBER", "nullable": True},
    },
}

_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "weight_kg": {"type": "NUMBER", "nullable": True},
        "body_fat_pct": {"type": "NUMBER", "nullable": True},
        "skeletal_muscle_kg": {"type": "NUMBER", "nullable": True},
        "visceral_fat": {"type": "NUMBER", "nullable": True},
        "bmr": {"type": "NUMBER", "nullable": True},
        "body_water_pct": {"type": "NUMBER", "nullable": True},
        "scan_date": {"type": "STRING", "nullable": True},
        "segments": {
            "type": "OBJECT",
            "nullable": True,
            "properties": {
                "right_arm": _SEGMENT_SCHEMA, "left_arm": _SEGMENT_SCHEMA,
                "trunk": _SEGMENT_SCHEMA, "right_leg": _SEGMENT_SCHEMA, "left_leg": _SEGMENT_SCHEMA,
            },
        },
        "notes": {"type": "STRING", "nullable": True},
    },
    "required": ["weight_kg"],
}


def extract_body_comp_scan(pdf_bytes=None, image_bytes=None, mime_type=None, caption=None):
    """Returns the parsed dict described above, or None on total failure.
    caption is optional free text the user typed alongside the upload --
    used for context only, never to override what's on the scan itself."""
    file_bytes = pdf_bytes or image_bytes
    if not file_bytes or not mime_type:
        return None

    instructions = _INSTRUCTIONS
    if caption:
        instructions += (
            f'\n\nThe user added this note about the upload: "{caption}" -- use it for context '
            "but never let it override what's actually printed on the scan."
        )
    parts = [
        {"text": instructions},
        {"inline_data": {"mime_type": mime_type, "data": base64.b64encode(file_bytes).decode("ascii")}},
    ]

    result = call(parts, response_schema=_RESPONSE_SCHEMA, max_output_tokens=1024)
    if not isinstance(result, dict):
        return None
    for key in ("weight_kg", "body_fat_pct", "skeletal_muscle_kg", "visceral_fat",
                "bmr", "body_water_pct", "scan_date", "segments", "notes"):
        result.setdefault(key, None)
    return result
