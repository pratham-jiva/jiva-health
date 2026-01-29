"""
Jiva Health - Web Server
Serve HTML page để xem health consultation reports.
"""

import json
from datetime import datetime
from pathlib import Path

from flask import Flask, render_template, jsonify, abort

app = Flask(__name__)
REPORTS_DIR = Path(__file__).parent / "reports"


@app.route("/")
def index():
    """Main page - list all reports."""
    reports = []
    if REPORTS_DIR.exists():
        for f in sorted(REPORTS_DIR.glob("c_*.md"), reverse=True):
            stat = f.stat()
            reports.append({
                "id": f.stem,
                "filename": f.name,
                "date": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                "size": stat.st_size,
            })
    return render_template("index.html", reports=reports)


@app.route("/report/<report_id>")
def view_report(report_id):
    """View a specific consultation report."""
    report_path = REPORTS_DIR / f"{report_id}.md"
    if not report_path.exists():
        abort(404)

    content = report_path.read_text(encoding="utf-8")
    return render_template("report.html", report_id=report_id, content=content)


@app.route("/api/reports")
def api_reports():
    """API - list reports as JSON."""
    reports = []
    if REPORTS_DIR.exists():
        for f in sorted(REPORTS_DIR.glob("c_*.md"), reverse=True):
            stat = f.stat()
            reports.append({
                "id": f.stem,
                "date": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "size": stat.st_size,
            })
    return jsonify(reports)


@app.route("/api/report/<report_id>")
def api_report(report_id):
    """API - get report content."""
    report_path = REPORTS_DIR / f"{report_id}.md"
    if not report_path.exists():
        abort(404)
    return jsonify({
        "id": report_id,
        "content": report_path.read_text(encoding="utf-8"),
    })


if __name__ == "__main__":
    REPORTS_DIR.mkdir(exist_ok=True)
    app.run(host="0.0.0.0", port=8080, debug=True)
