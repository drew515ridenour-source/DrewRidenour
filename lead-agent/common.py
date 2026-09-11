"""
Shared helpers used across the lead-gen pipeline scripts:
find_leads.py -> find_emails.py -> generate_emails.py -> review_emails.py -> send_emails.py

Keeping this logic in one place means every script agrees on the workbook
schema, loads config the same way, and fails the same way when a required
API key is missing.

Leads live in `leads.xlsx` (an Excel workbook, via openpyxl) so they're easy
to open, sort, and eyeball outside the pipeline. `send_log.csv` stays plain
CSV -- it's an append-only log, not a working dataset, so a spreadsheet
buys nothing there.
"""

import csv
import os
import sys
from pathlib import Path

import openpyxl
from openpyxl.styles import Font

# Loading .env is optional at import time (a shell env can supply the same
# vars) but we try it so `python find_leads.py` works out of the box.
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

BASE_DIR = Path(__file__).resolve().parent
LEADS_XLSX = BASE_DIR / "leads.xlsx"
SEND_LOG_CSV = BASE_DIR / "send_log.csv"

SHEET_NAME = "Leads"

# Canonical column order for leads.xlsx. Every script reads/writes this
# exact shape so a partially-completed row from an earlier step never gets
# silently dropped or reordered.
LEADS_FIELDS = [
    "name",
    "address",
    "phone",
    "category",
    "google_maps_url",
    "facebook_url",
    "email",
    "email_source",  # "facebook", "hunter", or "serper+hunter" -- which lookup(s) found this email
    "draft_email",
    "status",
]

# Lifecycle of the `status` column, in order:
#   new          -> written by find_leads.py, no email yet
#   phone_only   -> find_emails.py could not find a public email
#   email_found  -> find_emails.py found an email, ready for drafting
#   drafted      -> generate_emails.py wrote a draft_email
#   ready_to_send-> approved during the manual review step
#   skipped      -> rejected during the manual review step
#   sent         -> send_emails.py successfully sent it
#   send_failed  -> send_emails.py tried and failed (retryable)
STATUS_NEW = "new"
STATUS_PHONE_ONLY = "phone_only"
STATUS_EMAIL_FOUND = "email_found"
STATUS_DRAFTED = "drafted"
STATUS_READY_TO_SEND = "ready_to_send"
STATUS_SKIPPED = "skipped"
STATUS_SENT = "sent"
STATUS_SEND_FAILED = "send_failed"

SEND_LOG_FIELDS = ["timestamp", "recipient", "business_name", "status", "message"]


def require_env(*names):
    """
    Fail fast with a clear message instead of a confusing stack trace deep
    inside `requests` or `smtplib` when a required API key / config value
    is missing from .env.
    """
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        print(
            "ERROR: missing required environment variable(s): "
            + ", ".join(missing)
            + "\nCopy .env.example to .env and fill these in before running this script.",
            file=sys.stderr,
        )
        sys.exit(1)


# --------------------------------------------------------------------------
# leads.xlsx helpers
#
# Every write here re-saves the whole workbook. That's the simplest way to
# guarantee formatting (bold header, frozen header row, auto-width columns)
# stays correct after every mutation, and openpyxl loads the whole file into
# memory on open regardless -- for a local lead list (tens/hundreds of rows,
# not millions) the cost is negligible next to an API round-trip.
# --------------------------------------------------------------------------


def _format_header(ws):
    for cell in ws[1]:
        cell.font = Font(bold=True)
    ws.freeze_panes = "A2"


def _autofit_columns(ws):
    widths = {}
    for row in ws.iter_rows():
        for cell in row:
            if cell.value is None:
                continue
            widths[cell.column_letter] = max(widths.get(cell.column_letter, 0), len(str(cell.value)))
    for col_letter, width in widths.items():
        ws.column_dimensions[col_letter].width = min(max(width + 2, 10), 60)


def _new_workbook():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = SHEET_NAME
    ws.append(LEADS_FIELDS)
    _format_header(ws)
    return wb, ws


def _load_or_create_workbook():
    if LEADS_XLSX.exists():
        wb = openpyxl.load_workbook(LEADS_XLSX)
        ws = wb[SHEET_NAME] if SHEET_NAME in wb.sheetnames else wb.active
        return wb, ws
    return _new_workbook()


def read_leads():
    """Return leads.xlsx as a list of dicts, normalized to LEADS_FIELDS."""
    if not LEADS_XLSX.exists():
        return []
    wb = openpyxl.load_workbook(LEADS_XLSX)
    ws = wb[SHEET_NAME] if SHEET_NAME in wb.sheetnames else wb.active
    header = [cell.value for cell in ws[1]]

    rows = []
    for raw_row in ws.iter_rows(min_row=2, values_only=True):
        if raw_row is None or all(v is None or str(v).strip() == "" for v in raw_row):
            continue  # skip blank trailing rows
        row_dict = dict(zip(header, raw_row))
        rows.append({field: (row_dict.get(field) or "") for field in LEADS_FIELDS})
    return rows


def write_leads(rows):
    """Overwrite leads.xlsx with `rows` (list of dicts), in canonical column order."""
    wb, ws = _new_workbook()
    for row in rows:
        ws.append([row.get(field, "") or "" for field in LEADS_FIELDS])
    _autofit_columns(ws)
    wb.save(LEADS_XLSX)


def append_lead_row(row):
    """
    Append a single new lead to leads.xlsx immediately (creating the
    workbook if needed) and save. Used by find_leads.py so a long search
    run doesn't lose progress if it's interrupted partway through.
    """
    wb, ws = _load_or_create_workbook()
    ws.append([row.get(field, "") or "" for field in LEADS_FIELDS])
    _autofit_columns(ws)
    wb.save(LEADS_XLSX)


def update_lead_row(name, phone, updates):
    """
    Find the row matching the (name, phone) dedup key and merge `updates`
    (a dict of field -> value) into it in place, saving immediately. Used
    by find_emails.py so each resolved lead is persisted as it's processed,
    not just at the end of the run. Returns True if a matching row was found.
    """
    wb, ws = _load_or_create_workbook()
    header = [cell.value for cell in ws[1]]
    if "name" not in header or "phone" not in header:
        return False
    name_idx = header.index("name")
    phone_idx = header.index("phone")
    target_key = dedup_key(name, phone)

    for row in ws.iter_rows(min_row=2):
        row_key = dedup_key(row[name_idx].value, row[phone_idx].value)
        if row_key == target_key:
            for field, value in updates.items():
                if field in header:
                    row[header.index(field)].value = value
            _autofit_columns(ws)
            wb.save(LEADS_XLSX)
            return True
    return False


def existing_dedup_keys():
    """(name, phone) keys already present in leads.xlsx, for dedup in find_leads.py."""
    return {dedup_key(row["name"], row["phone"]) for row in read_leads()}


def normalize_phone(phone):
    """Digits-only phone number, used as part of the dedup key."""
    return "".join(ch for ch in str(phone or "") if ch.isdigit())


def normalize_name(name):
    return str(name or "").strip().lower()


def dedup_key(name, phone):
    return (normalize_name(name), normalize_phone(phone))


# --------------------------------------------------------------------------
# send_log.csv helpers (plain CSV, append-only)
# --------------------------------------------------------------------------


def append_send_log(timestamp, recipient, business_name, status, message=""):
    is_new = not SEND_LOG_CSV.exists()
    with open(SEND_LOG_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=SEND_LOG_FIELDS)
        if is_new:
            writer.writeheader()
        writer.writerow(
            {
                "timestamp": timestamp,
                "recipient": recipient,
                "business_name": business_name,
                "status": status,
                "message": message,
            }
        )


def count_sends_today():
    """How many rows in send_log.csv have today's date, for the daily rate limit."""
    import datetime

    if not SEND_LOG_CSV.exists():
        return 0
    today = datetime.date.today().isoformat()
    count = 0
    with open(SEND_LOG_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ts = row.get("timestamp", "")
            if ts.startswith(today) and row.get("status") == "sent":
                count += 1
    return count
