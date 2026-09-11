"""
send_emails.py

Step 5 of the pipeline: send approved outreach emails (status ==
`ready_to_send`), rate-limited and logged.

COMPLIANCE (CAN-SPAM), baked in as non-optional behavior, not a suggestion:
  - Every email includes a real physical mailing address in the footer.
  - Every email includes a clear opt-out / "reply STOP to opt out" line.
  - The subject line is honest -- no misleading claims, no fake "Re:"/"Fwd:"
    prefixes, no disguising this as a personal reply.
These are added by build_footer()/SUBJECT_LINE below and are always
appended -- there is no code path that sends an email without them.

Rate limiting: sends are capped at DAILY_SEND_LIMIT per day (default 25,
configurable in .env) and paused 60-120 seconds between sends, to protect
the sending domain's reputation and avoid tripping spam filters. This is a
courtesy/deliverability measure, not a security control -- don't rely on it
to enforce a hard external limit.

Supports two providers, chosen via SEND_PROVIDER in .env:
  - "smtp"     -- Gmail (with an App Password) or any SMTP server
  - "sendgrid" -- SendGrid Web API
  - "mailgun"  -- Mailgun Web API
"""

import datetime
import os
import random
import smtplib
import sys
import time
from email.mime.text import MIMEText

import requests

from common import (
    STATUS_READY_TO_SEND,
    STATUS_SENT,
    STATUS_SEND_FAILED,
    append_send_log,
    count_sends_today,
    read_leads,
    require_env,
    write_leads,
)

MIN_DELAY_SECONDS = 60
MAX_DELAY_SECONDS = 120

SUBJECT_LINE = "Quick note about {business_name}'s website"


def build_footer(from_name, mailing_address, reply_to):
    # CAN-SPAM: physical address + opt-out are required in every email,
    # not optional -- this footer is always appended in send_one().
    return (
        f"\n\n--\n{from_name}\n{mailing_address}\n\n"
        f"If you'd rather not hear from us again, just reply to this email "
        f"with \"unsubscribe\" or let {reply_to} know, and we won't reach out again."
    )


def build_body(draft_email, footer):
    return f"{draft_email}{footer}"


def send_via_smtp(to_email, subject, body, cfg):
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = f"{cfg['from_name']} <{cfg['from_email']}>"
    msg["To"] = to_email
    msg["Reply-To"] = cfg["reply_to"]

    with smtplib.SMTP(cfg["smtp_host"], int(cfg["smtp_port"]), timeout=30) as server:
        server.starttls()
        server.login(cfg["smtp_user"], cfg["smtp_pass"])
        server.sendmail(cfg["from_email"], [to_email], msg.as_string())


def send_via_sendgrid(to_email, subject, body, cfg):
    resp = requests.post(
        "https://api.sendgrid.com/v3/mail/send",
        headers={
            "Authorization": f"Bearer {cfg['sendgrid_api_key']}",
            "Content-Type": "application/json",
        },
        json={
            "personalizations": [{"to": [{"email": to_email}]}],
            "from": {"email": cfg["from_email"], "name": cfg["from_name"]},
            "reply_to": {"email": cfg["reply_to"]},
            "subject": subject,
            "content": [{"type": "text/plain", "value": body}],
        },
        timeout=30,
    )
    if resp.status_code >= 300:
        raise RuntimeError(f"SendGrid error {resp.status_code}: {resp.text}")


def send_via_mailgun(to_email, subject, body, cfg):
    resp = requests.post(
        f"https://api.mailgun.net/v3/{cfg['mailgun_domain']}/messages",
        auth=("api", cfg["mailgun_api_key"]),
        data={
            "from": f"{cfg['from_name']} <{cfg['from_email']}>",
            "to": [to_email],
            "subject": subject,
            "text": body,
            "h:Reply-To": cfg["reply_to"],
        },
        timeout=30,
    )
    if resp.status_code >= 300:
        raise RuntimeError(f"Mailgun error {resp.status_code}: {resp.text}")


def load_config():
    provider = os.environ.get("SEND_PROVIDER", "smtp").strip().lower()

    common_required = ["FROM_NAME", "FROM_EMAIL", "REPLY_TO", "MAILING_ADDRESS"]
    if provider == "smtp":
        require_env(*common_required, "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS")
    elif provider == "sendgrid":
        require_env(*common_required, "SENDGRID_API_KEY")
    elif provider == "mailgun":
        require_env(*common_required, "MAILGUN_API_KEY", "MAILGUN_DOMAIN")
    else:
        print(f"ERROR: unknown SEND_PROVIDER {provider!r} -- expected smtp, sendgrid, or mailgun.", file=sys.stderr)
        sys.exit(1)

    return provider, {
        "from_name": os.environ["FROM_NAME"],
        "from_email": os.environ["FROM_EMAIL"],
        "reply_to": os.environ["REPLY_TO"],
        "mailing_address": os.environ["MAILING_ADDRESS"],
        "smtp_host": os.environ.get("SMTP_HOST", ""),
        "smtp_port": os.environ.get("SMTP_PORT", "587"),
        "smtp_user": os.environ.get("SMTP_USER", ""),
        "smtp_pass": os.environ.get("SMTP_PASS", ""),
        "sendgrid_api_key": os.environ.get("SENDGRID_API_KEY", ""),
        "mailgun_api_key": os.environ.get("MAILGUN_API_KEY", ""),
        "mailgun_domain": os.environ.get("MAILGUN_DOMAIN", ""),
    }


def send_one(provider, cfg, lead):
    footer = build_footer(cfg["from_name"], cfg["mailing_address"], cfg["reply_to"])
    body = build_body(lead["draft_email"], footer)
    subject = SUBJECT_LINE.format(business_name=lead["name"])

    if provider == "smtp":
        send_via_smtp(lead["email"], subject, body, cfg)
    elif provider == "sendgrid":
        send_via_sendgrid(lead["email"], subject, body, cfg)
    elif provider == "mailgun":
        send_via_mailgun(lead["email"], subject, body, cfg)


def run():
    provider, cfg = load_config()
    daily_limit = int(os.environ.get("DAILY_SEND_LIMIT", "25"))

    leads = read_leads()
    queue = [row for row in leads if row["status"] == STATUS_READY_TO_SEND]

    if not queue:
        print("No leads with status ready_to_send. Run review_emails.py to approve some drafts first.")
        return

    already_sent_today = count_sends_today()
    remaining_today = max(0, daily_limit - already_sent_today)
    if remaining_today == 0:
        print(f"Daily send limit ({daily_limit}) already reached today. Try again tomorrow.")
        return

    print(f"{len(queue)} lead(s) ready to send. Sending up to {remaining_today} today "
          f"(already sent {already_sent_today}/{daily_limit}).")

    sent_count = 0
    for lead in queue:
        if sent_count >= remaining_today:
            print(f"Reached daily send limit ({daily_limit}). Stopping -- resume tomorrow.")
            break

        timestamp = datetime.datetime.now().isoformat(timespec="seconds")
        try:
            send_one(provider, cfg, lead)
        except Exception as e:  # noqa: BLE001 -- log and keep going with the next lead
            print(f"  FAILED to send to {lead['name']} <{lead['email']}>: {e}", file=sys.stderr)
            lead["status"] = STATUS_SEND_FAILED
            append_send_log(timestamp, lead["email"], lead["name"], "failed", str(e))
            write_leads(leads)
            continue

        lead["status"] = STATUS_SENT
        append_send_log(timestamp, lead["email"], lead["name"], "sent")
        write_leads(leads)  # persist after every send, not just at the end
        sent_count += 1
        print(f"  sent to {lead['name']} <{lead['email']}> ({sent_count}/{remaining_today})")

        is_last = sent_count >= remaining_today or lead is queue[-1]
        if not is_last:
            delay = random.randint(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS)
            print(f"  waiting {delay}s before next send (rate limiting)...")
            time.sleep(delay)

    print(f"\nDone. Sent {sent_count} email(s) this run. See send_log.csv for the full log.")


if __name__ == "__main__":
    run()
