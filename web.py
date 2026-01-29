"""
Jiva Health - Web Server
Serve HTML pages: consultations, patient profiles, reports, attachments.
Data from SQLite database.
"""

import os
from datetime import datetime
from pathlib import Path

from flask import Flask, render_template, jsonify, abort, send_file

import database as db

app = Flask(__name__)
REPORTS_DIR = Path(__file__).parent / "reports"


@app.route("/")
def index():
    """Main page - list all consultations from DB."""
    consultations = db.get_all_consultations(limit=50)
    return render_template("index.html", consultations=consultations)


@app.route("/report/<consultation_id>")
def view_report(consultation_id):
    """View a specific consultation report."""
    consultation = db.get_consultation(consultation_id)
    if not consultation:
        # Fallback to file-based report
        report_path = REPORTS_DIR / f"{consultation_id}.md"
        if report_path.exists():
            content = report_path.read_text(encoding="utf-8")
            return render_template(
                "report.html", report_id=consultation_id, content=content,
                consultation=None
            )
        abort(404)

    patient = None
    if consultation.get("patient_id"):
        patient = db.get_patient(consultation["patient_id"])

    attachments = []
    if consultation.get("id"):
        attachments = db.get_attachments_by_consultation(consultation["id"])

    content = consultation.get("report") or ""

    return render_template(
        "report.html",
        report_id=consultation_id,
        content=content,
        consultation=consultation,
        patient=patient,
        attachments=attachments,
    )


@app.route("/patients")
def patients_list():
    """List all patients."""
    conn = db.get_db()
    rows = conn.execute("""
        SELECT p.*, u.full_name as user_name, u.telegram_username,
               COUNT(c.id) as consultation_count
        FROM patients p
        LEFT JOIN users u ON p.user_id = u.id
        LEFT JOIN consultations c ON c.patient_id = p.id
        GROUP BY p.id
        ORDER BY p.updated_at DESC
    """).fetchall()
    conn.close()
    patients = [dict(r) for r in rows]
    return render_template("patients.html", patients=patients)


@app.route("/patient/<int:patient_id>")
def view_patient(patient_id):
    """View patient profile with consultation history."""
    patient = db.get_patient(patient_id)
    if not patient:
        abort(404)

    consultations = db.get_consultations_by_patient(patient_id, limit=20)
    attachments = db.get_attachments_by_patient(patient_id)

    return render_template(
        "patient.html",
        patient=patient,
        consultations=consultations,
        attachments=attachments,
    )


@app.route("/attachment/<int:attachment_id>")
def serve_attachment(attachment_id):
    """Serve an attachment file."""
    conn = db.get_db()
    row = conn.execute(
        "SELECT * FROM attachments WHERE id = ?", (attachment_id,)
    ).fetchone()
    conn.close()

    if not row:
        abort(404)

    att = dict(row)
    file_path = Path(att["stored_path"])
    if not file_path.exists():
        abort(404)

    return send_file(
        str(file_path),
        mimetype=att.get("mime_type") or "application/octet-stream",
        download_name=att.get("original_name"),
    )


# --- API endpoints ---

@app.route("/api/consultations")
def api_consultations():
    """API - list consultations as JSON."""
    consultations = db.get_all_consultations(limit=50)
    return jsonify(consultations)


@app.route("/api/consultation/<consultation_id>")
def api_consultation(consultation_id):
    """API - get consultation detail."""
    consultation = db.get_consultation(consultation_id)
    if not consultation:
        abort(404)
    return jsonify(consultation)


@app.route("/api/patients")
def api_patients():
    """API - list patients."""
    conn = db.get_db()
    rows = conn.execute("SELECT * FROM patients ORDER BY updated_at DESC").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/patient/<int:patient_id>")
def api_patient(patient_id):
    """API - patient detail with consultations."""
    patient = db.get_patient(patient_id)
    if not patient:
        abort(404)
    consultations = db.get_consultations_by_patient(patient_id)
    return jsonify({"patient": patient, "consultations": consultations})


if __name__ == "__main__":
    REPORTS_DIR.mkdir(exist_ok=True)
    app.run(host="0.0.0.0", port=int(os.getenv("WEB_PORT", "8080")), debug=True)
