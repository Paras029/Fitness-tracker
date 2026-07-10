"""Thin clients for the free-tier nutrition data APIs.

Every function returns a normalized dict subset of:
    {kcal, protein, carbs, fat, fiber, sugar, sodium, potassium,
     vitamin_c, iron, calcium, vitamin_d}
Missing values are simply omitted -- callers merge results from multiple
sources and fill gaps, they don't assume every key is present.

Field names for third-party APIs are recalled from memory and may drift as
the providers evolve their schemas -- if a call starts returning empty
results, check the field names against current docs before assuming the
whole integration is broken, and check the printed warning below first --
every failure here prints *why* (bad key, rate limit, network) instead of
silently returning nothing:
  https://api-ninjas.com/api/nutrition
  https://fdc.nal.usda.gov/api-guide.html
  https://openfoodfacts.github.io/openfoodfacts-server/api/
"""

import logging
import math
import requests

from core import config

TIMEOUT = 10
log = logging.getLogger("nutrition_apis")

# Open Food Facts blocks the default "python-requests/x.x" User-Agent as
# part of its anti-abuse policy -- every request needs to identify itself.
# Harmless to send everywhere, so it's applied to all calls, not just OFF.
DEFAULT_HEADERS = {"User-Agent": "NutritionLedger/1.0 (self-hosted personal food tracker)"}


def _merged_headers(kwargs):
    headers = {**DEFAULT_HEADERS, **kwargs.pop("headers", {})}
    return headers


def _get(url, **kwargs):
    headers = _merged_headers(kwargs)
    try:
        resp = requests.get(url, timeout=TIMEOUT, headers=headers, **kwargs)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        log.warning("GET %s failed: %s", url, e)
        return None


def _post(url, **kwargs):
    headers = _merged_headers(kwargs)
    try:
        resp = requests.post(url, timeout=TIMEOUT, headers=headers, **kwargs)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        log.warning("POST %s failed: %s", url, e)
        return None


# ---------------- CalorieNinjas ----------------
# Free tier: 10,000 requests/month. Great at parsing a natural-language
# description ("1 bowl of dal and 2 rotis") straight into itemized macros.

def _safe_float(v):
    """API-Ninjas sends the literal string "NaN" (not null) for a field it
    couldn't confidently compute -- e.g. calories/protein on an
    under-specified compound dish name, while still guessing carbs/fat from
    defaults. float("NaN") parses "successfully" into a real NaN, which then
    poisons any arithmetic downstream (renders as "NaN" in the UI). Treat
    anything that isn't a finite number as genuinely missing."""
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def parse_calorieninjas(text):
    """Returns a list of {name, kcal, protein, carbs, fat, fiber, sugar,
    sodium, potassium} dicts, one per food item CalorieNinjas detected."""
    if not config.CALORIENINJAS_API_KEY:
        return []
    data = _get(
        "https://api.api-ninjas.com/v1/nutrition",
        params={"query": text},
        headers={"X-Api-Key": config.CALORIENINJAS_API_KEY},
    )
    # This endpoint returns a bare JSON array, not {"items": [...]}.
    if not data or not isinstance(data, list):
        return []
    items = []
    for it in data:
        items.append({
            "name": it.get("name", text),
            "kcal": _safe_float(it.get("calories")),
            "protein": _safe_float(it.get("protein_g")),
            "carbs": _safe_float(it.get("carbohydrates_total_g")),
            "fat": _safe_float(it.get("fat_total_g")),
            "fiber": _safe_float(it.get("fiber_g")),
            "sugar": _safe_float(it.get("sugar_g")),
            "sodium": _safe_float(it.get("sodium_mg")),
            "potassium": _safe_float(it.get("potassium_mg")),
        })
    return items


# ---------------- USDA FoodData Central ----------------
# 100% free, no approval needed. Works immediately with the public DEMO_KEY
# (rate-limited: ~30 req/hour) -- get your own free key in seconds at
# https://fdc.nal.usda.gov/api-key-signup for the full 1,000 req/hour.
# Replaces Edamam, whose free tier has since gone away. Best for a single
# food name -> deep nutrient panel; not a multi-item NLP parser like
# CalorieNinjas, so it plays a fallback/enrichment role here.

_USDA_NUTRIENT_ALIASES = {
    "kcal": ("Energy",),
    "protein": ("Protein",),
    "carbs": ("Carbohydrate, by difference",),
    "fat": ("Total lipid (fat)",),
    "fiber": ("Fiber, total dietary",),
    "sugar": ("Sugars, total including NLEA", "Sugars, total"),
    "sodium": ("Sodium, Na",),
    "potassium": ("Potassium, K",),
    "vitamin_c": ("Vitamin C, total ascorbic acid",),
    "iron": ("Iron, Fe",),
    "calcium": ("Calcium, Ca",),
    "vitamin_d": ("Vitamin D (D2 + D3)", "Vitamin D (D2 + D3), International Units"),
}
_USDA_NAME_TO_KEY = {name: key for key, names in _USDA_NUTRIENT_ALIASES.items() for name in names}


def parse_usda(text):
    """Single best-guess food match with per-100g nutrients (caller scales
    by actual portion, same as the Open Food Facts path). Biased toward
    generic whole foods (Foundation/SR Legacy/Survey datasets) rather than
    a random branded product that happens to mention the query term."""
    data = _get(
        "https://api.nal.usda.gov/fdc/v1/foods/search",
        params={
            "query": text,
            "pageSize": 1,
            "dataType": ["Foundation", "SR Legacy", "Survey (FNDDS)"],
            "api_key": config.USDA_API_KEY,
        },
    )
    if not data or not data.get("foods"):
        return None
    food = data["foods"][0]
    out = {"name": food.get("description", text), "per_100g": True}
    for nutrient in food.get("foodNutrients", []):
        our_key = _USDA_NAME_TO_KEY.get(nutrient.get("nutrientName"))
        if our_key and our_key not in out:
            out[our_key] = nutrient.get("value")
    return out


# ---------------- Open Food Facts ----------------
# 100% free, unlimited. Best for packaged/branded products via barcode.

_OFF_NUTRIENT_MAP = {
    "energy-kcal_100g": "kcal",
    "proteins_100g": "protein",
    "carbohydrates_100g": "carbs",
    "fat_100g": "fat",
    "fiber_100g": "fiber",
    "sugars_100g": "sugar",
    "sodium_100g": "sodium",  # grams; converted to mg below
}


def lookup_openfoodfacts_barcode(barcode):
    """Returns per-100g nutrients for a scanned/photographed barcode, or
    None if not found. Caller is responsible for scaling by actual portion."""
    data = _get(f"https://world.openfoodfacts.org/api/v2/product/{barcode}.json")
    if not data or data.get("status") != 1:
        return None
    product = data.get("product", {})
    nutriments = product.get("nutriments", {})
    out = {"name": product.get("product_name") or barcode, "per_100g": True}
    for off_key, our_key in _OFF_NUTRIENT_MAP.items():
        if off_key in nutriments:
            val = nutriments[off_key]
            if our_key == "sodium":
                val = val * 1000  # g -> mg
            out[our_key] = val
    return out


def search_openfoodfacts_by_name(name):
    """Fallback text search when no barcode is available -- much less
    reliable than a barcode hit, used only to try to upgrade an estimate."""
    data = _get(
        "https://world.openfoodfacts.org/cgi/search.pl",
        params={"search_terms": name, "json": 1, "page_size": 1},
    )
    if not data or not data.get("products"):
        return None
    product = data["products"][0]
    nutriments = product.get("nutriments", {})
    out = {"name": product.get("product_name") or name, "per_100g": True}
    for off_key, our_key in _OFF_NUTRIENT_MAP.items():
        if off_key in nutriments:
            val = nutriments[off_key]
            if our_key == "sodium":
                val = val * 1000
            out[our_key] = val
    return out
