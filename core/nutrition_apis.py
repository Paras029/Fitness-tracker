"""Thin clients for the free-tier nutrition data APIs.

Every function returns a normalized dict subset of:
    {kcal, protein, carbs, fat, fiber, sugar, sodium, potassium,
     vitamin_c, iron, calcium, vitamin_d}
Missing values are simply omitted -- callers merge results from multiple
sources and fill gaps, they don't assume every key is present.

Field names for third-party APIs are recalled from memory and may drift as
the providers evolve their schemas -- if a call starts returning empty
results, check the field names against current docs before assuming the
whole integration is broken:
  https://api-ninjas.com/api/nutrition
  https://developer.edamam.com/food-database-api-docs
  https://openfoodfacts.github.io/openfoodfacts-server/api/
"""

import requests

from core import config

TIMEOUT = 10


def _get(url, **kwargs):
    try:
        resp = requests.get(url, timeout=TIMEOUT, **kwargs)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException:
        return None


def _post(url, **kwargs):
    try:
        resp = requests.post(url, timeout=TIMEOUT, **kwargs)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException:
        return None


# ---------------- CalorieNinjas ----------------
# Free tier: 10,000 requests/month. Great at parsing a natural-language
# description ("1 bowl of dal and 2 rotis") straight into itemized macros.

def parse_calorieninjas(text):
    """Returns a list of {name, kcal, protein, carbs, fat, fiber, sugar,
    sodium, potassium} dicts, one per food item CalorieNinjas detected."""
    if not config.CALORIENINJAS_API_KEY:
        return []
    data = _get(
        "https://api.calorieninjas.com/v1/nutrition",
        params={"query": text},
        headers={"X-Api-Key": config.CALORIENINJAS_API_KEY},
    )
    if not data or "items" not in data:
        return []
    items = []
    for it in data["items"]:
        items.append({
            "name": it.get("name", text),
            "kcal": it.get("calories"),
            "protein": it.get("protein_g"),
            "carbs": it.get("carbohydrates_total_g"),
            "fat": it.get("fat_total_g"),
            "fiber": it.get("fiber_g"),
            "sugar": it.get("sugar_g"),
            "sodium": it.get("sodium_mg"),
            "potassium": it.get("potassium_mg"),
        })
    return items


# ---------------- Edamam Food Database ----------------
# Free tier: 10,000 requests/month. Best for multi-ingredient recipes and
# deeper micronutrient coverage than CalorieNinjas offers.

_EDAMAM_NUTRIENT_MAP = {
    "ENERC_KCAL": "kcal",
    "PROCNT": "protein",
    "CHOCDF": "carbs",
    "FAT": "fat",
    "FIBTG": "fiber",
    "SUGAR": "sugar",
    "NA": "sodium",
    "K": "potassium",
    "VITC": "vitamin_c",
    "FE": "iron",
    "CA": "calcium",
    "VITD": "vitamin_d",
}


def parse_edamam(text):
    """Uses Edamam's ingredient parser as a single-shot lookup. This is the
    simple path (one phrase -> best-guess food -> its per-100g/serving
    nutrients); Edamam's more precise flow re-queries the /nutrients
    endpoint with an exact foodId + measureURI, which is a good next step
    if estimates from this path prove too coarse."""
    if not config.EDAMAM_APP_ID or not config.EDAMAM_APP_KEY:
        return None
    data = _get(
        "https://api.edamam.com/api/food-database/v2/parser",
        params={
            "app_id": config.EDAMAM_APP_ID,
            "app_key": config.EDAMAM_APP_KEY,
            "ingr": text,
        },
    )
    if not data:
        return None
    hints = data.get("hints") or data.get("parsed")
    if not hints:
        return None
    food = hints[0].get("food")
    if not food:
        return None
    nutrients = food.get("nutrients", {})
    out = {"name": food.get("label", text)}
    for edamam_key, our_key in _EDAMAM_NUTRIENT_MAP.items():
        if edamam_key in nutrients:
            out[our_key] = nutrients[edamam_key]
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
