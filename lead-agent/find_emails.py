"""
find_emails.py

Step 2 of the pipeline: for each lead in leads.xlsx that has no email yet,
try to find a publicly-listed email address via the Facebook Graph API.

COMPLIANCE NOTE: The spec's draft notes mention falling back to "a simple
requests search ... and scrape the Facebook page['s] About section". That
conflicts with the spec's own compliance section, which says:

    "Only use official APIs (Google Places, Facebook Graph) -- don't
    scrape Google/Facebook HTML directly."

We follow the compliance section: this script ONLY calls the official
Facebook Graph API (which requires an access token you generate yourself
in the Meta Developer portal) and never fetches/parses Facebook HTML
pages directly. If the Graph API can't find an email (no token configured,
page not found, or the business simply hasn't listed one publicly), the
lead is marked `phone_only` and left for manual follow-up -- it is NOT
scraped as a fallback.

Setup: create a Meta developer app, generate a token with at least the
`pages_read_engagement` permission (or use an App Access Token for basic
public Page Search), and put it in .env as FACEBOOK_ACCESS_TOKEN.
Without a token configured, this script simply marks every lead
`phone_only` and exits cleanly -- it will not error out, since email
discovery is optional and the pipeline can continue on phone contact alone.
"""

import os
import sys
import time

import requests

from common import (
    STATUS_EMAIL_FOUND,
    STATUS_NEW,
    STATUS_PHONE_ONLY,
    read_leads,
    update_lead_row,
)

FB_ACCESS_TOKEN_ENV = "FACEBOOK_ACCESS_TOKEN"
GRAPH_API_VERSION = "v19.0"
GRAPH_SEARCH_URL = f"https://graph.facebook.com/{GRAPH_API_VERSION}/pages/search"
GRAPH_PAGE_URL = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{{page_id}}"

REQUEST_DELAY_SECONDS = 1.0


def find_facebook_email(access_token, business_name, city):
    """
    Search the Graph API's Page Search for a page matching this business
    name near the given city, then request its `emails` field.

    Returns an email string, or None if not found / not accessible.
    Note: Facebook's Graph API restricts `pages/search` and page field
    access based on the token's permissions -- with only a basic App
    Access Token, this will often return nothing beyond id/name. That's
    expected and handled gracefully (falls through to phone_only).
    """
    try:
        search_resp = requests.get(
            GRAPH_SEARCH_URL,
            params={
                "q": business_name,
                "access_token": access_token,
                "fields": "id,name,location",
            },
            timeout=20,
        )
    except requests.RequestException as e:
        print(f"  Graph API search request failed: {e}", file=sys.stderr)
        return None

    if search_resp.status_code != 200:
        print(f"  Graph API search error ({search_resp.status_code}): {search_resp.text}", file=sys.stderr)
        return None

    candidates = search_resp.json().get("data", [])
    if not candidates:
        return None

    # Prefer a candidate whose location city matches, otherwise just take
    # the first result -- Graph Search already ranks by relevance to `q`.
    city_short = city.split(",")[0].strip().lower()
    best = None
    for cand in candidates:
        loc_city = (cand.get("location", {}) or {}).get("city", "").strip().lower()
        if loc_city and city_short and loc_city == city_short:
            best = cand
            break
    if best is None:
        best = candidates[0]

    page_id = best.get("id")
    if not page_id:
        return None

    time.sleep(REQUEST_DELAY_SECONDS)
    try:
        page_resp = requests.get(
            GRAPH_PAGE_URL.format(page_id=page_id),
            params={"fields": "emails,link", "access_token": access_token},
            timeout=20,
        )
    except requests.RequestException as e:
        print(f"  Graph API page lookup failed: {e}", file=sys.stderr)
        return None

    if page_resp.status_code != 200:
        # Most common case with a limited-permission token: 403/access
        # denied on the `emails` field. That's not an error in our
        # pipeline -- it just means we can't read it, so fall back.
        return None

    page_data = page_resp.json()
    emails = page_data.get("emails") or []
    return emails[0] if emails else None


def run():
    access_token = os.environ.get(FB_ACCESS_TOKEN_ENV, "").strip()
    if not access_token:
        print(
            f"NOTE: {FB_ACCESS_TOKEN_ENV} is not set in .env -- skipping Facebook "
            "lookup and marking all emailless leads as phone_only.\n"
            "See find_emails.py's module docstring for setup instructions."
        )

    leads = read_leads()
    if not leads:
        print("No leads found in leads.xlsx -- run find_leads.py first.")
        return

    updated = 0
    for lead in leads:
        if lead["email"] or lead["status"] not in (STATUS_NEW, ""):
            continue  # already has an email, or already processed

        if access_token:
            print(f"Looking up Facebook page for: {lead['name']} ...")
            # Best-effort city hint for disambiguating search results --
            # leads.xlsx stores a full address, not a separate city column.
            email = find_facebook_email(access_token, lead["name"], lead["address"])
            time.sleep(REQUEST_DELAY_SECONDS)
        else:
            email = None

        if email:
            updates = {"email": email, "status": STATUS_EMAIL_FOUND}
            print(f"  found email: {email}")
        else:
            updates = {"status": STATUS_PHONE_ONLY}
            print("  no public email found -- marked phone_only")

        # Save after each row, not just at the end, so an interrupted run
        # doesn't lose the lookups already done.
        update_lead_row(lead["name"], lead["phone"], updates)
        updated += 1

    print(f"\nDone. Processed {updated} lead(s) without an email.")


if __name__ == "__main__":
    run()
