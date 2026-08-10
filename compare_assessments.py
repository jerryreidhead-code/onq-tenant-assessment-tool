#!/usr/bin/env python3
"""
Compare a move-in and move-out property condition assessment PDF and suggest
which condition changes are plausibly chargeable to the tenant, cross-referenced
against Arizona landlord-tenant statute and HUD useful-life/damage guidance.

NOT LEGAL ADVICE. See reference/az_hud_reference.md for citations and known gaps
(Arizona law does not define "normal wear and tear"; HUD figures are industry
guidance, not binding AZ law). A human must review every suggested charge before
it is communicated to a tenant.

The primary parser targets On Q's actual FastField-generated inspection PDFs: a
coordinate-based form (notes column / category column / pass-fail-na column) with
room section headers formatted "<Room> Pass N, Fail M". Generic table/line parsers
are kept as a fallback for other PDF formats.

Usage:
    python3 compare_assessments.py move_in.pdf move_out.pdf --property "123 Main St"
    python3 compare_assessments.py move_in.pdf move_out.pdf --dump-parsed   # debug parser
"""
import argparse
import base64
import io
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import pdfplumber

REFERENCE_DIR = Path(__file__).parent / "reference"
KB_PATH = REFERENCE_DIR / "knowledge_base.json"

ROOM_KEYWORDS = [
    "kitchen", "living room", "family room", "dining room", "bedroom", "bathroom",
    "hallway", "hall", "garage", "laundry", "patio", "balcony", "exterior", "yard",
    "entry", "foyer", "closet", "office", "den", "loft", "basement", "attic",
    "general", "common area",
]
ROOM_KEYWORDS_SET = set(ROOM_KEYWORDS)

LINE_ITEM_RE = re.compile(r"^(?P<item>[A-Za-z][A-Za-z0-9 /\-']{2,40}?)\s*[:\-]\s*(?P<rest>.+)$")

# Items that are the property manager's own equipment/access or pure informational
# facts (not part of the tenant's unit condition) -- always excluded from the
# comparison, checked against both the item name and its notes text.
EXCLUDED_TEXT_KEYWORDS = ["lockbox", "lock box"]
EXCLUDED_CATEGORIES = {"property service"}  # e.g. utility meter reading notes


def is_excluded(item_name, notes):
    haystack = f"{item_name} {notes}".lower()
    if item_name.strip().lower() in EXCLUDED_CATEGORIES:
        return True
    return any(kw in haystack for kw in EXCLUDED_TEXT_KEYWORDS)


def load_knowledge_base():
    with open(KB_PATH) as f:
        return json.load(f)


def extract_text_and_tables(pdf_path):
    """Returns (full_text, list_of_tables) using pdfplumber. Used by the generic fallback parsers."""
    text_parts = []
    tables = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text_parts.append(page.extract_text() or "")
            tables.extend(page.extract_tables())
    return "\n".join(text_parts), tables


@dataclass
class AssessmentItem:
    room: str
    item: str
    condition: str
    notes: str = ""
    images: list = None

    def __post_init__(self):
        if self.images is None:
            self.images = []


# ---------------------------------------------------------------------------
# FastField coordinate-based parser
#
# On Q's inspection PDFs render each field as three columns at fixed x-ranges:
#   notes/description (x0 < 350) | category label (350 <= x0 < 500) | pass/fail/na (x0 >= 500)
# Room section headers appear as their own row: "<Room name>" (left) + "Pass N, Fail M" (right).
# Notes text can wrap across additional lines that land either just before or just
# after the category/status line in reading order, so fields are reconstructed by
# nearest-neighbor assignment to the closest anchor (category+status pair), not by
# simple top-to-bottom line grouping.
# ---------------------------------------------------------------------------

_HEADER_RE = re.compile(r"^Pass\s+\d+,\s*Fail\s+\d+$")
_PAGE_GAP = 100000  # large fixed offset per page so pages never interleave when merged
_IMAGE_RESOLUTION = 100  # dpi for cropped item photos -- fast to render, plenty legible as a thumbnail


def _extract_global_words(pdf):
    words = []
    offset = 0
    for page in pdf.pages:
        for w in page.extract_words():
            w = dict(w)
            w["gtop"] = w["top"] + offset
            words.append(w)
        offset += _PAGE_GAP
    return words


def _extract_global_images(pdf):
    images = []
    offset = 0
    for page_index, page in enumerate(pdf.pages):
        for img in page.images:
            if img["x0"] >= 350:
                continue  # only the notes/photo column, not the category or status columns
            images.append({
                "page_index": page_index,
                "gtop": img["top"] + offset,
                "bbox": (img["x0"], img["top"], img["x1"], img["bottom"]),
            })
        offset += _PAGE_GAP
    return images


def _render_field_images(pdf, images_by_field):
    """images_by_field: {field_index: [image dict, ...]}. Renders each needed page once
    and crops photos from it, returning {field_index: [data-uri, ...]}."""
    needed_pages = {img["page_index"] for imgs in images_by_field.values() for img in imgs}
    page_renders = {}
    scale = _IMAGE_RESOLUTION / 72
    for page_index in needed_pages:
        page_renders[page_index] = pdf.pages[page_index].to_image(resolution=_IMAGE_RESOLUTION).original

    result = {}
    for field_index, imgs in images_by_field.items():
        data_uris = []
        for img in imgs:
            page_img = page_renders[img["page_index"]]
            x0, top, x1, bottom = img["bbox"]
            box = (int(x0 * scale), int(top * scale), int(x1 * scale), int(bottom * scale))
            if box[2] <= box[0] or box[3] <= box[1]:
                continue
            crop = page_img.crop(box)
            buf = io.BytesIO()
            crop.save(buf, format="PNG")
            data_uris.append("data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii"))
        result[field_index] = data_uris
    return result


def parse_fastfield_pdf(pdf_path):
    with pdfplumber.open(pdf_path) as pdf:
        words = _extract_global_words(pdf)
        all_images = _extract_global_images(pdf)

        left = [w for w in words if w["x0"] < 350]
        mid = [w for w in words if 350 <= w["x0"] < 500]
        right = [w for w in words if w["x0"] >= 500]

        right_by_top = {}
        for w in right:
            right_by_top.setdefault(round(w["gtop"], 1), []).append(w)

        header_tops = set()
        headers = {}
        for top, ws in right_by_top.items():
            joined = " ".join(w["text"] for w in sorted(ws, key=lambda w: w["x0"]))
            if _HEADER_RE.match(joined):
                header_tops.add(top)
                name_words = [w["text"] for w in left if round(w["gtop"], 1) == top]
                headers[top] = " ".join(name_words) if name_words else "(unnamed section)"

        anchors = []
        for top, ws in right_by_top.items():
            if top in header_tops:
                continue
            joined = " ".join(w["text"] for w in sorted(ws, key=lambda w: w["x0"])).strip()
            if joined.lower() in ("pass", "fail", "na"):
                cat_words = sorted([w for w in mid if round(w["gtop"], 1) == top], key=lambda w: w["x0"])
                category = " ".join(w["text"] for w in cat_words) if cat_words else "(unlabeled item)"
                anchors.append({"top": top, "category": category, "status": joined.lower()})
        anchors.sort(key=lambda a: a["top"])

        if not anchors:
            return []  # not a FastField-layout PDF -- let the caller fall back

        items = []
        images_by_field = {}
        for idx, anchor in enumerate(anchors):
            top = anchor["top"]
            prev_top = anchors[idx - 1]["top"] if idx > 0 else -1e12
            next_top = anchors[idx + 1]["top"] if idx < len(anchors) - 1 else 1e18
            window_start = (prev_top + top) / 2
            window_end = (top + next_top) / 2
            notes_words = sorted(
                (w for w in left if window_start < w["gtop"] <= window_end),
                key=lambda w: (w["gtop"], w["x0"]),
            )
            notes = " ".join(w["text"] for w in notes_words).strip()
            items.append({"top": top, "category": anchor["category"], "status": anchor["status"], "notes": notes})
            images_by_field[idx] = [
                img for img in all_images if window_start < img["gtop"] <= window_end
            ]

        field_images = _render_field_images(pdf, images_by_field)

        header_list = sorted(headers.items())
        result = []
        for idx, it in enumerate(items):
            room = "General"
            for htop, hname in header_list:
                if htop < it["top"]:
                    room = hname
                else:
                    break
            result.append(AssessmentItem(
                room=room, item=it["category"], condition=it["status"], notes=it["notes"],
                images=field_images.get(idx, []),
            ))
        return result


# ---------------------------------------------------------------------------
# Generic fallback parsers (used only if the FastField parser finds nothing)
# ---------------------------------------------------------------------------

def parse_from_tables(tables):
    """
    Best-effort parse of table rows into AssessmentItem records. Expects each
    table row to look roughly like [Room/Area, Item, Condition, Notes] or
    [Item, Condition, Notes]. Header row is used to detect column meaning;
    falls back to positional guessing (item, condition, notes) otherwise.
    """
    items = []
    current_room = "General"
    for table in tables:
        if not table:
            continue
        header = [((c or "").strip().lower()) for c in table[0]]
        col_map = {}
        for idx, col in enumerate(header):
            if any(k in col for k in ("room", "area", "location")):
                col_map["room"] = idx
            elif any(k in col for k in ("item", "component", "feature")):
                col_map["item"] = idx
            elif any(k in col for k in ("condition", "rating", "status")):
                col_map["condition"] = idx
            elif "note" in col or "comment" in col:
                col_map["notes"] = idx

        rows = table[1:] if col_map else table
        for row in rows:
            row = [(c or "").strip() for c in row]
            if not any(row):
                continue
            if "room" in col_map:
                current_room = row[col_map["room"]] or current_room
            item_val = row[col_map["item"]] if "item" in col_map else (row[0] if row else "")
            cond_val = row[col_map["condition"]] if "condition" in col_map else (row[1] if len(row) > 1 else "")
            notes_val = row[col_map["notes"]] if "notes" in col_map else (row[2] if len(row) > 2 else "")
            if not item_val:
                continue
            if not cond_val and not notes_val and item_val.strip().lower() in ROOM_KEYWORDS_SET:
                current_room = item_val.strip()
                continue
            items.append(AssessmentItem(room=current_room, item=item_val, condition=cond_val, notes=notes_val))
    return items


def parse_from_text(text):
    """
    Fallback line-based parser for non-tabular, non-FastField PDFs. Looks for room
    headers (short line matching a known room keyword) and "Item: condition - notes"
    lines beneath them.
    """
    items = []
    current_room = "General"
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lower = line.lower().strip(":").strip()
        if lower in ROOM_KEYWORDS_SET:
            current_room = line.strip(":").title()
            continue
        m = LINE_ITEM_RE.match(line)
        if m:
            item_name = m.group("item").strip()
            rest = m.group("rest").strip()
            parts = re.split(r"\s+-\s+", rest, maxsplit=1)
            condition = parts[0].strip()
            notes = parts[1].strip() if len(parts) > 1 else ""
            items.append(AssessmentItem(room=current_room, item=item_name, condition=condition, notes=notes))
    return items


def parse_assessment(pdf_path):
    items = parse_fastfield_pdf(pdf_path)
    if items:
        return items
    text, tables = extract_text_and_tables(pdf_path)
    items = parse_from_tables(tables) if tables else []
    if not items:
        items = parse_from_text(text)
    return items


# ---------------------------------------------------------------------------
# Aggregation -- FastField repeats a category multiple times per room (e.g. several
# "Plumbing" checks per bathroom). Group by (room, category) and reduce each group
# to one worst-case status ("fail" beats "pass" beats "na") with combined notes,
# since the comparison is inherently per-room-per-category, not per fixture.
# ---------------------------------------------------------------------------

_STATUS_RANK = {"fail": 2, "pass": 1, "na": 0}
_COUNT_RE = re.compile(r"\b(\d+)\s+(?:Issues?|Working|Not Working|Wallplates|Light Fixture|Window Covering)\b", re.I)


def normalize_key(room, item):
    return f"{room.strip().lower()}::{re.sub(r'[^a-z0-9]+', ' ', item.strip().lower()).strip()}"


def aggregate_items(items):
    groups = {}
    for it in items:
        key = normalize_key(it.room, it.item)
        g = groups.setdefault(key, {"room": it.room, "item": it.item, "status": "na", "notes": [], "max_count": None, "images": []})
        if _STATUS_RANK.get(it.condition, 0) > _STATUS_RANK.get(g["status"], 0):
            g["status"] = it.condition
        note = it.notes.strip()
        if note and note not in g["notes"]:
            g["notes"].append(note)
        for m in _COUNT_RE.finditer(note):
            n = int(m.group(1))
            if g["max_count"] is None or n > g["max_count"]:
                g["max_count"] = n
        for uri in it.images:
            if uri not in g["images"]:
                g["images"].append(uri)
    for g in groups.values():
        g["notes"] = "; ".join(g["notes"])
    return groups


# ---------------------------------------------------------------------------
# Category mapping -- FastField's own category vocabulary is now known directly
# from real exports, so map it straight to the knowledge base instead of guessing
# via fuzzy keyword matching against freeform text.
# ---------------------------------------------------------------------------

FASTFIELD_CATEGORY_MAP = {
    "doors": "doors",
    "garage door": "doors",
    "walls": "paint_walls",
    "paint": "paint_walls",
    "floor": "carpet_flooring",
    "cabinets": "cabinets",
    "closet": "cabinets",
    "counters": "cabinets",
    "plumbing": "plumbing_fixtures",
    "windows": "windows",
    "light fixtures": "light_fixtures",
    "smoke detector": "smoke_detector",
    "filter": "hvac_filters",
    "electrical": "electrical",
    "door locks": "keys_locks",
    "fences": "exterior_structure",
    "gates": "exterior_structure",
    "landscape": "exterior_structure",
    "roof": "exterior_structure",
    "thermostat": "appliances",
    "unit": "appliances",
}

# Category names that mean different things depending on which room/section they're
# under -- resolved by room name instead of a flat lookup.
ROOM_DEPENDENT_CATEGORIES = {
    "entry": {"access": "keys_locks", "_default": "cleaning"},
    "whole property": {"_default": "keys_locks"},
}

APPLIANCE_ROOMS = {
    "dishwasher", "dryer", "microwave", "oven", "refrigerator", "washer",
    "water heater", "garbage disposal", "other appliance 1", "other appliance 2",
}


WINDOW_COVERING_WORDS = ["window covering", "blind", "slat", "shade", "curtain"]


def resolve_category(room, item, notes, kb_by_key):
    item_lower = item.strip().lower()
    room_lower = room.strip().lower()
    if item_lower == "overview" and room_lower in APPLIANCE_ROOMS:
        return kb_by_key.get("appliances")
    if item_lower == "windows" and any(w in notes.lower() for w in WINDOW_COVERING_WORDS):
        return kb_by_key.get("blinds_window_coverings")
    if item_lower in ROOM_DEPENDENT_CATEGORIES:
        mapping = ROOM_DEPENDENT_CATEGORIES[item_lower]
        key = mapping.get(room_lower, mapping["_default"])
        return kb_by_key.get(key)
    key = FASTFIELD_CATEGORY_MAP.get(item_lower)
    return kb_by_key.get(key) if key else None


DAMAGE_WORDS = [
    "damage", "damaged", "broken", "hole", "holes", "crack", "cracked", "stain", "stains",
    "scratch", "scratches", "missing", "torn", "chipped", "gouged", "water damage",
    "not operating", "not working", "hanging", "does not function", "does not work",
]
CLEANING_ONLY_WORDS = ["dirty", "clean", "dust", "grille is dirty"]


def classify_change(room, item, move_in, move_out, kb, kb_by_key):
    in_status, out_status = move_in["status"], move_out["status"]
    notes = move_out["notes"] or move_in["notes"]
    notes_lower = notes.lower()
    category = resolve_category(room, item, notes, kb_by_key)

    in_count, out_count = move_in.get("max_count"), move_out.get("max_count")
    worsened = in_count is not None and out_count is not None and out_count > in_count

    has_damage_word = any(w in notes_lower for w in DAMAGE_WORDS)
    cleaning_only = any(w in notes_lower for w in CLEANING_ONLY_WORDS) and not has_damage_word

    if in_status != "fail" and out_status == "fail":
        if cleaning_only:
            verdict = "NEEDS HUMAN REVIEW (possible cleaning charge -- only excess-cleaning beyond normal turnover is chargeable)"
        elif has_damage_word or category is None:
            verdict = "LIKELY TENANT-CHARGEABLE"
        else:
            verdict = "NEEDS HUMAN REVIEW"
    elif in_status == "fail" and out_status != "fail":
        verdict = "NO CHARGE — resolved by move-out"
    elif in_status == "fail" and out_status == "fail":
        # An item that already failed at move-in is the landlord's pre-existing condition to
        # absorb, not the tenant's -- charging for it is not standard practice even if it looks
        # worse at move-out. A worsened count is flagged for a human to look at (the incremental
        # change might warrant a partial charge) but is never auto-marked chargeable.
        verdict = "NEEDS HUMAN REVIEW (pre-existing at move-in, but appears to have worsened)" if worsened else "NO CHARGE — pre-existing at move-in"
    else:
        verdict = "NEEDS HUMAN REVIEW"

    proration_note = None
    if category and category["key"] == "paint_walls" and "CHARGEABLE" in verdict:
        proration_note = (
            "Paint charges are prorated, not full-cost -- judgment call based on (1) the extent "
            "of damage shown in the photos (e.g. one wall vs. whole room) and (2) how much of "
            "that is beyond normal wear and tear vs. ordinary fading/nail holes. No fixed "
            "percentage table; a reviewer sets the % from the photos above. HUD's age-based "
            "useful life (flat paint 3-5 yrs, enamel 5-7 yrs -- see HUD Citation) is a separate "
            "ceiling on top of that: if the paint was already past its useful life, the charge "
            "should be $0 regardless of damage extent."
        )

    return {
        "category": category["label"] if category else "Uncategorized",
        "verdict": verdict,
        "az_citations": category["az_citations"] if category else ["A.R.S. § 33-1341(6)"],
        "hud_citation": category.get("hud_citation") if category else None,
        "useful_life_years": category.get("useful_life_years") if category else None,
        "proration_note": proration_note,
    }


def compute_proration(useful_life_years, age_years, repair_cost):
    """Returns (chargeable_amount, explanation), or (None, reason) if data is insufficient."""
    if not useful_life_years or age_years is None or repair_cost is None:
        return None, "insufficient data for proration (need item age, useful life, and repair cost)"
    total_life = list(useful_life_years.values())[0] if isinstance(useful_life_years, dict) else useful_life_years
    remaining = max(total_life - age_years, 0)
    if remaining <= 0:
        return 0.0, f"item is past its {total_life}-year useful life ({age_years:.1f} yrs old) -- no charge for ordinary deterioration"
    return round(repair_cost * (remaining / total_life), 2), f"{remaining:.1f} of {total_life} useful-life years remaining"


def diff_assessments(move_in_items, move_out_items, kb):
    kb_by_key = {cat["key"]: cat for cat in kb["categories"]}
    move_in_groups = aggregate_items(move_in_items)
    move_out_groups = aggregate_items(move_out_items)

    results = []
    for key, out_g in move_out_groups.items():
        if is_excluded(out_g["item"], out_g["notes"]):
            continue
        in_g = move_in_groups.get(key, {"status": "na", "notes": "(not present at move-in)", "max_count": None, "images": []})

        no_status_change = in_g["status"] == out_g["status"]
        no_notes_change = in_g["notes"].strip().lower() == out_g["notes"].strip().lower()
        if no_status_change and no_notes_change:
            continue  # nothing changed -- don't clutter the report

        classification = classify_change(out_g["room"], out_g["item"], in_g, out_g, kb, kb_by_key)
        results.append({
            "room": out_g["room"],
            "item": out_g["item"],
            "move_in_condition": in_g["status"],
            "move_in_notes": in_g["notes"],
            "move_in_images": in_g["images"],
            "move_out_condition": out_g["status"],
            "move_out_notes": out_g["notes"],
            "move_out_images": out_g["images"],
            **classification,
        })
    return results


def render_report(property_label, results):
    lines = [
        f"# Move-In / Move-Out Comparison — {property_label}",
        "",
        "**This is a decision-support draft, not legal advice or a final charge determination.**",
        'Arizona statute does not define "normal wear and tear" (see reference/az_hud_reference.md, Part 1.8).',
        "HUD figures below are industry guidance, not binding Arizona law. A human must review every",
        "suggested charge before it is communicated to a tenant.",
        "",
        "| Room | Item | Move-In | Move-Out | Verdict | Category | AZ Citation | HUD Citation |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        az = "; ".join(r["az_citations"]) or "—"
        hud = r["hud_citation"] or "—"
        move_in = f"{r['move_in_condition']}" + (f" — {r['move_in_notes']}" if r["move_in_notes"] else "")
        move_out = f"{r['move_out_condition']}" + (f" — {r['move_out_notes']}" if r["move_out_notes"] else "")
        lines.append(
            f"| {r['room']} | {r['item']} | {move_in} | {move_out} "
            f"| {r['verdict']} | {r['category']} | {az} | {hud} |"
        )
        if r.get("proration_note"):
            lines.append(f"| | | | | _{r['proration_note']}_ | | | |")
    lines.append("")
    lines.append(f"_{len(results)} item(s) flagged as changed between move-in and move-out._")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("move_in_pdf", type=Path)
    parser.add_argument("move_out_pdf", type=Path)
    parser.add_argument("--property", default=None, help="Property label for the report header")
    parser.add_argument("--output", type=Path, default=None, help="Write markdown report to this file (default: stdout)")
    parser.add_argument("--dump-parsed", action="store_true", help="Print parsed items as JSON instead of running the comparison (use this to debug the parser against a new PDF format)")
    args = parser.parse_args()

    kb = load_knowledge_base()
    move_in_items = parse_assessment(args.move_in_pdf)
    move_out_items = parse_assessment(args.move_out_pdf)

    if args.dump_parsed:
        def _summarize(i):
            d = dict(vars(i))
            d["images"] = f"{len(d['images'])} image(s)"
            return d
        print(json.dumps({
            "move_in": [_summarize(i) for i in move_in_items],
            "move_out": [_summarize(i) for i in move_out_items],
        }, indent=2))
        return

    if not move_in_items or not move_out_items:
        print(
            "WARNING: parser extracted 0 items from one or both PDFs. Run with --dump-parsed "
            "to inspect raw extraction, or share the PDF so the parser can be tuned to its format.",
            file=sys.stderr,
        )

    results = diff_assessments(move_in_items, move_out_items, kb)
    label = args.property or args.move_out_pdf.stem
    report = render_report(label, results)

    if args.output:
        args.output.write_text(report)
        print(f"Report written to {args.output}")
    else:
        print(report)


if __name__ == "__main__":
    main()
