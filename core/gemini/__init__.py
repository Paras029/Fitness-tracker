"""Gemini access, split by pipeline stage -- each submodule is one job:

  extraction.py  extract_ingredients()   STAGE 1: WHAT was eaten and HOW
                                          MUCH. Never touches nutrition.

  fill.py        fill_nutrition()        STAGE 2 (post API lookup): turns
                                          an ingredient list into a
                                          complete per-100g nutrient
                                          profile, treating API data as
                                          ground truth.

  review.py      sanity_check_meal()     ON-DEMAND only -- neither runs
                 refine_draft()          automatically, each costs one
                                          more request. sanity_check_meal
                                          reviews a resolved draft for
                                          internal consistency;
                                          refine_draft applies a user's
                                          free-text correction or answers
                                          their concern about it.

  rating.py      rate_meal()             STAGE 3: healthiness rating for
                 generate_daily_report() a confirmed meal, the daily and
                 generate_weekly_report()weekly reports, and free-form
                 answer_question()       Q&A over already-computed data.
                 transcribe_voice()

  client.py                              Shared HTTP plumbing (the only
                                          module that touches `requests`)
                                          and response-JSON parsing.

  lab_extraction.py extract_lab_results() Health-domain extraction: reads
                                          a lab/blood-test report (PDF or
                                          photo) into structured test
                                          results. Same extraction-only
                                          discipline as extraction.py --
                                          in/out-of-range judgment happens
                                          in core/health_service.py.

  common.py                              Nutrient-key constants and
                                          schema helpers shared by fill.py
                                          and review.py.

Everything below is re-exported here so callers keep writing
`from core import gemini; gemini.extract_ingredients(...)` -- the split
is an internal reorganization, not a change to the public API.
"""

from core.gemini.client import DEBUG, TIMEOUT, call as _call, extract_json as _extract_json
from core.gemini.common import CORE_MACRO_KEYS, NUTRIENT_KEYS
from core.gemini.extraction import extract_ingredients
from core.gemini.fill import fill_nutrition
from core.gemini.lab_extraction import extract_lab_results
from core.gemini.rating import (
    answer_question, generate_daily_report, generate_weekly_report, rate_meal, transcribe_voice,
)
from core.gemini.review import refine_draft, sanity_check_meal

__all__ = [
    "extract_ingredients",
    "fill_nutrition",
    "sanity_check_meal",
    "refine_draft",
    "rate_meal",
    "generate_daily_report",
    "generate_weekly_report",
    "answer_question",
    "transcribe_voice",
    "extract_lab_results",
    "NUTRIENT_KEYS",
    "CORE_MACRO_KEYS",
]
