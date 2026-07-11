"""Parses a lab/blood-test report (PDF or photo) into structured test
results. Extraction only -- like extraction.py's ingredient stage, this
never decides whether a value is in/out of range; that's a deterministic
comparison against ref_low/ref_high done by health_service.py once the
draft comes back here.

Input:  pdf_bytes or image_bytes (exactly one) -- may be a full report or
        just a page-range chunk of one, see health_service.py's chunked
        extraction for long PDFs -- plus its mime_type.
Output: {
  "tests": [
    {"test_name": str, "category_hint": str, "value": number|null,
     "unit": str|null, "ref_low": number|null, "ref_high": number|null,
     "ref_text": str|null, "description": str|null, "how_to_read": str|null},
    ...
  ],
  "report_date": str|null,   -- YYYY-MM-DD if visible on the report
  "notes": str|null
}
"""

import base64

from core.gemini.client import call

# Long/scanned reports take Gemini noticeably longer to read than a
# single-page food photo -- the client module's default 30s timeout was
# tuned for that, not a multi-page PDF. See health_service.py for the
# matching file-size preflight check (done there, before bytes get this
# far, so it can give a specific error instead of a wasted request).
_TIMEOUT_SECONDS = 100
# A comprehensive panel (dozens of pages, 100+ discrete results) needs
# more room than a typical extraction response -- 4096 tokens was cutting
# large reports off mid-JSON.
_MAX_OUTPUT_TOKENS = 8192

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
    "a clean numeric range (e.g. \"Negative\", \"< 5\"), else null.\n"
    "- description: ONLY if the report itself prints an explanation of what this test "
    "measures (some reports include a short blurb per test/panel) -- copy/summarize it "
    "in 1 sentence. Leave null if the report doesn't explain it; do NOT invent one from "
    "general knowledge here, that happens in a separate step.\n"
    "- how_to_read: ONLY if the report itself prints guidance on interpreting the value "
    "(e.g. \"higher indicates inflammation\") -- 1 sentence, verbatim/summarized from the "
    "report. Leave null if the report doesn't say.\n\n"
    "Also report report_date (the collection/report date on the document, YYYY-MM-DD) "
    "if visible, else null, and notes for anything ambiguous or illegible.\n\n"
    "Respond with JSON only: {\"tests\": [{\"test_name\":str, \"category_hint\":str, "
    "\"value\":number|null, \"unit\":str|null, \"ref_low\":number|null, \"ref_high\":number|null, "
    "\"ref_text\":str|null, \"description\":str|null, \"how_to_read\":str|null}, ...], "
    "\"report_date\": str|null, \"notes\": str|null}"
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
                    "description": {"type": "STRING", "nullable": True},
                    "how_to_read": {"type": "STRING", "nullable": True},
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

    result = call(parts, response_schema=_RESPONSE_SCHEMA,
                  max_output_tokens=_MAX_OUTPUT_TOKENS, timeout=_TIMEOUT_SECONDS)
    if not isinstance(result, dict) or "tests" not in result:
        return None
    result.setdefault("report_date", None)
    result.setdefault("notes", None)
    return result
