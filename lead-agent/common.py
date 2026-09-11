"""
Shared helpers used across the lead-gen pipeline scripts:
find_leads.py -> find_emails.py -> generate_emails.py -> review_emails.py -> send_emails.py

Keeping this logic in one place means every script agrees on the CSV schema,
loads config the same way, and fails the same way when a required API key
is missing.
"""

import csv
import os
import sys
from pathlib import Path

# Loading .env is optional at import time (a shell env can supply the same
# vars) but we try it so `python find_leads.py` works out of the box.
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

BASE_DIR = Path(__file__).resolve().parent
LEADS_CSV = BASE_DIR / "leads.csv"
SEND_LOG_CSV = BASE_DIR / "send_log.csv"

# Canonical column order for leads.csv. Every script reads/writes this exact
# shape so a partially-completed row from an earlier step never gets
# silently dropped or reordered.
LEADS_FIELDS = [
    "name",
    "address",
    "phone",
    "category",
    "google_maps_url",
    "facebook_url",
    "email",
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


def read_leads():
    """Return leads.csv as a list of dicts, normalized to LEADS_FIELDS."""
    if not LEADS_CSV.exists():
        return []
    with open(LEADS_CSV, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    normalized = []
    for row in rows:
        normalized.append({field: row.get(field, "") or "" for field in LEADS_FIELDS})
    return normalized


def write_leads(rows):
    """Overwrite leads.csv with `rows` (list of dicts), in canonical column order."""
    with open(LEADS_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=LEADS_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") or "" for field in LEADS_FIELDS})


def normalize_phone(phone):
    """Digits-only phone number, used as part of the dedup key."""
    return "".join(ch for ch in (phone or "") if ch.isdigit())


def normalize_name(name):
    return (name or "").strip().lower()


def dedup_key(name, phone):
    return (normalize_name(name), normalize_phone(phone))


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
