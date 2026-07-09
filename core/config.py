import os
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")

CALORIENINJAS_API_KEY = os.environ.get("CALORIENINJAS_API_KEY", "")
EDAMAM_APP_ID = os.environ.get("EDAMAM_APP_ID", "")
EDAMAM_APP_KEY = os.environ.get("EDAMAM_APP_KEY", "")

DB_PATH = Path(os.environ.get("DB_PATH", ROOT_DIR / "data" / "nutrition.db"))
API_PORT = int(os.environ.get("API_PORT", "8001"))

# Local timezone offset used to bucket "today" for the bot / reports.
# Set to your IANA zone if you add zoneinfo handling later; for now a fixed
# UTC offset in hours keeps this dependency-free for Termux.
LOCAL_UTC_OFFSET_HOURS = float(os.environ.get("LOCAL_UTC_OFFSET_HOURS", "0"))
