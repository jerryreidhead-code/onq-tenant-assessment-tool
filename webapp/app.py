"""
Flask front-end for compare_assessments.py.

Handles PDF upload in-memory only -- uploaded files are never written to disk
and are discarded once the request completes, to minimize exposure of tenant PII.

Auth: single shared password gate (HTTP session cookie) via BASIC_AUTH_PASSWORD
env var. This is a minimum bar for a small internal tool, not a full user/roles
system -- see README "Security notes" before putting a real public URL on this.
"""
import base64
import io
import os
import secrets
import sys
import uuid
from collections import OrderedDict
from functools import wraps
from pathlib import Path

from flask import Flask, Response, abort, render_template, request, session, redirect, url_for, flash

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import compare_assessments as ca  # noqa: E402

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(32))

APP_PASSWORD = os.environ.get("APP_PASSWORD")  # set this before deploying anywhere reachable

# In-memory store of per-report photo bytes, keyed by report_id, so the report page can
# reference small /photo/... URLs and let the browser lazy-load them instead of embedding
# every image inline. Bounded so a long-running dev server doesn't grow unbounded; photos
# are never written to disk, consistent with the in-memory-only handling of the PDFs.
PHOTO_CACHE = OrderedDict()
MAX_CACHED_REPORTS = 8


def _data_uri_to_bytes(data_uri):
    return base64.b64decode(data_uri.split(",", 1)[1])


def _store_photos_and_replace_with_urls(report_id, results):
    photo_store = []
    for row_index, r in enumerate(results):
        row_photos = {"move_in": [], "move_out": []}
        move_in_urls = []
        for i, uri in enumerate(r["move_in_images"]):
            row_photos["move_in"].append(_data_uri_to_bytes(uri))
            move_in_urls.append(url_for("serve_photo", report_id=report_id, row=row_index, side="in", idx=i))
        move_out_urls = []
        for i, uri in enumerate(r["move_out_images"]):
            row_photos["move_out"].append(_data_uri_to_bytes(uri))
            move_out_urls.append(url_for("serve_photo", report_id=report_id, row=row_index, side="out", idx=i))
        photo_store.append(row_photos)
        r["move_in_images"] = move_in_urls
        r["move_out_images"] = move_out_urls

    PHOTO_CACHE[report_id] = photo_store
    while len(PHOTO_CACHE) > MAX_CACHED_REPORTS:
        PHOTO_CACHE.popitem(last=False)


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
    for r in results:
        if "CHARGEABLE" in r["verdict"]:
            r["verdict_group"] = "chargeable"
        elif "NO CHARGE" in r["verdict"]:
            r["verdict_group"] = "wear"
        else:
            r["verdict_group"] = "review"
    summary = {
        "chargeable": sum(1 for r in results if r["verdict_group"] == "chargeable"),
        "no_charge": sum(1 for r in results if r["verdict_group"] == "wear"),
        "review": sum(1 for r in results if r["verdict_group"] == "review"),
    }

    report_id = uuid.uuid4().hex
    _store_photos_and_replace_with_urls(report_id, results)

    return render_template(
        "report.html",
        property_label=property_label,
        results=results,
        total_count=len(results),
        warnings=warnings,
        summary=summary,
    )


@app.route("/photo/<report_id>/<int:row>/<side>/<int:idx>")
@login_required
def serve_photo(report_id, row, side, idx):
    key = "move_in" if side == "in" else "move_out" if side == "out" else None
    entry = PHOTO_CACHE.get(report_id)
    if key is None or entry is None or row >= len(entry) or idx >= len(entry[row][key]):
        abort(404)
    return Response(entry[row][key][idx], mimetype="image/png")


def _parse_uploaded(file_storage):
    """Parse an uploaded PDF entirely in memory; never touches disk."""
    buf = io.BytesIO(file_storage.read())
    items = ca.parse_fastfield_pdf(buf)
    if not items:
        buf.seek(0)
        text, tables = ca.extract_text_and_tables(buf)
        items = ca.parse_from_tables(tables) if tables else []
        if not items:
            items = ca.parse_from_text(text)
    warning = None
    if not items:
        warning = f'Could not extract any line items from "{file_storage.filename}" -- report below may be empty or incomplete.'
    return items, warning


if __name__ == "__main__":
    # 5000 conflicts with macOS AirPlay Receiver -- use 8000 instead.
    app.run(debug=True, port=int(os.environ.get("PORT", 8000)))
