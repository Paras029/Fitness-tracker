"""Shared between fill.py and review.py -- both send Gemini a per-100g
nutrient object schema built from whatever keys are currently tracked."""

NUTRIENT_KEYS = [
    "kcal", "protein", "carbs", "fat", "fiber", "sugar",
    "sodium", "potassium", "vitamin_c", "iron", "calcium", "vitamin_d",
]
CORE_MACRO_KEYS = ("kcal", "protein", "carbs", "fat")


def nutrient_props(keys):
    return {k: {"type": "NUMBER"} for k in keys}
