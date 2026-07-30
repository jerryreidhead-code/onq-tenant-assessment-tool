#!/usr/bin/env python3
"""
Compare a move-in and move-out property condition assessment PDF and suggest
which condition changes are plausibly chargeable to the tenant, cross-referenced
against Arizona landlord-tenant statute and HUD useful-life/damage guidance.

NOT LEGAL ADVICE. See reference/az_hud_reference.md for citations and known gaps
(Arizona law does not define "normal wear and tear"; HUD figures are industry
guidance, not binding AZ law). A human must review every suggested charge before
it is communicated to a tenant.

Usage:
    python3 compare_assessments.py move_in.pdf move_out.pdf --property "123 Main St"
    python3 compare_assessments.py move_in.pdf move_out.pdf --dump-parsed   # debug parser
"""
import argparse
import json
import re
import sys
from dataclasses import dataclass
from difflib import SequenceMatcher
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

# Items that are the property manager's own equipment/access, not part of the tenant's
# unit condition -- always excluded from the comparison, never shown in the report.
EXCLUDED_ITEM_KEYWORDS = ["lockbox", "lock box"]


def is_excluded_item(item_name):
    name = item_name.lower()
    return any(kw in name for kw in EXCLUDED_ITEM_KEYWORDS)


def load_knowledge_base():
    with open(KB_PATH) as f:
        return json.load(f)


def extract_text_and_tables(pdf_path):
    """Returns (full_text, list_of_tables) using pdfplumber."""
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
    Fallback line-based parser for non-tabular PDFs. Looks for room headers
    (short line matching a known room keyword) and "Item: condition - notes"
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
    text, tables = extract_text_and_tables(pdf_path)
    items = parse_from_tables(tables) if tables else []
    if not items:
        items = parse_from_text(text)
    return items, text


def normalize_key(room, item):
    return f"{room.strip().lower()}::{re.sub(r'[^a-z0-9]+', ' ', item.strip().lower()).strip()}"


def index_items(items):
    return {normalize_key(it.room, it.item): it for it in items}


def fuzzy_match_category(item_name, notes, kb):
    haystack = f"{item_name} {notes}".lower()
    best, best_score = None, 0.0
    for cat in kb["categories"]:
        score = 0.0
        for kw in cat["match_keywords"]:
            if kw in haystack:
                score = max(score, 1.0)
            else:
                score = max(score, SequenceMatcher(None, kw, haystack).ratio())
        if score > best_score:
            best_score, best = score, cat
    return best if best_score >= 0.35 else None


def classify_change(move_out_item, kb):
    category = fuzzy_match_category(move_out_item.item, move_out_item.notes, kb)
    text = f"{move_out_item.condition} {move_out_item.notes}".lower()

    verdict = "NEEDS HUMAN REVIEW"
    matched_examples = []
    if category:
        for phrase in category.get("damage_examples", []):
            if phrase.lower() in text:
                verdict = "LIKELY TENANT-CHARGEABLE"
                matched_examples.append(phrase)
        if verdict == "NEEDS HUMAN REVIEW":
            for phrase in category.get("wear_and_tear_examples", []):
                if phrase.lower() in text:
                    verdict = "LIKELY NORMAL WEAR & TEAR (not chargeable)"
                    matched_examples.append(phrase)

    return {
        "category": category["label"] if category else "Uncategorized",
        "verdict": verdict,
        "matched_examples": matched_examples,
        "az_citations": category["az_citations"] if category else [],
        "hud_citation": category.get("hud_citation") if category else None,
        "useful_life_years": category.get("useful_life_years") if category else None,
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
    move_in_idx = index_items(move_in_items)
    move_out_idx = index_items(move_out_items)

    results = []
    for key, out_item in move_out_idx.items():
        if is_excluded_item(out_item.item):
            continue
        in_item = move_in_idx.get(key)
        in_condition = in_item.condition if in_item else "(not present at move-in)"
        unchanged = in_item and in_item.condition.strip().lower() == out_item.condition.strip().lower() and not out_item.notes
        if unchanged:
            continue
        classification = classify_change(out_item, kb)
        results.append({
            "room": out_item.room,
            "item": out_item.item,
            "move_in_condition": in_condition,
            "move_out_condition": out_item.condition,
            "move_out_notes": out_item.notes,
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
        lines.append(
            f"| {r['room']} | {r['item']} | {r['move_in_condition']} | {r['move_out_condition']} "
            f"| {r['verdict']} | {r['category']} | {az} | {hud} |"
        )
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
    move_in_items, _ = parse_assessment(args.move_in_pdf)
    move_out_items, _ = parse_assessment(args.move_out_pdf)

    if args.dump_parsed:
        print(json.dumps({
            "move_in": [vars(i) for i in move_in_items],
            "move_out": [vars(i) for i in move_out_items],
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
