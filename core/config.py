import os
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
# gemini-2.0-flash's free tier is exhausted (0 quota) as of mid-2026 --
# gemini-3.1-flash-lite has a real free-tier allowance (500 req/day per
# the account dashboard). Model names change; run
# `python -m scripts.check_setup` any time this stops working -- it lists
# every model your key actually has generateContent access to.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")

# CalorieNinjas' free tier now gates core nutrition fields (calories etc.)
# behind a premium subscription -- it still gets tried, but expect it to
# come back empty and fall through to USDA/Gemini most of the time.
CALORIENINJAS_API_KEY = os.environ.get("CALORIENINJAS_API_KEY", "")
# USDA FoodData Central: the public DEMO_KEY is shared globally and easily
# exhausted -- get your own free key instantly (no approval) at
# https://fdc.nal.usda.gov/api-key-signup for 1,000 req/hour.
USDA_API_KEY = os.environ.get("USDA_API_KEY") or "DEMO_KEY"

DB_PATH = Path(os.environ.get("DB_PATH") or (ROOT_DIR / "data" / "nutrition.db"))
API_PORT = int(os.environ.get("API_PORT") or "8001")

# Local timezone offset used to bucket "today" for the bot / reports.
# Set to your IANA zone if you add zoneinfo handling later; for now a fixed
# UTC offset in hours keeps this dependency-free for Termux.
LOCAL_UTC_OFFSET_HOURS = float(os.environ.get("LOCAL_UTC_OFFSET_HOURS") or "0")
