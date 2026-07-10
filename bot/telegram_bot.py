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

# In-memory only, keyed by the pending_confirms token (which IS persisted
# in the DB -- see db.create_pending_confirm). This just tracks which
# message is showing that draft right now (for editing in place) and
# whatever the last Double-check result was (so a follow-up refine can
# pass it along as context). Losing this on a bot restart just means
# Double-check/reply-to-correct stop working for that one in-flight
# draft -- confirming it still works fine straight from the DB.
_draft_meta = {}          # token -> {"chat_id", "message_id", "sanity"}
_message_token = {}       # (chat_id, message_id) -> token, for reply-based refine

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
    kb.add(types.InlineKeyboardButton("\U0001F50D Double-check", callback_data=f"sanity:{token}"),
           types.InlineKeyboardButton("❌ Discard", callback_data=f"discard:{token}"))
    return kb


def safe_edit(text, chat_id, message_id, reply_markup=None):
    """Telegram errors on an edit whose text+markup are byte-identical to
    what's already there ("message is not modified") -- harmless, just
    means this stage produced the same content as the last one."""
    try:
        bot.edit_message_text(text, chat_id, message_id, reply_markup=reply_markup)
    except Exception as e:
        if "message is not modified" not in str(e).lower():
            raise


def draft_message_text(draft, prefix="", sanity=None):
    text = (prefix + "\n" if prefix else "")
    if draft.get("meal_label"):
        text += f"*{draft['meal_label']}*\n\n"
    text += fmt_items(draft["items"])
    if draft.get("notes"):
        text += f"\n\n_Note: {draft['notes']}_"
    if sanity:
        icon = "✅" if sanity.get("ok") else "⚠️"
        text += f"\n\n{icon} _{sanity.get('note', '')}_"
        for f in sanity.get("flags") or []:
            text += f"\n   • *{f.get('item_name')}* ({f.get('field')}): {f.get('concern')}"
    text += "\n\nLog this as, or reply to this message to fix something:"
    return text


def _register_draft(token, chat_id, message_id, sanity=None):
    _draft_meta[token] = {"chat_id": chat_id, "message_id": message_id, "sanity": sanity}
    _message_token[(chat_id, message_id)] = token


def _forget_draft(token):
    meta = _draft_meta.pop(token, None)
    if meta:
        _message_token.pop((meta["chat_id"], meta["message_id"]), None)


def offer_confirm(chat_id, draft, prefix=""):
    if draft.get("error") or not draft.get("items"):
        bot.send_message(chat_id, draft.get("error") or "Couldn't identify any food in that.")
        return
    token = db.create_pending_confirm(chat_id, draft, meal_slot=guess_meal_slot())
    msg = bot.send_message(chat_id, draft_message_text(draft, prefix=prefix), reply_markup=meal_keyboard(token))
    _register_draft(token, chat_id, msg.message_id)


def staged_build_and_offer(chat_id, extract_fn, prefix=""):
    """Sends one message and edits it in place through each real pipeline
    stage -- ingredients appear as soon as extraction finishes, then
    macros once the fill call finishes -- rather than one long silence
    followed by the finished result."""
    msg = bot.send_message(chat_id, (prefix + "\n" if prefix else "") + "\U0001F50E Identifying ingredients…")
    extraction = extract_fn()
    if extraction.get("error") or not extraction.get("items"):
        safe_edit(extraction.get("error") or "Couldn't identify any food in that.", chat_id, msg.message_id)
        return

    lines = (prefix + "\n" if prefix else "")
    if extraction.get("meal_label"):
        lines += f"*{extraction['meal_label']}*\n\n"
    lines += "\n".join(f"• {it['name']} ({it.get('grams', 0):.0f}g)" for it in extraction["items"])
    lines += "\n\n\U0001F9EE Calculating macros…"
    safe_edit(lines, chat_id, msg.message_id)

    draft = logging_service.resolve_draft(extraction)
    if draft.get("error") or not draft.get("items"):
        safe_edit(draft.get("error") or "Couldn't resolve nutrition for that.", chat_id, msg.message_id)
        return

    token = db.create_pending_confirm(chat_id, draft, meal_slot=guess_meal_slot())
    safe_edit(draft_message_text(draft, prefix=prefix), chat_id, msg.message_id, reply_markup=meal_keyboard(token))
    _register_draft(token, chat_id, msg.message_id)


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
        "Before confirming, you can tap 🔍 *Double-check* to have it review the "
        "ingredients/weights/macros for anything that looks off, or just *reply* "
        "to the draft message with a correction (\"actually the chicken was "
        "150g\") -- either costs one extra request, so neither happens "
        "automatically.\n\n"
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
    previous_context = logging_service.previous_week_context(db.today_str())
    report = gemini_report_or_fallback(context, previous_context)
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


def gemini_report_or_fallback(context, previous_context=None):
    from core import gemini
    report = gemini.generate_weekly_report(context, previous_context=previous_context)
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
    if message.reply_to_message is not None:
        handle_refine_reply(message)
        return
    bot.send_chat_action(message.chat.id, "typing")
    staged_build_and_offer(message.chat.id, lambda: logging_service.extract_only(text=message.text))


@bot.message_handler(content_types=["photo"])
def handle_photo(message):
    bot.send_chat_action(message.chat.id, "typing")
    file_info = bot.get_file(message.photo[-1].file_id)
    image_bytes = bot.download_file(file_info.file_path)
    caption = message.caption or ""
    staged_build_and_offer(message.chat.id,
        lambda: logging_service.extract_only(image_bytes=image_bytes, mime_type="image/jpeg", caption=caption))


@bot.message_handler(content_types=["voice"])
def handle_voice(message):
    from core import gemini
    bot.send_chat_action(message.chat.id, "typing")
    file_info = bot.get_file(message.voice.file_id)
    audio_bytes = bot.download_file(file_info.file_path)
    transcript = gemini.transcribe_voice(audio_bytes, "audio/ogg")
    if not transcript:
        bot.send_message(message.chat.id, "Couldn't make that out -- try again or send text instead.")
        return
    staged_build_and_offer(message.chat.id, lambda: logging_service.extract_only(text=transcript),
                            prefix=f"_Heard:_ “{transcript}”\n")


def handle_refine_reply(message):
    """A text reply to one of our own pending-draft messages is treated as
    a correction/concern, not a new meal -- one more Gemini call, only
    fired because the user actually asked for a change. Sends a NEW
    message with the result (rather than editing the old one) so it
    visibly follows the user's correction in the chat, and clears the old
    message's keyboard so there's only ever one "confirm" button in play."""
    chat_id = message.chat.id
    token = _message_token.get((chat_id, message.reply_to_message.message_id))
    if not token:
        return  # a reply to something else -- not ours to handle
    pending = db.peek_pending_confirm(token)
    if not pending:
        bot.send_message(chat_id, "That confirmation expired -- log it again.")
        _forget_draft(token)
        return
    draft = pending["items"]
    meta = _draft_meta.get(token, {})
    bot.send_chat_action(chat_id, "typing")
    updated_draft, changed, note = logging_service.refine_meal(draft, message.text, sanity=meta.get("sanity"))
    if note is None:
        # the call itself failed (quota/network) -- distinct from "reviewed
        # it and nothing needed changing", which is a normal, useful answer.
        bot.send_message(chat_id, "Couldn't process that -- check GEMINI_API_KEY / quota, or try rephrasing.")
        return
    if not changed:
        # a question/concern that Gemini looked at and found nothing wrong
        # with -- the original draft (and its keyboard) is still accurate,
        # so there's nothing to re-send other than the explanation.
        bot.send_message(chat_id, f"_{note}_")
        return
    db.update_pending_confirm(token, updated_draft)
    try:
        bot.edit_message_reply_markup(chat_id, meta.get("message_id"), reply_markup=None)
    except Exception:
        pass
    text = f"✎ {note}\n\n" + draft_message_text(updated_draft, sanity=meta.get("sanity"))
    new_msg = bot.send_message(chat_id, text, reply_markup=meal_keyboard(token))
    _register_draft(token, chat_id, new_msg.message_id, sanity=meta.get("sanity"))


# ---------------- callbacks ----------------

@bot.callback_query_handler(func=lambda c: c.data.startswith("log:"))
def cb_log(call):
    _, token, slot = call.data.split(":", 2)
    pending = db.pop_pending_confirm(token)
    _forget_draft(token)
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
    _forget_draft(token)
    bot.answer_callback_query(call.id, "Discarded.")
    bot.edit_message_text("Discarded.", call.message.chat.id, call.message.message_id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("sanity:"))
def cb_sanity(call):
    token = call.data.split(":", 1)[1]
    pending = db.peek_pending_confirm(token)
    if not pending:
        bot.answer_callback_query(call.id, "This confirmation expired, please log it again.")
        return
    bot.answer_callback_query(call.id, "Double-checking…")
    draft = pending["items"]
    sanity = logging_service.sanity_check(draft)
    meta = _draft_meta.setdefault(token, {"chat_id": call.message.chat.id, "message_id": call.message.message_id})
    meta["sanity"] = sanity
    if sanity is None:
        bot.send_message(call.message.chat.id, "Sanity check failed -- check GEMINI_API_KEY / quota with "
                                                 "`python -m scripts.check_setup`.")
        return
    safe_edit(draft_message_text(draft, sanity=sanity), call.message.chat.id, call.message.message_id,
              reply_markup=meal_keyboard(token))


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
    msg = bot.send_message(call.message.chat.id, draft_message_text(draft), reply_markup=meal_keyboard(token))
    _register_draft(token, call.message.chat.id, msg.message_id)


if __name__ == "__main__":
    db.init_db()
    print("Nutrition Ledger bot running (long polling)...")
    bot.infinity_polling()
