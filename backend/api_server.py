"""Flask REST API for the web dashboard.

Run with: python -m backend.api_server
Reads/writes the same SQLite file as the Telegram bot -- both can run at
the same time, on the same machine, alongside anything else you already
have running on other ports (defaults to 8001 here; set API_PORT in .env
to change it).
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flask import Flask, jsonify, request, send_from_directory

from core import config, db, logging_service

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

app = Flask(__name__)


@app.route("/")
def dashboard():
    return send_from_directory(WEB_DIR, "index.html")


@app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,DELETE,OPTIONS"
    return resp


@app.route("/api/<path:_unused>", methods=["OPTIONS"])
def cors_preflight(_unused):
    return ("", 204)


# ---------------- day view ----------------

@app.route("/api/day/<date>")
def day_summary(date):
    summary = logging_service.build_day_summary(date)
    entries = db.get_day_entries(date)
    return jsonify({**summary, "entries": entries})


@app.route("/api/day/<date>/contribution/<nutrient_key>")
def day_contribution(date, nutrient_key):
    return jsonify(logging_service.nutrient_contribution(date, nutrient_key))


@app.route("/api/entries/<int:entry_id>", methods=["PUT"])
def edit_entry(entry_id):
    body = request.get_json(force=True)
    ok = logging_service.apply_correction(entry_id, body.get("nutrients", {}), body.get("name"))
    return jsonify({"ok": ok})


@app.route("/api/entries/<int:entry_id>", methods=["DELETE"])
def delete_entry(entry_id):
    db.delete_log_entry(entry_id)
    return jsonify({"ok": True})


# ---------------- trends ----------------

@app.route("/api/trends")
def trends():
    end = request.args.get("end") or db.today_str()
    days = int(request.args.get("days", 7))
    end_dt = datetime.strptime(end, "%Y-%m-%d")
    start_dt = end_dt - timedelta(days=days - 1)
    entries = db.get_range_entries(start_dt.strftime("%Y-%m-%d"), end)

    by_date = {}
    for e in entries:
        d = by_date.setdefault(e["log_date"], {})
        for k, v in e["nutrients"].items():
            d[k] = d.get(k, 0) + (v or 0)

    series = []
    for i in range(days):
        d = (start_dt + timedelta(days=i)).strftime("%Y-%m-%d")
        totals = by_date.get(d, {})
        series.append({"date": d, "totals": {k: round(v, 1) for k, v in totals.items()}})

    return jsonify({"start": start_dt.strftime("%Y-%m-%d"), "end": end, "series": series})


# ---------------- settings ----------------

@app.route("/api/settings/nutrients")
def get_nutrient_defs():
    return jsonify(db.list_nutrient_defs())


@app.route("/api/settings/nutrients", methods=["POST"])
def post_nutrient_def():
    body = request.get_json(force=True)
    try:
        key = db.create_nutrient_def(
            key=body["key"], label=body["label"], unit=body.get("unit", ""),
            category=body.get("category", "other"), direction=body.get("direction", "higher_better"),
            target_mode=body.get("target_mode", "flat"), target_value=body.get("target_value", 0),
        )
    except (KeyError, ValueError) as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        if "UNIQUE constraint" in str(e):
            return jsonify({"error": f"a nutrient with key '{body.get('key')}' already exists"}), 409
        raise
    return jsonify({"key": key})


@app.route("/api/settings/nutrients/<key>", methods=["PUT"])
def put_nutrient_def(key):
    body = request.get_json(force=True)
    db.update_nutrient_def(key, **body)
    return jsonify({"ok": True})


@app.route("/api/settings/profile", methods=["GET"])
def get_profile():
    with db.get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM profile").fetchall()
        return jsonify({r["key"]: r["value"] for r in rows})


@app.route("/api/settings/profile", methods=["PUT"])
def put_profile():
    body = request.get_json(force=True)
    for k, v in body.items():
        db.set_profile(k, v)
    return jsonify({"ok": True})


# ---------------- saved meals ----------------

@app.route("/api/saved-meals", methods=["GET"])
def get_saved_meals():
    return jsonify(db.list_saved_meals())


@app.route("/api/saved-meals", methods=["POST"])
def post_saved_meal():
    body = request.get_json(force=True)
    meal_id = db.save_meal(body["name"], body["items"])
    return jsonify({"id": meal_id})


# ---------------- quick add (text) ----------------

@app.route("/api/quickadd/parse", methods=["POST"])
def quickadd_parse():
    text = request.get_json(force=True).get("text", "")
    items = logging_service.parse_text_entry(text)
    return jsonify({"items": items})


@app.route("/api/quickadd/confirm", methods=["POST"])
def quickadd_confirm():
    body = request.get_json(force=True)
    ids = logging_service.confirm_and_log(
        body["items"], body.get("meal_slot", "snack"),
        raw_input=body.get("raw_input"), log_date=body.get("date"),
    )
    return jsonify({"entry_ids": ids})


# ---------------- reports & ask ----------------

@app.route("/api/report/weekly")
def report_weekly():
    from core import gemini
    end = request.args.get("end") or db.today_str()
    context = logging_service.build_week_context(end)
    report = gemini.generate_weekly_report(context) or {}
    return jsonify({"context": context, "report": report})


@app.route("/api/ask", methods=["POST"])
def ask():
    from core import gemini
    body = request.get_json(force=True)
    question = body.get("question", "")
    date = body.get("date") or db.today_str()
    context = logging_service.build_day_summary(date)
    answer = gemini.answer_question(question, context)
    return jsonify({"answer": answer})


if __name__ == "__main__":
    db.init_db()
    app.run(host="0.0.0.0", port=config.API_PORT, debug=True)
