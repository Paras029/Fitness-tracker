"""On-demand review calls -- neither runs automatically. Each is one more
request against a tight daily quota, so both only fire when the user
explicitly asks: a "Double-check this" tap, or a follow-up message about
an already-resolved draft.

sanity_check_meal()  -- reviews a resolved draft's ingredients/weights/
                         macros for internal consistency. Tuned to only
                         flag genuine problems -- estimates naturally
                         carry noise, and a well-resolved meal having
                         *zero* issues is the normal, expected outcome,
                         not something to talk itself out of.

refine_draft()        -- applies a user's free-text message to an
                         already-resolved draft. The message might be an
                         explicit instruction ("it was 150g") to apply
                         exactly, or a question/doubt ("does this look
                         right?") that calls for the same judgment as a
                         sanity check rather than a blind edit -- see
                         _REFINE_INSTRUCTIONS for how that distinction is
                         made.
"""

import json

from core.gemini.client import call
from core.gemini.common import CORE_MACRO_KEYS, NUTRIENT_KEYS, nutrient_props

# ==================== sanity check ====================
#
# Input:  items [{"name", "grams", "nutrients_per_100g"}, ...], totals dict
# Output: {"ok": bool, "flags": [{"item_name", "field", "concern"}, ...], "note": str}

_SANITY_INSTRUCTIONS = (
    "You are reviewing an already-logged meal's ingredient list, portion "
    "weights, and computed macros for internal consistency and plausibility "
    "-- a sanity check, not a re-estimate.\n\n"
    "These are estimates, not lab measurements. Nutrition data from "
    "different sources routinely disagrees by 5-10% for the exact same "
    "food, and that is completely normal -- it is NOT something to flag. "
    "Rounding, brand variation, and reasonable estimation differences are "
    "expected noise, not errors. It is common and expected for a "
    "well-resolved meal to have ZERO issues; do not invent a minor nitpick "
    "just to have something to say. Only set ok=false when you are "
    "confident something is actually, meaningfully wrong.\n\n"
    "Only flag things that are genuinely wrong, such as:\n"
    "- a macro that's off by roughly 20% or more, or an order of magnitude "
    "off, for that food and weight\n"
    "- a weight that clearly doesn't match the described portion (e.g. "
    "\"a banana\" logged at 500g)\n"
    "- protein/carbs/fat that don't add up to the stated calories even "
    "loosely (kcal should be within about 15% of 4*protein + 4*carbs + "
    "9*fat -- fiber, alcohol, and rounding explain small gaps, so only "
    "flag a large one)\n"
    "- a value that's clearly implausible for the named food (e.g. "
    "near-zero protein for a plain meat, triple-digit fat grams per 100g "
    "for a vegetable)\n\n"
    "Do NOT flag: a value being merely a bit higher or lower than you "
    "personally would have guessed, minor brand/recipe variation, or "
    "anything within roughly 5% of what you'd expect.\n\n"
    "Items (name, grams, nutrients per 100g): {items_json}\n"
    "Totals for the whole meal: {totals_json}\n\n"
    "Respond with JSON only: {\"ok\": true if nothing is genuinely wrong "
    "(the common case), false only if you found at least one real issue "
    "by the standard above, \"flags\": [{\"item_name\": str, \"field\": "
    "str, \"concern\": one short specific sentence}, ...] (empty array "
    "when ok), \"note\": one short overall sentence -- reassuring if ok, "
    "specific about the worst issue if not}"
)

_SANITY_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "ok": {"type": "BOOLEAN"},
        "flags": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "item_name": {"type": "STRING"},
                    "field": {"type": "STRING"},
                    "concern": {"type": "STRING"},
                },
                "required": ["item_name", "field", "concern"],
            },
        },
        "note": {"type": "STRING"},
    },
    "required": ["ok", "flags", "note"],
}


def sanity_check_meal(items, totals):
    """items: resolved draft items (name, grams, nutrients_per_100g).
    Returns the shape above, or None if the call failed."""
    items_str = json.dumps([{"name": it["name"], "grams": it.get("grams"),
                              "nutrients_per_100g": it.get("nutrients_per_100g")} for it in items])
    prompt = _SANITY_INSTRUCTIONS.replace("{items_json}", items_str).replace("{totals_json}", json.dumps(totals))
    result = call([{"text": prompt}], response_schema=_SANITY_RESPONSE_SCHEMA, max_output_tokens=600)
    if not isinstance(result, dict) or "ok" not in result:
        return None
    return result


# ==================== conversational refine ====================
#
# Input:  items, totals, user_message (str), sanity (dict|None)
# Output: {"items": [the COMPLETE corrected item list], "changed": [names],
#          "action": "corrected"|"confirmed", "note": str}

_REFINE_INSTRUCTIONS = (
    "You previously resolved this meal log. The user has now sent a "
    "follow-up message about it. First work out what kind of message it "
    "is, because that changes what you should do:\n\n"
    "(a) An EXPLICIT INSTRUCTION or correction -- a specific fact stated "
    "as true (\"it was 150g\", \"remove the ketchup\", \"add a side salad, "
    "about 80g\", \"that's chicken thigh not breast\"). Apply exactly what "
    "they said, no matter how small -- they know their own meal better "
    "than you do. Set \"action\":\"corrected\".\n\n"
    "(b) A QUESTION or expressed DOUBT with no specific replacement value "
    "(\"does this look right?\", \"that seems like a lot of calories\", "
    "\"are you sure about the chicken?\"). Do NOT blindly change anything "
    "here -- use the same judgment as a sanity check: only revise a value "
    "if you genuinely believe it's wrong (roughly 20%+ off, or clearly "
    "implausible for the food -- ordinary estimation noise of a few "
    "percent is normal and not an error). If everything already looks "
    "correct, leave every item exactly as it is and explain why in the "
    "note. Set \"action\":\"confirmed\" if you changed nothing, "
    "\"corrected\" if your review did turn up something worth fixing.\n\n"
    "Never change an ITEM the user neither instructed you to change nor "
    "expressed doubt about -- but this is scoped to other items, not to "
    "other FIELDS of the item they did ask about.\n\n"
    "CRITICAL -- when a nutrient value itself is what's being corrected "
    "(not just the weight), remember kcal, protein, carbs, and fat are not "
    "independent: kcal must stay within about 15% of 4*protein + 4*carbs "
    "+ 9*fat (Atwater, standard for all nutrition labels). If the user "
    "corrects one of these four for an item, check whether the other "
    "three still reconcile with the new value -- if they don't anymore, "
    "adjust the ones you're least confident in so the whole item stays "
    "internally consistent, and say what you adjusted (and why) in "
    "\"note\". Example: user says \"that item was actually only 200 "
    "kcal\" and it's currently 165g protein / 0g carbs / 3.6g fat (which "
    "alone implies ~226 kcal, already close) -- if instead protein was "
    "31g and fat was way higher such that the math implied 500+ kcal, "
    "lowering kcal to 200 without touching protein/fat would leave the "
    "item self-contradictory, so scale the macros down too until they "
    "roughly explain 200 kcal. Don't do this reflexively for every tiny "
    "edit -- weight changes, ingredient swaps, and additions/removals "
    "don't need this (grams scale everything proportionally already); it "
    "only applies when a nutrient value is the thing being corrected and "
    "the result would otherwise stop reconciling.\n\n"
    "If you have web search available and the user's correction concerns "
    "a specific known product or a factual nutrition claim you're unsure "
    "of, use it to check a real source rather than relying only on what "
    "you recall -- but don't let searching slow down or block a simple "
    "correction (a stated weight or an added/removed ingredient never "
    "needs a lookup).\n\n"
    "Current items (name, grams, nutrients per 100g): {items_json}\n"
    "Current totals: {totals_json}\n"
    "{sanity_block}"
    "User's message: \"{message}\"\n\n"
    "Respond with JSON only: {\"items\": the COMPLETE item list (same "
    "shape as the input items -- include every item that should still be "
    "in the meal, not only the ones you changed), \"changed\": names of "
    "items you actually added, removed, or modified (empty array if "
    "none), \"action\": \"corrected\"|\"confirmed\", \"note\": one short "
    "sentence for the user -- what you changed and why, or why you left "
    "it as-is}"
)


def _response_schema(keys):
    return {
        "type": "OBJECT",
        "properties": {
            "items": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "name": {"type": "STRING"},
                        "grams": {"type": "NUMBER"},
                        "nutrients_per_100g": {
                            "type": "OBJECT",
                            "properties": nutrient_props(keys),
                            "required": list(CORE_MACRO_KEYS),
                        },
                    },
                    "required": ["name", "grams", "nutrients_per_100g"],
                },
            },
            "changed": {"type": "ARRAY", "items": {"type": "STRING"}},
            "action": {"type": "STRING", "enum": ["corrected", "confirmed"]},
            "note": {"type": "STRING"},
        },
        "required": ["items", "action", "note"],
    }


def refine_draft(items, totals, user_message, sanity=None, nutrient_keys=None, use_search=True):
    """Returns {"items": [...], "changed": [names], "action": str, "note":
    str} or None if the call failed (caller should leave the draft
    untouched and tell the user to try rephrasing).

    use_search: try grounding the correction in a real web search first
    (see client.call's docstring for why this drops responseSchema and
    what that trades off). This is the "remediate" moment -- the one
    place in the pipeline where spending an extra round-trip on a lookup
    is worth it -- so it defaults on, with an automatic, transparent
    fallback to the normal schema-constrained call if grounding isn't
    supported or doesn't come back as usable JSON."""
    if not user_message or not user_message.strip():
        return None
    keys = nutrient_keys or NUTRIENT_KEYS
    items_str = json.dumps([{"name": it["name"], "grams": it.get("grams"),
                              "nutrients_per_100g": it.get("nutrients_per_100g")} for it in items])
    sanity_block = ""
    if sanity and sanity.get("flags"):
        sanity_block = "A sanity check already flagged: " + json.dumps(sanity["flags"]) + "\n"
    prompt = (_REFINE_INSTRUCTIONS
              .replace("{items_json}", items_str)
              .replace("{totals_json}", json.dumps(totals))
              .replace("{sanity_block}", sanity_block)
              .replace("{message}", user_message.strip()))
    max_tokens = min(max(500, 150 * len(items) + 200), 8192)

    result = None
    if use_search:
        result = call([{"text": prompt}], max_output_tokens=max_tokens, use_search=True)
        if not isinstance(result, dict) or "items" not in result:
            result = None  # fall through to the ungrounded, schema-constrained attempt below
    if result is None:
        result = call([{"text": prompt}], response_schema=_response_schema(keys), max_output_tokens=max_tokens)

    if not isinstance(result, dict) or "items" not in result:
        return None
    result.setdefault("changed", [])
    result.setdefault("action", "corrected" if result["changed"] else "confirmed")
    return result
