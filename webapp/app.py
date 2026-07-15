"""
Flask front-end for compare_assessments.py.

Handles PDF upload in-memory only -- uploaded files are never written to disk
and are discarded once the request completes, to minimize exposure of tenant PII.

Auth: single shared password gate (HTTP session cookie) via BASIC_AUTH_PASSWORD
env var. This is a minimum bar for a small internal tool, not a full user/roles
system -- see README "Security notes" before putting a real public URL on this.
"""
import io
import os
import secrets
import sys
from functools import wraps
from pathlib import Path

from flask import Flask, render_template, request, session, redirect, url_for, flash

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import compare_assessments as ca  # noqa: E402

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(32))

APP_PASSWORD = os.environ.get("APP_PASSWORD")  # set this before deploying anywhere reachable


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if APP_PASSWORD and not session.get("authed"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


@app.route("/login", methods=["GET", "POST"])
def login():
    if not APP_PASSWORD:
        return redirect(url_for("index"))
    if request.method == "POST":
        if secrets.compare_digest(request.form.get("password", ""), APP_PASSWORD):
            session["authed"] = True
            return redirect(request.args.get("next") or url_for("index"))
        flash("Incorrect password.")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/", methods=["GET"])
@login_required
def index():
    return render_template("index.html")


@app.route("/compare", methods=["POST"])
@login_required
def compare():
    move_in_file = request.files.get("move_in_pdf")
    move_out_file = request.files.get("move_out_pdf")
    property_label = request.form.get("property") or "Unnamed Property"

    if not move_in_file or not move_out_file:
        flash("Please upload both a move-in and a move-out assessment PDF.")
        return redirect(url_for("index"))

    kb = ca.load_knowledge_base()

    move_in_items, move_in_warn = _parse_uploaded(move_in_file)
    move_out_items, move_out_warn = _parse_uploaded(move_out_file)

    warnings = [w for w in (move_in_warn, move_out_warn) if w]
    results = ca.diff_assessments(move_in_items, move_out_items, kb)

    return render_template(
        "report.html",
        property_label=property_label,
        results=results,
        warnings=warnings,
    )


def _parse_uploaded(file_storage):
    """Parse an uploaded PDF entirely in memory; never touches disk."""
    buf = io.BytesIO(file_storage.read())
    text, tables = ca.extract_text_and_tables(buf)
    items = ca.parse_from_tables(tables) if tables else []
    if not items:
        items = ca.parse_from_text(text)
    warning = None
    if not items:
        warning = f'Could not extract any line items from "{file_storage.filename}" -- report below may be empty or incomplete.'
    return items, warning


if __name__ == "__main__":
    app.run(debug=True, port=5000)
