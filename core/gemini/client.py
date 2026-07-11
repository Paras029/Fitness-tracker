"""Plain REST access to the Gemini API -- no google-generativeai SDK.

The official SDK pulls in grpcio, which has no prebuilt wheel for
Termux's architecture and takes a very long time (and often fails) to
compile from source on tablet hardware. Talking to the REST API directly
with `requests` sidesteps that entirely.

Every other module in this package (extraction.py, fill.py, review.py,
rating.py) calls through `_call()` here -- it's the one place that knows
about HTTP, auth, response-shape parsing, and the responseSchema/
maxOutputTokens plumbing. Nothing else should touch `requests` directly.

If GEMINI_MODEL starts returning 404s, the model name has likely been
retired -- check https://ai.google.dev/gemini-api/docs/models for the
current free-tier model and update GEMINI_MODEL in .env, or run
`python -m scripts.check_setup` which cross-checks it automatically.
"""

import collections
import json
import logging
import os
import re
import threading
import time

import requests

from core import config

TIMEOUT = 30
_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
log = logging.getLogger("gemini")
DEBUG = os.environ.get("GEMINI_DEBUG") == "1"

# Free-tier Gemini keys are commonly capped around 15 requests/minute --
# chunked lab-report extraction (see health_service.py) can easily fire
# off that many calls for one long report, especially with a few running
# concurrently. Rather than hoping callers space themselves out, every
# call() blocks here until there's room in the last 60s window -- a
# sliding-window throttle shared process-wide (thread-safe: chunk
# extraction runs several calls on a thread pool). Override via
# GEMINI_RPM_LIMIT if a paid key allows more.
GEMINI_RPM_LIMIT = int(os.environ.get("GEMINI_RPM_LIMIT") or "15")
_rate_lock = threading.Lock()
_recent_call_times = collections.deque()


def _throttle_for_rate_limit():
    while True:
        with _rate_lock:
            now = time.monotonic()
            while _recent_call_times and now - _recent_call_times[0] >= 60:
                _recent_call_times.popleft()
            if len(_recent_call_times) < GEMINI_RPM_LIMIT:
                _recent_call_times.append(now)
                return
            wait = 60 - (now - _recent_call_times[0]) + 0.05
        log.info("Gemini rate limit (%d/min) reached -- waiting %.1fs before the next call.", GEMINI_RPM_LIMIT, wait)
        time.sleep(wait)

# call() returns None on any failure (the uniform "didn't work, fall back"
# signal every caller already relies on) -- but a bare None can't tell a
# quota error apart from a truncated response apart from a timeout, and
# for large-file callers (lab/body-comp scan extraction) that difference
# is exactly what the user needs to see instead of one generic message.
# Rather than changing call()'s return contract everywhere, the specific
# reason for the *last* failure is stashed here and callers that care can
# read it right after a None comes back. Thread-local (not a plain module
# global) because chunked lab-report extraction runs several calls
# concurrently on a thread pool -- a shared global would let one thread's
# error silently clobber another's.
_local = threading.local()


def get_last_error():
    return getattr(_local, "last_error", None)


def call(parts, want_json=True, response_schema=None, max_output_tokens=None, use_search=False, timeout=None):
    """use_search=True adds Gemini's google_search grounding tool, so the
    model can check a real page instead of only recalling training data --
    used for the on-demand correction flow, where "look it up" is exactly
    what a user asking for a fix wants. responseSchema is deliberately
    dropped when grounding is on: whether structured output and the search
    tool can be combined varies by model/tier and isn't something this app
    can verify without a live key, so grounded calls fall back to prompt-
    described JSON (parsed by extract_json's regex fallback below) rather
    than risk silently breaking. Callers should treat a grounded call as
    best-effort and retry ungrounded on failure -- see review.py."""
    _local.last_error = None
    if not config.GEMINI_API_KEY:
        _local.last_error = "GEMINI_API_KEY is not set."
        log.warning("GEMINI_API_KEY is not set -- skipping Gemini call.")
        return None
    _throttle_for_rate_limit()
    url = f"{_BASE}/{config.GEMINI_MODEL}:generateContent"
    body = {"contents": [{"parts": parts}]}
    if use_search:
        body["tools"] = [{"google_search": {}}]
    if want_json:
        gen_config = {}
        if not use_search:
            gen_config["responseMimeType"] = "application/json"
            if response_schema:
                # A schema *constrains* generation -- required fields genuinely
                # cannot be omitted, which is a much stronger guarantee than
                # asking nicely in the prompt text and hoping it's followed.
                gen_config["responseSchema"] = response_schema
        if max_output_tokens:
            gen_config["maxOutputTokens"] = max_output_tokens
        if gen_config:
            body["generationConfig"] = gen_config
    if DEBUG:
        log.warning("Gemini request body: %s", json.dumps(body)[:2000])

    try:
        resp = requests.post(
            url, params={"key": config.GEMINI_API_KEY}, json=body, timeout=timeout or TIMEOUT
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.Timeout:
        _local.last_error = f"Timed out waiting on Gemini after {timeout or TIMEOUT}s -- a large file (many pages / high-res photo) can take longer than that to process."
        log.warning("Gemini call to model '%s' timed out after %ss.", config.GEMINI_MODEL, timeout or TIMEOUT)
        return None
    except requests.RequestException as e:
        status = getattr(e.response, "status_code", None)
        detail = e.response.text if getattr(e, "response", None) is not None else str(e)
        if status == 429:
            _local.last_error = "Gemini quota/rate limit hit (HTTP 429) -- wait a bit and retry, or check your plan's limits."
        elif status == 413 or (status == 400 and "large" in detail.lower()):
            _local.last_error = "The upload is too large for Gemini's request size limit -- try a smaller file (fewer pages, lower-res scan, or split it up)."
        elif status:
            _local.last_error = f"Gemini request failed (HTTP {status}): {detail[:200]}"
        else:
            _local.last_error = f"Gemini request failed: {detail[:200]}"
        log.warning("Gemini call to model '%s' failed: %s", config.GEMINI_MODEL, detail if DEBUG else detail[:300])
        return None

    if DEBUG:
        log.warning("Gemini raw response: %s", json.dumps(data)[:4000])

    candidates = data.get("candidates") or []
    finish_reason = candidates[0].get("finishReason") if candidates else data.get("promptFeedback", {}).get("blockReason")
    if finish_reason and finish_reason not in ("STOP", None):
        # Common ones: MAX_TOKENS (response got cut off -- bump maxOutputTokens
        # or shorten the prompt), SAFETY / PROHIBITED_CONTENT (a food photo or
        # description tripped a safety filter), RECITATION.
        if finish_reason == "MAX_TOKENS":
            _local.last_error = "Gemini's response got cut off before finishing (too much to extract in one go, e.g. a very long report) -- try uploading a shorter excerpt (just the pages you need) instead of the whole document."
        else:
            _local.last_error = f"Gemini stopped early with reason '{finish_reason}' (often a safety filter on the file's content)."
        log.warning("Gemini finished with reason '%s' instead of a normal stop -- "
                    "this usually means the response was blocked or truncated, not a bug in the request.",
                    finish_reason)

    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError):
        if not _local.last_error:
            _local.last_error = "Gemini returned no usable response."
        log.warning("Gemini response had no usable candidate (finishReason=%s): %s",
                    finish_reason, json.dumps(data)[:300])
        return None

    if not want_json:
        return text
    result = extract_json(text)
    if result is None:
        if not _local.last_error:
            _local.last_error = "Gemini's response wasn't valid JSON."
        log.warning("Gemini response wasn't valid JSON: %s", text[:300])
    return result


def extract_json(text):
    cleaned = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"(\[.*\]|\{.*\})", cleaned, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                return None
    return None
