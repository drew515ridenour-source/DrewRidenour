"""
find_emails.py

Step 2 of the pipeline: for each lead in leads.xlsx that has no email yet,
try to find a publicly-listed email address. Two lookup sources, tried in
order:

  1. Facebook Graph API -- search for the business's Facebook page and
     read its `emails` / `website` fields.
  2. Hunter.io Domain Search -- only if a domain is available for this
     lead. In practice that domain can only come from the Facebook page's
     own `website` field here (see note below), since find_leads.py only
     keeps businesses Google Places reports as having NO website.

If neither source turns up an email, the lead is marked `phone_only` --
which, per the addendum this implements, is still the correct, expected
outcome for a genuinely website-less business with no domain at all.

COMPLIANCE NOTE: The original spec's draft notes mention falling back to
"a simple requests search ... and scrape the Facebook page['s] About
section". That conflicts with the spec's own compliance section, which
says:

    "Only use official APIs (Google Places, Facebook Graph) -- don't
    scrape Google/Facebook HTML directly."

We follow the compliance section: this script ONLY calls official APIs
(Facebook Graph API, Hunter.io's Domain Search API) and never
fetches/parses HTML pages directly. Both require an API key/token you
generate yourself; without one configured, that lookup source is simply
skipped, not replaced with scraping.

Setup:
- Facebook: create a Meta developer app, generate a token with at least
  `pages_read_engagement` (or a basic App Access Token for public Page
  Search), put it in .env as FACEBOOK_ACCESS_TOKEN.
- Hunter.io: sign up free at hunter.io (25 searches/month on the free
  plan), grab an API key under API Keys, put it in .env as
  HUNTER_API_KEY.
Either or both can be left blank -- this script degrades gracefully and
simply marks leads `phone_only` for whichever source(s) aren't configured.

NOTE ON DOMAIN AVAILABILITY: Hunter's Domain Search needs a domain to
search, and find_leads.py deliberately only keeps leads with no
websiteUri from Google Places -- so by construction, most leads reaching
this script have no domain at all, and Hunter will have nothing to look
up for them. This is expected, not a bug (see the addendum this
implements). A dedicated domain-discovery step (e.g. checking Yelp or a
general web search for a domain a business hasn't listed with Google) is
out of scope here and left for a future addendum.
"""

import os
import re
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

HUNTER_API_KEY_ENV = "HUNTER_API_KEY"
HUNTER_DOMAIN_SEARCH_URL = "https://api.hunter.io/v2/domain-search"

REQUEST_DELAY_SECONDS = 1.0
# Hunter's free tier is also rate-limited (15 req/sec, 500/min) -- this is
# far more conservative than the limit requires, just to be a good citizen.
HUNTER_DELAY_SECONDS = 1.0


def find_facebook_contact(access_token, business_name, city):
    """
    Search the Graph API's Page Search for a page matching this business
    name near the given city, then request its `emails` and `website`
    fields.

    Returns {"email": str or None, "website": str or None}.
    Note: Facebook's Graph API restricts field access based on the
    token's permissions -- with only a basic App Access Token, this will
    often return nothing beyond id/name. That's expected and handled
    gracefully (falls through to the Hunter lookup, then phone_only).
    """
    empty = {"email": None, "website": None}
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
        return empty

    if search_resp.status_code != 200:
        print(f"  Graph API search error ({search_resp.status_code}): {search_resp.text}", file=sys.stderr)
        return empty

    candidates = search_resp.json().get("data", [])
    if not candidates:
        return empty

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
        return empty

    time.sleep(REQUEST_DELAY_SECONDS)
    try:
        page_resp = requests.get(
            GRAPH_PAGE_URL.format(page_id=page_id),
            params={"fields": "emails,link,website", "access_token": access_token},
            timeout=20,
        )
    except requests.RequestException as e:
        print(f"  Graph API page lookup failed: {e}", file=sys.stderr)
        return empty

    if page_resp.status_code != 200:
        # Most common case with a limited-permission token: 403/access
        # denied on these fields. That's not an error in our pipeline --
        # it just means we can't read them, so fall back.
        return empty

    page_data = page_resp.json()
    emails = page_data.get("emails") or []
    return {
        "email": emails[0] if emails else None,
        "website": (page_data.get("website") or "").strip() or None,
    }


def extract_domain(url):
    """Pull a bare domain (no scheme/path) out of a URL string, or None."""
    if not url:
        return None
    domain = url.strip()
    domain = re.sub(r"^https?://", "", domain, flags=re.IGNORECASE)
    domain = re.sub(r"^www\.", "", domain, flags=re.IGNORECASE)
    domain = domain.split("/")[0].split("?")[0].strip()
    # A Facebook page's "website" field is sometimes just its own Facebook
    # URL -- that's not a company domain Hunter can do anything with.
    if not domain or "facebook.com" in domain.lower():
        return None
    return domain


class HunterQuotaExhausted(Exception):
    """Raised when Hunter.io's rate limit (429) or free-plan search limit (403) is hit."""


def hunter_domain_search(api_key, domain):
    """
    Look up a domain via Hunter's Domain Search API.

    Returns an email string (preferring a `generic` role address like
    info@domain.com over a named/personal one, since we have no specific
    contact to target), or None if Hunter has nothing on file for this
    domain.

    Raises HunterQuotaExhausted if Hunter's rate limit or the free plan's
    monthly search limit has been hit, so the caller can stop making
    further Hunter calls for the rest of this run instead of hammering a
    dead quota lead after lead.
    """
    try:
        resp = requests.get(
            HUNTER_DOMAIN_SEARCH_URL,
            params={"domain": domain, "api_key": api_key},
            timeout=20,
        )
    except requests.RequestException as e:
        print(f"  Hunter.io request failed: {e}", file=sys.stderr)
        return None

    # Hunter returns 429 for rate limiting and 403 when a plan limit (e.g.
    # the free tier's 25 searches/month) is reached -- both mean "stop
    # asking Hunter for now" rather than "this lead has no answer".
    if resp.status_code in (403, 429):
        detail = f"HTTP {resp.status_code}"
        try:
            errors = resp.json().get("errors") or []
            if errors:
                detail = errors[0].get("details", detail)
        except ValueError:
            pass
        raise HunterQuotaExhausted(detail)

    if resp.status_code != 200:
        print(f"  Hunter.io error ({resp.status_code}): {resp.text}", file=sys.stderr)
        return None

    emails = ((resp.json().get("data") or {}).get("emails")) or []
    if not emails:
        return None

    generic = next((e for e in emails if e.get("type") == "generic"), None)
    best = generic or emails[0]
    return best.get("value") or None


def run():
    fb_token = os.environ.get(FB_ACCESS_TOKEN_ENV, "").strip()
    hunter_key = os.environ.get(HUNTER_API_KEY_ENV, "").strip()

    if not fb_token:
        print(f"NOTE: {FB_ACCESS_TOKEN_ENV} is not set -- skipping Facebook lookup.")
    if not hunter_key:
        print(f"NOTE: {HUNTER_API_KEY_ENV} is not set -- skipping Hunter.io lookup.")
    if not fb_token and not hunter_key:
        print("No email lookup sources configured -- every lead below will be marked phone_only.")

    leads = read_leads()
    if not leads:
        print("No leads found in leads.xlsx -- run find_leads.py first.")
        return

    hunter_quota_hit = False
    updated = 0

    for lead in leads:
        if lead["email"] or lead["status"] not in (STATUS_NEW, ""):
            continue  # already has an email, or already processed

        email = None
        website = None
        email_source = ""

        if fb_token:
            print(f"Looking up Facebook page for: {lead['name']} ...")
            # Best-effort city hint for disambiguating search results --
            # leads.xlsx stores a full address, not a separate city column.
            fb_result = find_facebook_contact(fb_token, lead["name"], lead["address"])
            time.sleep(REQUEST_DELAY_SECONDS)
            email, website = fb_result["email"], fb_result["website"]
            if email:
                email_source = "facebook"

        if not email and hunter_key and not hunter_quota_hit:
            domain = extract_domain(website)
            if domain:
                print(f"  no Facebook email -- trying Hunter.io for domain {domain} ...")
                try:
                    email = hunter_domain_search(hunter_key, domain)
                    time.sleep(HUNTER_DELAY_SECONDS)
                except HunterQuotaExhausted as e:
                    hunter_quota_hit = True
                    print(
                        f"  Hunter.io free tier limit reached this month -- skipping "
                        f"remaining Hunter lookups this run ({e}).",
                        file=sys.stderr,
                    )
                else:
                    if email:
                        email_source = "hunter"
            else:
                print("  no domain available for this lead -- Hunter.io can't help here")

        if email:
            updates = {"email": email, "status": STATUS_EMAIL_FOUND, "email_source": email_source}
            print(f"  found email via {email_source}: {email}")
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
