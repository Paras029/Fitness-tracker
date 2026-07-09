"""Fuzzy matching against previously logged/corrected foods.

Checking the cache before calling any external API is what keeps this app
inside the free-tier request budgets over time -- every correction the user
makes gets remembered, so the same meal gets recognized instantly (and more
accurately) the second time.
"""

import difflib
import re

from core import db

MATCH_THRESHOLD = 0.82


def normalize(text):
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9\s]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text


def find_cached_match(text):
    """Returns the best matching food_cache row (dict) if it's a
    confident-enough match, else None."""
    norm = normalize(text)
    if not norm:
        return None

    exact = db.search_food_cache_exact(norm)
    if exact:
        return exact

    best_row, best_ratio = None, 0.0
    for row in db.all_food_cache():
        ratio = difflib.SequenceMatcher(None, norm, row["match_text"]).ratio()
        if ratio > best_ratio:
            best_row, best_ratio = row, ratio

    if best_row and best_ratio >= MATCH_THRESHOLD:
        return best_row
    return None
