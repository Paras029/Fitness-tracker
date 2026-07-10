"""Telegram bot -- the primary day-to-day logging surface.

Run with: python -m bot.telegram_bot
Uses long-polling, so nothing needs to be publicly reachable. If this
process is offline when you send a message, Telegram queues it and
delivers it as soon as polling resumes -- no extra sync step needed.
"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import telebot
from telebot import types

from core import config, db, logging_service

if not config.TELEGRAM_BOT_TOKEN:
    raise SystemExit("TELEGRAM_BOT_TOKEN is not set -- add it to .env first.")

bot = telebot.TeleBot(config.TELEGRAM_BOT_TOKEN, parse_mode="Markdown")

# In-memory only: "what did this chat just confirm", used by /save.
# Fine for a single-user bot process; lost on restart, which is harmless.
_last_logged = {}

MEAL_EMOJI = {"breakfast": "\U0001F305", "lunch": "\U0001F372", "dinner": "\U0001F307", "snack": "\U0001F34E"}
CONFIDENCE_TAG = {"database": "✓", "cache": "✓", "llm_filled": "~", "llm_estimated": "~", "estimated": "~", "user_corrected": "✎"}
CORE_MACROS = ("kcal", "protein", "carbs", "fat")


def missing_macros(nutrients):
    # A field that's genuinely unknown is ABSENT from the dict -- never
    # present-as-zero -- so this only flags real gaps, not real zeros
    # (black coffee's kcal is legitimately ~0).
    return [k for k in CORE_MACROS if k not in nutrients]


def guess_meal_slot():
    offset = db.get_profile_float("timezone_offset_hours", config.LOCAL_UTC_OFFSET_HOURS)
    hour = (datetime.utcnow().hour + int(offset)) % 24
    if hour < 11:
        return "breakfast"
    if hour < 16:
        return "lunch"
    if hour < 21:
        return "dinner"
    return "snack"


def fmt_items(items):
    lines = []
    total_kcal = 0
    for it in items:
        n = it["nutrients"]
        kcal = n.get("kcal", 0) or 0
        total_kcal += kcal
        tag = CONFIDENCE_TAG.get(it.get("confidence"), "~")
        missing = missing_macros(n)
        warn = f" ⚠️ _missing {', '.join(missing)}_" if missing else ""
        lines.append(f"{tag} *{it['name']}* ({it.get('grams', '?'):.0f}g) -- {kcal:.0f} kcal "
                      f"(P {n.get('protein', 0):.0f}g / C {n.get('carbs', 0):.0f}g / F {n.get('fat', 0):.0f}g){warn}")
    lines.append(f"\n*Total: {total_kcal:.0f} kcal*")
    return "\n".join(lines)


def fmt_rating(meal):
    if meal.get("rating_score") is None:
        return ""
    stars = "⭐" * round(meal["rating_score"] / 2)
    return f"\n\n{stars} *{meal['rating_score']}/10 -- {meal['rating_label']}*\n_{meal['rating_note']}_"


def meal_keyboard(token):
    kb = types.InlineKeyboardMarkup(row_width=4)
    kb.add(*[
        types.InlineKeyboardButton(f"{MEAL_EMOJI[m]} {m.title()}", callback_data=f"log:{token}:{m}")
        for m in ("breakfast", "lunch", "dinner", "snack")
    ])
    kb.add(types.InlineKeyboardButton("❌ Discard", callback_data=f"discard:{token}"))
    return kb


def offer_confirm(chat_id, draft, prefix=""):
    if draft.get("error") or not draft.get("items"):
        bot.send_message(chat_id, draft.get("error") or "Couldn't identify any food in that.")
        return
    token = db.create_pending_confirm(chat_id, draft, meal_slot=guess_meal_slot())
    text = (prefix + "\n" if prefix else "")
    if draft.get("meal_label"):
        text += f"*{draft['meal_label']}*\n\n"
    text += fmt_items(draft["items"])
    if draft.get("notes"):
        text += f"\n\n_Note: {draft['notes']}_"
    text += "\n\nLog this as:"
    bot.send_message(chat_id, text, reply_markup=meal_keyboard(token))


# ---------------- commands ----------------

@bot.message_handler(commands=["start", "help"])
def cmd_start(message):
    bot.send_message(message.chat.id,
        "*Nutrition Ledger bot*\n\n"
        "Just tell me what you ate, in plain language:\n"
        "`2 rotis and dal, black coffee`\n\n"
        "Or send a photo of your plate (add a caption with weight if you "
        "know it, e.g. `250g`), a photo of a barcode/ingredient label, or a "
        "voice note describing the meal.\n\n"
        "Confidence tags: ✓ database-backed, ~ estimated, ✎ your correction.\n\n"
        "Commands:\n"
        "/today -- today's totals\n"
        "/water <ml> -- log water (default 250ml)\n"
        "/save <name> -- save your last logged meal for quick re-use\n"
        "/quick -- re-log a saved meal\n"
        "/report -- this week's summary + suggestions"
    )


@bot.message_handler(commands=["today"])
def cmd_today(message):
    summary = logging_service.build_day_summary(db.today_str())
    lines = [f"*Today ({summary['date']})* -- {summary['meal_count']} meals logged\n"]
    for n in summary["nutrients"]:
        bar = ""
        if n["pct"] is not None:
            bar = f" -- {n['pct']:.0f}%"
        lines.append(f"{n['label']}: {n['value']:.0f}/{n['target']:.0f}{n['unit']}{bar}")
    bot.send_message(message.chat.id, "\n".join(lines))


@bot.message_handler(commands=["water"])
def cmd_water(message):
    parts = message.text.split()
    try:
        ml = float(parts[1]) if len(parts) > 1 else 250.0
    except ValueError:
        ml = 250.0
    # grams fixed at 100 (multiplier 1) so nutrients_per_100g == the actual
    # value -- water isn't scaled by a "how much of it did you eat" weight.
    db.create_meal("water", [{
        "name": f"Water {ml:.0f}ml", "grams": 100,
        "nutrients_per_100g": {"water_ml": ml}, "source": "user", "confidence": "database",
    }])
    bot.send_message(message.chat.id, f"\U0001F4A7 Logged {ml:.0f}ml of water.")


@bot.message_handler(commands=["save"])
def cmd_save(message):
    name = message.text.partition(" ")[2].strip()
    meal = _last_logged.get(message.chat.id)
    if not name:
        bot.send_message(message.chat.id, "Usage: `/save My usual breakfast` (right after logging something).")
        return
    if not meal:
        bot.send_message(message.chat.id, "Nothing to save yet -- log a meal first, then /save it.")
        return
    items = [{"name": it["name"], "grams": it["grams"], "nutrients_per_100g": it["nutrients_per_100g"]}
             for it in meal["items"]]
    db.save_meal(name, items)
    bot.send_message(message.chat.id, f"Saved as *{name}*. Use /quick to re-log it.")


@bot.message_handler(commands=["quick"])
def cmd_quick(message):
    meals = db.list_saved_meals()
    if not meals:
        bot.send_message(message.chat.id, "No saved meals yet -- log something, then `/save <name>`.")
        return
    kb = types.InlineKeyboardMarkup(row_width=1)
    for m in meals[:15]:
        kb.add(types.InlineKeyboardButton(m["name"], callback_data=f"quick:{m['id']}"))
    bot.send_message(message.chat.id, "Pick a saved meal to log now:", reply_markup=kb)


@bot.message_handler(commands=["report"])
def cmd_report(message):
    bot.send_message(message.chat.id, "Crunching this week's numbers...")
    context = logging_service.build_week_context(db.today_str())
    report = gemini_report_or_fallback(context)
    lines = [f"*Weekly report ({context['start_date']} - {context['end_date']})*\n"]
    lines.append(report["summary"])
    if report.get("suggestions"):
        lines.append("\n*Suggestions:*")
        lines.extend(f"- {s}" for s in report["suggestions"])
    if report.get("watch"):
        lines.append(f"\n⚠ {report['watch']}")
    if context.get("avg_meal_rating") is not None:
        lines.append(f"\nAvg meal rating: {context['avg_meal_rating']}/10")
    bot.send_message(message.chat.id, "\n".join(lines))


def gemini_report_or_fallback(context):
    from core import gemini
    report = gemini.generate_weekly_report(context)
    if report:
        return report
    gaps = sorted(context["targets_vs_actual"], key=lambda g: g["pct_of_target"])
    worst = gaps[0] if gaps else None
    return {
        "summary": f"Averaged {context['averages'].get('kcal', 0):.0f} kcal/day this week.",
        "suggestions": [],
        "watch": f"{worst['label']} averaged {worst['pct_of_target']:.0f}% of target." if worst else "",
    }


# ---------------- natural language logging ----------------

@bot.message_handler(content_types=["text"])
def handle_text(message):
    if message.text.startswith("/"):
        return  # unknown command, ignore
    bot.send_chat_action(message.chat.id, "typing")
    draft = logging_service.build_meal_draft(text=message.text)
    offer_confirm(message.chat.id, draft)


@bot.message_handler(content_types=["photo"])
def handle_photo(message):
    bot.send_chat_action(message.chat.id, "typing")
    file_info = bot.get_file(message.photo[-1].file_id)
    image_bytes = bot.download_file(file_info.file_path)
    caption = message.caption or ""
    draft = logging_service.build_meal_draft(image_bytes=image_bytes, mime_type="image/jpeg", caption=caption)
    offer_confirm(message.chat.id, draft)


@bot.message_handler(content_types=["voice"])
def handle_voice(message):
    bot.send_chat_action(message.chat.id, "typing")
    file_info = bot.get_file(message.voice.file_id)
    audio_bytes = bot.download_file(file_info.file_path)
    transcript, draft = logging_service.build_meal_draft_from_voice(audio_bytes, "audio/ogg")
    if not transcript:
        bot.send_message(message.chat.id, draft.get("error", "Couldn't make that out -- try again or send text instead."))
        return
    offer_confirm(message.chat.id, draft, prefix=f"_Heard:_ “{transcript}”\n")


# ---------------- callbacks ----------------

@bot.callback_query_handler(func=lambda c: c.data.startswith("log:"))
def cb_log(call):
    _, token, slot = call.data.split(":", 2)
    pending = db.pop_pending_confirm(token)
    if not pending:
        bot.answer_callback_query(call.id, "This confirmation expired, please log it again.")
        return
    bot.answer_callback_query(call.id, "Logging...")
    draft = pending["items"]  # the whole draft dict was stored as "items" by create_pending_confirm
    meal = logging_service.confirm_meal(draft, slot)
    _last_logged[call.message.chat.id] = meal
    bot.edit_message_text(
        f"{MEAL_EMOJI.get(slot, '')} Logged as *{slot}*:\n\n{fmt_items(meal['items'])}{fmt_rating(meal)}",
        call.message.chat.id, call.message.message_id,
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("discard:"))
def cb_discard(call):
    token = call.data.split(":", 1)[1]
    db.pop_pending_confirm(token)
    bot.answer_callback_query(call.id, "Discarded.")
    bot.edit_message_text("Discarded.", call.message.chat.id, call.message.message_id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("quick:"))
def cb_quick(call):
    meal_id = int(call.data.split(":", 1)[1])
    saved = {m["id"]: m for m in db.list_saved_meals()}
    saved_meal = saved.get(meal_id)
    if not saved_meal:
        bot.answer_callback_query(call.id, "That saved meal is gone.")
        return
    draft = {"items": [{**it, "source": "saved", "confidence": "database",
                         "nutrients": db.item_absolute_nutrients(it)} for it in saved_meal["items"]],
             "meal_label": saved_meal["name"], "extraction_confidence": None, "raw_input": None}
    token = db.create_pending_confirm(call.message.chat.id, draft, meal_slot=guess_meal_slot())
    bot.answer_callback_query(call.id, "Pick a meal slot.")
    bot.send_message(call.message.chat.id, f"*{saved_meal['name']}*\n\n{fmt_items(draft['items'])}\n\nLog this as:",
                      reply_markup=meal_keyboard(token))


if __name__ == "__main__":
    db.init_db()
    print("Nutrition Ledger bot running (long polling)...")
    bot.infinity_polling()
