"""Standalone sanity check for every API key in .env.

Run this whenever logging silently fails ("at least one API key must be
set" even though you configured one) -- it hits each real endpoint
directly and prints exactly what's wrong instead of making you guess.

    python -m scripts.check_setup
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from core import config

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
SKIP = "\033[33mSKIP\033[0m"


def check_telegram():
    if not config.TELEGRAM_BOT_TOKEN:
        print(f"[{SKIP}] Telegram      -- TELEGRAM_BOT_TOKEN not set")
        return
    r = requests.get(f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/getMe", timeout=10)
    if r.ok and r.json().get("ok"):
        me = r.json()["result"]
        print(f"[{PASS}] Telegram      -- bot @{me.get('username')}")
    else:
        print(f"[{FAIL}] Telegram      -- {r.status_code}: {r.text[:200]}")


def check_gemini():
    if not config.GEMINI_API_KEY:
        print(f"[{SKIP}] Gemini        -- GEMINI_API_KEY not set")
        return
    r = requests.get(
        "https://generativelanguage.googleapis.com/v1beta/models",
        params={"key": config.GEMINI_API_KEY}, timeout=10,
    )
    if not r.ok:
        print(f"[{FAIL}] Gemini        -- key check failed, {r.status_code}: {r.text[:200]}")
        return
    names = [m["name"].replace("models/", "") for m in r.json().get("models", [])
             if "generateContent" in m.get("supportedGenerationMethods", [])]
    configured = config.GEMINI_MODEL
    if configured in names:
        print(f"[{PASS}] Gemini        -- key valid, '{configured}' is available")
    else:
        print(f"[{FAIL}] Gemini        -- key valid, but GEMINI_MODEL='{configured}' is NOT in the "
              f"available list. Try one of: {', '.join(names[:8])}")


def check_calorieninjas():
    if not config.CALORIENINJAS_API_KEY:
        print(f"[{SKIP}] CalorieNinjas -- CALORIENINJAS_API_KEY not set")
        return
    r = requests.get(
        "https://api.api-ninjas.com/v1/nutrition", params={"query": "1 apple"},
        headers={"X-Api-Key": config.CALORIENINJAS_API_KEY}, timeout=10,
    )
    data = r.json() if r.ok else None
    if r.ok and isinstance(data, list) and data:
        print(f"[{PASS}] CalorieNinjas -- parsed 'apple' -> {data[0].get('calories')} kcal")
    else:
        print(f"[{FAIL}] CalorieNinjas -- {r.status_code}: {r.text[:200]}")


def check_usda():
    r = requests.get(
        "https://api.nal.usda.gov/fdc/v1/foods/search",
        params={"query": "apple", "pageSize": 1, "api_key": config.USDA_API_KEY}, timeout=10,
    )
    label = "USDA (DEMO_KEY)" if config.USDA_API_KEY == "DEMO_KEY" else "USDA"
    if r.ok and r.json().get("foods"):
        note = "" if config.USDA_API_KEY != "DEMO_KEY" else \
            "  (shared demo key, ~30 req/hour -- get your own free key at https://fdc.nal.usda.gov/api-key-signup)"
        print(f"[{PASS}] {label:13s} -- reachable{note}")
    else:
        print(f"[{FAIL}] {label:13s} -- {r.status_code}: {r.text[:200]}")


def check_openfoodfacts():
    from core.nutrition_apis import DEFAULT_HEADERS
    r = requests.get(
        "https://world.openfoodfacts.org/api/v2/product/3017620422003.json",
        headers=DEFAULT_HEADERS, timeout=10,
    )
    if r.ok and r.json().get("status") == 1:
        print(f"[{PASS}] Open Food Facts -- reachable (no key needed)")
    else:
        print(f"[{FAIL}] Open Food Facts -- {r.status_code}: {r.text[:200]}")


if __name__ == "__main__":
    print(f"Checking APIs using config from {config.ROOT_DIR / '.env'}\n")
    check_telegram()
    check_gemini()
    check_calorieninjas()
    check_usda()
    check_openfoodfacts()
    print("\nSKIP just means that key isn't set -- the app degrades gracefully without it.")
    print("A FAIL for a key you did set means: check the printed status/message above first.")
