"""Parses a lab/blood-test report (PDF or photo) into structured test
results. Extraction only -- like extraction.py's ingredient stage, this
never decides whether a value is in/out of range; that's a deterministic
comparison against ref_low/ref_high done by health_service.py once the
draft comes back here.

Input:  pdf_bytes or image_bytes (exactly one), plus its mime_type.
Output: {
  "tests": [
    {"test_name": str, "category_hint": str, "value": number|null,
     "unit": str|null, "ref_low": number|null, "ref_high": number|null,
     "ref_text": str|null},
    ...
  ],
  "report_date": str|null,   -- YYYY-MM-DD if visible on the report
  "notes": str|null
}
"""

import base64

from core.gemini.client import call

_INSTRUCTIONS = (
    "You are a medical lab report extraction assistant. Your ONLY job is reading "
    "every individual test result off this report and structuring it -- never "
    "judge whether a value is healthy or concerning, that happens elsewhere.\n\n"
    "For each distinct test/measurement, determine:\n"
    "- test_name: the test's name as printed (e.g. \"Total Cholesterol\", \"Hemoglobin\", \"Creatinine\")\n"
    "- category_hint: which broad panel it belongs to -- a short lowercase word "
    "like \"heart\", \"kidney\", \"liver\", \"cbc\", \"urine\", \"thyroid\", \"diabetes\", "
    "or \"other\" if none fit. Guess from context (panel heading, nearby tests).\n"
    "- value: the numeric result, or null if it's non-numeric (e.g. \"Negative\"/\"Trace\") --"
    " in that case put the text in ref_text instead and leave value null.\n"
    "- unit: the unit exactly as printed (e.g. \"mg/dL\"), or null.\n"
    "- ref_low / ref_high: the numeric reference range bounds if the report prints one "
    "(e.g. \"70-100\" -> ref_low=70, ref_high=100), else null.\n"
    "- ref_text: the reference range or normal value AS PRINTED, verbatim, if it isn't "
    "a clean numeric range (e.g. \"Negative\", \"< 5\"), else null.\n\n"
    "Also report report_date (the collection/report date on the document, YYYY-MM-DD) "
    "if visible, else null, and notes for anything ambiguous or illegible.\n\n"
    "Respond with JSON only: {\"tests\": [{\"test_name\":str, \"category_hint\":str, "
    "\"value\":number|null, \"unit\":str|null, \"ref_low\":number|null, \"ref_high\":number|null, "
    "\"ref_text\":str|null}, ...], \"report_date\": str|null, \"notes\": str|null}"
)

_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "tests": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "test_name": {"type": "STRING"},
                    "category_hint": {"type": "STRING"},
                    "value": {"type": "NUMBER", "nullable": True},
                    "unit": {"type": "STRING", "nullable": True},
                    "ref_low": {"type": "NUMBER", "nullable": True},
                    "ref_high": {"type": "NUMBER", "nullable": True},
                    "ref_text": {"type": "STRING", "nullable": True},
                },
                "required": ["test_name", "category_hint"],
            },
        },
        "report_date": {"type": "STRING", "nullable": True},
        "notes": {"type": "STRING", "nullable": True},
    },
    "required": ["tests"],
}


def extract_lab_results(pdf_bytes=None, image_bytes=None, mime_type=None, caption=None):
    """Returns the parsed dict described above, or None on total failure
    (caller should treat that as "couldn't parse, ask the user to retry
    with a clearer scan"). caption is optional free text the user typed
    alongside the upload (e.g. "this is a follow-up thyroid panel, ignore
    the first page") -- used to disambiguate, never to override what's
    actually printed on the document."""
    file_bytes = pdf_bytes or image_bytes
    if not file_bytes or not mime_type:
        return None

    instructions = _INSTRUCTIONS
    if caption:
        instructions += (
            f'\n\nThe user added this note about the upload: "{caption}" -- use it for context '
            "(e.g. which panel this is, what to focus on) but never let it override what's "
            "actually printed on the document."
        )
    parts = [
        {"text": instructions},
        {"inline_data": {"mime_type": mime_type, "data": base64.b64encode(file_bytes).decode("ascii")}},
    ]

    result = call(parts, response_schema=_RESPONSE_SCHEMA, max_output_tokens=4096)
    if not isinstance(result, dict) or "tests" not in result:
        return None
    result.setdefault("report_date", None)
    result.setdefault("notes", None)
    return result
