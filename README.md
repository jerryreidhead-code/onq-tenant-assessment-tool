# Disposition Tool

Move-in / move-out tenant charge assessment comparison.

Compares a property's move-in and move-out condition assessment PDFs and suggests
which condition changes are plausibly chargeable to the tenant, citing Arizona
landlord-tenant statute and HUD useful-life / damage-classification guidance for
each suggestion.

**This tool produces a decision-support draft, not legal advice or a final charge
determination.** See [`reference/az_hud_reference.md`](reference/az_hud_reference.md)
for full citations and known gaps — notably, Arizona statute does not define
"normal wear and tear"; HUD figures used here are industry guidance, not binding
Arizona law. A human must review every suggested charge before it is communicated
to a tenant.

## Status

Assessments currently live in Salesforce (no connector yet) — for now, export/download
the move-in and move-out assessment PDFs manually and pass them as file paths. When a
Salesforce connector becomes available, only `parse_assessment()`'s input source needs
to change; the diff/classification/reporting logic stays the same.

**The PDF parser has not yet been tuned against a real Propertyware/Salesforce
assessment export** — it was built and smoke-tested against synthetic text only
(see the `_meta` note in `reference/knowledge_base.json`). Run `--dump-parsed`
against a real sample first (see below) before trusting the comparison output.

## Setup

Already verified working with the system Python 3 (`pdfplumber` and `pypdf` are installed).

## Usage

```bash
# Debug: see exactly what the parser extracted from a real PDF, before trusting the diff
python3 compare_assessments.py move_in.pdf move_out.pdf --dump-parsed

# Full comparison report (prints markdown to stdout)
python3 compare_assessments.py move_in.pdf move_out.pdf --property "123 Main St, Tempe AZ"

# Write the report to a file instead
python3 compare_assessments.py move_in.pdf move_out.pdf --output report.md
```

## How it works

1. `extract_text_and_tables()` pulls raw text and any tables out of each PDF via `pdfplumber`.
2. `parse_from_tables()` / `parse_from_text()` turn that into a flat list of
   `AssessmentItem(room, item, condition, notes)` records — table-based parsing is tried
   first (most condition-report forms are tables), falling back to line-based text parsing.
3. `diff_assessments()` matches items between the two assessments by room+item name and
   keeps only ones whose condition or notes changed.
4. `classify_change()` fuzzy-matches the item name/notes against `reference/knowledge_base.json`
   categories, then checks the move-out notes against that category's HUD-sourced
   wear-and-tear vs. damage example phrases to produce a verdict:
   `LIKELY TENANT-CHARGEABLE`, `LIKELY NORMAL WEAR & TEAR (not chargeable)`, or
   `NEEDS HUMAN REVIEW` (no confident phrase match — always err toward flagging for review).
5. `render_report()` outputs a markdown table with move-in vs. move-out condition, verdict,
   and the specific AZ statute + HUD citation backing that category.

`compute_proration()` implements HUD's useful-life proration formula
(`repair_cost × remaining_life / total_life`) for categories with a known HUD life
expectancy (carpet, paint, appliances, blinds, tile) — but needs an item install/last-replaced
date as input, which assessment PDFs typically don't contain. Feed it manually per item
when you have that date (e.g., from a maintenance/capex record) to get a defensible
prorated charge amount instead of the full replacement cost.

## Next steps once real samples are available

1. Run `--dump-parsed` against one real move-in and one real move-out PDF.
2. If item counts are 0 or rooms/items look wrong, share the raw extracted text/tables
   so `parse_from_tables()`/`parse_from_text()` can be adjusted to the actual column
   layout and room-header convention Salesforce/Propertyware produces.
3. Expand `reference/knowledge_base.json` wear-and-tear / damage phrase lists based on
   the actual vocabulary your inspectors use in condition notes (the current phrases are
   HUD's own example language, which may not match word-for-word).

## Web app (browser UI)

`webapp/app.py` is a Flask front end over the exact same `compare_assessments.py`
functions — upload two PDFs in a browser, get the same report rendered as an HTML table
instead of markdown. Uploaded PDFs are parsed in memory and never written to disk.

**Recent-analyses search:** the last `MAX_CACHED_REPORTS` (8) comparisons are kept in an
in-memory cache (`REPORT_CACHE` in `app.py`) so a property can be searched up again from
the home page without re-uploading PDFs. This is deliberately in-memory only, not a real
database — restarting the server clears it, consistent with the tool's "nothing is stored"
posture. If this needs to survive restarts later, that's a real scope change (encryption at
rest, access control, retention/deletion policy), not a quick add.

Run it locally:

```bash
source .venv/bin/activate   # created via: python3 -m venv .venv && pip install -r requirements.txt
python3 webapp/app.py       # http://127.0.0.1:5000
```

### Security notes (read before putting this on a real public URL)

This tool will handle tenant PII (names, addresses, damage photos/descriptions). Before
it's reachable outside your own machine:

1. **Set `APP_PASSWORD`** (env var) — gates the whole app behind a shared password. This
   is a minimum bar for a small internal tool, not real per-user accounts/roles. If On Q
   needs individual logins or an audit trail of who ran what, that's a bigger change than
   what's built here — flag it if you want that instead of a shared password.
2. **Set `FLASK_SECRET_KEY`** (env var, random string) — without it, a new random key is
   generated on every restart, which invalidates all sessions each time the app redeploys.
3. **Serve over HTTPS only.** Most hosts (Render, Fly.io, Railway, etc.) provide this
   automatically for their default domain — don't disable it.
4. Uploaded PDFs are never written to disk by the app itself, but reverse proxies/hosts
   sometimes log request bodies or keep temp files — worth confirming with whichever host
   you pick.

### Deploying

A `Dockerfile` and `requirements.txt` are included so this can be deployed to any host that
builds containers (Render, Fly.io, Railway, AWS App Runner, etc.) or run directly with
`gunicorn` on a VM. Local build/run:

```bash
docker build -t onq-assessment-tool .
docker run -p 8000:8000 -e APP_PASSWORD=changeme -e FLASK_SECRET_KEY=$(openssl rand -hex 32) onq-assessment-tool
```

**Note:** Docker isn't installed in the environment this was built in, so the image build
itself hasn't been test-run yet — the Flask app was verified directly (dev server + a real
end-to-end PDF upload via curl). Test the Docker build once you've picked a host, since most
container hosts build it for you from this repo anyway.
