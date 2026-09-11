"""
generate_emails.py

Step 3 of the pipeline: for each lead with an email address, ask Claude to
draft a short, personalized outreach email pitching a new website.

This script only DRAFTS emails into the `draft_email` column and sets
status to `drafted`. It never sends anything -- review_emails.py (manual
approval) and send_emails.py (actual sending) are separate steps by design,
so a bad prompt or API hiccup can never result in an email going out
unreviewed.
"""

import os
import sys

from anthropic import Anthropic

from common import (
    STATUS_DRAFTED,
    STATUS_EMAIL_FOUND,
    read_leads,
    require_env,
    write_leads,
)

ANTHROPIC_API_KEY_ENV = "ANTHROPIC_API_KEY"

# Model id from the project spec. If Anthropic renames/retires this model,
# override it via ANTHROPIC_MODEL in .env rather than editing this file.
DEFAULT_MODEL = "claude-sonnet-4-6"

INTRO_PRICE = 500
STANDARD_PRICE = 750

PROMPT_TEMPLATE = """You are writing a short cold-outreach email for a web design agency \
in Iowa. Write ONLY the email body text (no subject line, no explanation, no markdown).

Business being contacted:
- Name: {name}
- Category: {category}
- City: {city}

Context: this business currently has no website (confirmed via Google Places). \
We build small-business websites. Intro price is ${intro_price} for the first \
site, standard price is ${standard_price} after that, with an optional monthly \
maintenance plan.

Requirements for the email:
- Under 150 words.
- Friendly, personalized, no hard sell or pressure tactics.
- Mention the business by name and that we noticed they don't have a website yet.
- Mention the ${intro_price} intro price and that it's normally ${standard_price}.
- Mention the optional monthly maintenance plan briefly.
- Include a clear, low-friction call to action (reply to this email or call).
- Sign off with exactly:
[Your Name]
[Your Phone]
"""


def build_prompt(name, category, city):
    return PROMPT_TEMPLATE.format(
        name=name,
        category=category or "local business",
        city=city,
        intro_price=INTRO_PRICE,
        standard_price=STANDARD_PRICE,
    )


def guess_city(address):
    """Best-effort city extraction from a formatted address for the prompt."""
    parts = [p.strip() for p in (address or "").split(",")]
    # formattedAddress is typically "street, city, ST zip, country" -- city
    # is usually the second comma-separated segment.
    if len(parts) >= 2:
        return parts[-3] if len(parts) >= 3 else parts[-2]
    return address or "Iowa City, IA"


def run():
    require_env(ANTHROPIC_API_KEY_ENV)
    model = os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL)
    client = Anthropic(api_key=os.environ[ANTHROPIC_API_KEY_ENV])

    leads = read_leads()
    if not leads:
        print("No leads found in leads.xlsx -- run find_leads.py and find_emails.py first.")
        return

    drafted = 0
    for lead in leads:
        if not lead["email"]:
            continue  # no email to send to -- skip (phone_only leads)
        if lead["status"] not in (STATUS_EMAIL_FOUND, ""):
            continue  # already drafted / reviewed / sent

        city = guess_city(lead["address"])
        prompt = build_prompt(lead["name"], lead["category"], city)

        print(f"Drafting email for: {lead['name']} <{lead['email']}> ...")
        try:
            response = client.messages.create(
                model=model,
                max_tokens=400,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as e:  # noqa: BLE001 -- surface any API error and keep going
            print(f"  ERROR generating draft for {lead['name']}: {e}", file=sys.stderr)
            continue

        draft_text = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        ).strip()

        if not draft_text:
            print(f"  WARNING: empty draft returned for {lead['name']}, skipping.", file=sys.stderr)
            continue

        lead["draft_email"] = draft_text
        lead["status"] = STATUS_DRAFTED
        drafted += 1

    write_leads(leads)
    print(f"\nDone. Drafted {drafted} email(s). Next: run review_emails.py to approve/edit before sending.")


if __name__ == "__main__":
    run()
