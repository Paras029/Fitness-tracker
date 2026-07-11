#!/usr/bin/env python3
"""Single-command launcher -- runs the dashboard and the Telegram bot
together in one process:

    python run.py

Each piece still runs standalone too, if you want them in separate
terminals/logs while debugging:

    python -m backend.api_server
    python -m bot.telegram_bot
"""

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import config, db, health_db


def run_dashboard():
    from backend.api_server import app
    # debug/reloader off: the reloader forks and only works on the main
    # thread, and this runs in a background thread alongside the bot.
    app.run(host="0.0.0.0", port=config.API_PORT, debug=False, use_reloader=False)


def main():
    db.init_db()
    health_db.init_health_db()

    dashboard_thread = threading.Thread(target=run_dashboard, daemon=True)
    dashboard_thread.start()
    print(f"Dashboard running at http://localhost:{config.API_PORT}")

    if not config.TELEGRAM_BOT_TOKEN:
        print("TELEGRAM_BOT_TOKEN not set in .env -- dashboard only, bot not started.")
        print("Press Ctrl+C to stop.")
        dashboard_thread.join()
        return

    from bot.telegram_bot import bot
    print("Telegram bot running (long polling)... Press Ctrl+C to stop both.")
    bot.infinity_polling()


if __name__ == "__main__":
    main()
