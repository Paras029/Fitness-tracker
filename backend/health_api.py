"""Flask routes for the Health & Body domain -- registered as a Blueprint
onto the main app in api_server.py so nutrition and health routes don't
pile up in one growing file. Same thin-route/delegate-to-service style and
error-handling idiom as api_server.py.
"""

import uuid
from pathlib import Path

from flask import Blueprint, jsonify, request

from core import config, db, health_db, health_service

health_bp = Blueprint("health", __name__, url_prefix="/api/health")


def _save_upload(file_storage):
    """Persists an uploaded file under LAB_UPLOADS_DIR with a collision-proof
    name, returns (file_path, mime_type). Uploads aren't stored in memory-only
    like meal photos are, since the user explicitly wants to keep/revisit the
    original report later."""
    config.LAB_UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    ext = Path(file_storage.filename or "").suffix or ""
    name = f"{uuid.uuid4().hex}{ext}"
    dest = config.LAB_UPLOADS_DIR / name
    file_storage.save(dest)
    return str(dest), file_storage.mimetype or "application/octet-stream"


# ---------------- body composition ----------------

@health_bp.route("/body-comp", methods=["GET"])
def get_body_comp():
    limit = request.args.get("limit")
    return jsonify(health_db.list_body_comp_entries(limit=limit))


@health_bp.route("/body-comp", methods=["POST"])
def post_body_comp():
    body = request.get_json(force=True)
    entry_id = health_service.log_body_comp(
        weight_kg=body.get("weight_kg"), body_fat_pct=body.get("body_fat_pct"),
        skeletal_muscle_kg=body.get("skeletal_muscle_kg"), visceral_fat=body.get("visceral_fat"),
        bmr=body.get("bmr"), body_water_pct=body.get("body_water_pct"),
        source=body.get("source", "manual"), note=body.get("note"), log_date=body.get("log_date"),
        segments=body.get("segments"),
    )
    return jsonify({"id": entry_id})


@health_bp.route("/body-comp/extract", methods=["POST"])
def body_comp_extract():
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "no file uploaded"}), 400
    file_bytes = file.read()
    mime_type = file.mimetype or "application/octet-stream"
    kwargs = {"pdf_bytes": file_bytes} if mime_type == "application/pdf" else {"image_bytes": file_bytes}
    draft = health_service.extract_body_comp_scan(mime_type=mime_type, **kwargs)
    return jsonify(draft)


@health_bp.route("/body-comp/<int:entry_id>/summary", methods=["POST"])
def body_comp_summary(entry_id):
    result = health_service.get_body_comp_summary(entry_id)
    if result is None:
        return jsonify({"error": "entry not found"}), 404
    return jsonify(result)


# ---------------- lab categories ----------------

@health_bp.route("/lab-categories", methods=["GET"])
def get_lab_categories():
    return jsonify(health_db.list_lab_categories())


@health_bp.route("/lab-categories", methods=["POST"])
def post_lab_category():
    body = request.get_json(force=True)
    try:
        key = health_db.create_lab_category(key=body["key"], label=body["label"])
    except (KeyError, ValueError) as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        if "UNIQUE constraint" in str(e):
            return jsonify({"error": f"a category with key '{body.get('key')}' already exists"}), 409
        raise
    return jsonify({"key": key})


@health_bp.route("/lab-categories/<key>", methods=["PUT"])
def put_lab_category(key):
    body = request.get_json(force=True)
    health_db.update_lab_category(key, **body)
    return jsonify({"ok": True})


# ---------------- labs: upload -> extract -> confirm ----------------

@health_bp.route("/labs/extract", methods=["POST"])
def labs_extract():
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "no file uploaded"}), 400
    file_bytes = file.read()
    mime_type = file.mimetype or "application/octet-stream"
    kwargs = {"pdf_bytes": file_bytes} if mime_type == "application/pdf" else {"image_bytes": file_bytes}
    draft = health_service.extract_lab_report(mime_type=mime_type, **kwargs)
    return jsonify(draft)


@health_bp.route("/labs/confirm", methods=["POST"])
def labs_confirm():
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "no file uploaded"}), 400
    tests = request.form.get("tests")
    report_date = request.form.get("report_date") or None
    label = request.form.get("label") or None
    if not tests:
        return jsonify({"error": "no tests provided"}), 400
    import json
    draft = {"tests": json.loads(tests), "report_date": report_date}
    file_path, mime_type = _save_upload(file)
    report_id = health_service.confirm_lab_report(draft, file_path=file_path, mime_type=mime_type, label=label)
    return jsonify({"report_id": report_id})


@health_bp.route("/labs", methods=["GET"])
def get_labs():
    test_name = request.args.get("test_name")
    category = request.args.get("category")
    return jsonify(health_db.list_lab_results(test_name=test_name, category_key=category))


@health_bp.route("/labs/reports", methods=["GET"])
def get_lab_reports():
    return jsonify(health_db.list_lab_reports())


# ---------------- other documents ----------------

@health_bp.route("/documents", methods=["GET"])
def get_documents():
    return jsonify(health_db.list_other_documents())


@health_bp.route("/documents", methods=["POST"])
def post_document():
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "no file uploaded"}), 400
    label = request.form.get("label") or file.filename or "Document"
    notes = request.form.get("notes")
    file_path, mime_type = _save_upload(file)
    doc_id = health_db.create_other_document(file_path=file_path, mime_type=mime_type, label=label, notes=notes)
    return jsonify({"id": doc_id})


# ---------------- water ----------------

@health_bp.route("/water", methods=["GET"])
def get_water():
    days = request.args.get("days")
    if days:
        return jsonify(health_db.water_trend(days=int(days)))
    log_date = request.args.get("date") or db.today_str()
    return jsonify({"date": log_date, "total_ml": health_db.day_water_total(log_date),
                     "logs": health_db.list_water_logs(log_date)})


@health_bp.route("/water", methods=["POST"])
def post_water():
    body = request.get_json(force=True)
    log_id = health_service.log_water(body["ml"], log_date=body.get("log_date"))
    log_date = body.get("log_date") or db.today_str()
    return jsonify({"id": log_id, "total_ml": health_db.day_water_total(log_date)})
