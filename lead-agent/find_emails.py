"""
find_emails.py

Step 2 of the pipeline: for each lead in leads.xlsx that has no email yet,
try to find a publicly-listed email address. Sources are tried in order,
each one only running if the previous ones didn't already produce what's
needed:

  1. Facebook Graph API -- search for the business's Facebook page and
     read its `emails` / `website` fields. May directly yield an email,
     or a domain (via `website`) to hand to Hunter.
  2. Serper.dev domain discovery -- if step 1 didn't find a domain, run
     a real Google search (via Serper's Search API, not HTML scraping)
     for the business name + address and take the first plausible,
     non-directory result as a candidate domain.
  3. Hunter.io Domain Search -- if any step above found a domain (but not
     yet an email), look up that domain for a contact email.

If nothing turns up anywhere, the lead is marked `phone_only` -- which,
per the addenda this implements, is still the correct, expected outcome
for a business with genuinely no web presence at all (no domain, no
directory listing Google can find, nothing).

COMPLIANCE NOTE: The original spec's draft notes mention falling back to
"a simple requests search ... and scrape the Facebook page['s] About
section". That conflicts with the spec's own compliance section, which
says:

    "Only use official APIs (Google Places, Facebook Graph) -- don't
    scrape Google/Facebook HTML directly."

We follow the compliance section, extended to search engines too: this
script ONLY calls official APIs (Facebook Graph API, Serper.dev's Search
API, Hunter.io's Domain Search API) and never fetches/parses HTML pages
directly -- including Google's own search result pages, which Serper.dev
gives us official, structured API access to instead of scraping. Every
source requires an API key/token you generate yourself; without one
configured, that lookup source is simply skipped, not replaced with
scraping.

Setup:
- Facebook: create a Meta developer app, generate a token with at least
  `pages_read_engagement` (or a basic App Access Token for public Page
  Search), put it in .env as FACEBOOK_ACCESS_TOKEN.
- Serper.dev: sign up free at serper.dev (2,500 searches, one-time free
  allotment), grab your API key from the dashboard, put it in .env as
  SERPER_API_KEY.
- Hunter.io: sign up free at hunter.io (25 searches/month on the free
  plan), grab an API key under API Keys, put it in .env as
  HUNTER_API_KEY.
Any or all of these can be left blank -- this script degrades gracefully
and simply marks leads `phone_only` for whichever source(s) aren't
configured, rather than erroring out.

NOTE ON REALISTIC OUTCOMES: even with all three configured, a business
with truly zero web presence -- no domain, no directory listing, no
mention Google has indexed -- will still correctly end up `phone_only`.
This step meaningfully raises the email-found rate for businesses that
have *some* footprint (an old/dormant domain, a directory listing, a
mention somewhere), not for every lead.
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

SERPER_API_KEY_ENV = "SERPER_API_KEY"
SERPER_SEARCH_URL = "https://google.serper.dev/search"

# Directory/aggregator/social domains that are never a business's own
# site -- skip these when picking a candidate domain out of search
# results, even if they rank first.
DIRECTORY_DOMAINS = {
    "yelp.com",
    "facebook.com",
    "yellowpages.com",
    "mapquest.com",
    "bbb.org",
    "google.com",
    "instagram.com",
    "tripadvisor.com",
    "twitter.com",
    "x.com",
    "linkedin.com",
    "nextdoor.com",
    "foursquare.com",
    "angi.com",
    "thumbtack.com",
    "manta.com",
}

HUNTER_API_KEY_ENV = "HUNTER_API_KEY"
HUNTER_DOMAIN_SEARCH_URL = "https://api.hunter.io/v2/domain-search"

REQUEST_DELAY_SECONDS = 1.0
# Both free tiers are rate-limited -- these delays are more conservative
# than either requires, just to be a good citizen on a shared free API.
SERPER_DELAY_SECONDS = 1.0
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


def _is_directory_domain(domain):
    domain = domain.lower()
    return any(domain == d or domain.endswith("." + d) for d in DIRECTORY_DOMAINS)


class SerperQuotaExhausted(Exception):
    """Raised when Serper.dev rejects the API key (401) or credits are exhausted (403)."""


def serper_find_domain(api_key, business_name, address):
    """
    Search Google (via Serper.dev's Search API -- an official API, not
    HTML scraping) for the business name + address, and return a
    plausible company domain from the first non-directory result in the
    top 3 organic results, or None if nothing plausible turns up.

    Raises SerperQuotaExhausted if the API key is rejected or the
    account's search credits are exhausted, so the caller can stop
    making further Serper calls for the rest of this run.
    """
    query = f"\"{business_name}\" {address}"
    try:
        resp = requests.post(
            SERPER_SEARCH_URL,
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": query},
            timeout=20,
        )
    except requests.RequestException as e:
        print(f"  Serper.dev request failed: {e}", file=sys.stderr)
        return None

    # 401 = bad/rejected API key, 403 = out of credits -- either way,
    # retrying on the next lead won't help; stop for the rest of this run.
    if resp.status_code in (401, 403):
        detail = f"HTTP {resp.status_code}"
        try:
            body = resp.json()
            detail = body.get("message") or body.get("error") or detail
        except ValueError:
            pass
        raise SerperQuotaExhausted(detail)

    if resp.status_code != 200:
        print(f"  Serper.dev error ({resp.status_code}): {resp.text}", file=sys.stderr)
        return None

    for result in (resp.json().get("organic") or [])[:3]:
        domain = extract_domain(result.get("link"))
        if domain and not _is_directory_domain(domain):
            return domain
    return None


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
    serper_key = os.environ.get(SERPER_API_KEY_ENV, "").strip()
    hunter_key = os.environ.get(HUNTER_API_KEY_ENV, "").strip()

    if not fb_token:
        print(f"NOTE: {FB_ACCESS_TOKEN_ENV} is not set -- skipping Facebook lookup.")
    if not serper_key:
        print(f"NOTE: {SERPER_API_KEY_ENV} is not set -- skipping Serper.dev domain discovery.")
    if not hunter_key:
        print(f"NOTE: {HUNTER_API_KEY_ENV} is not set -- skipping Hunter.io lookup.")
    if not fb_token and not serper_key and not hunter_key:
        print("No email lookup sources configured -- every lead below will be marked phone_only.")

    leads = read_leads()
    if not leads:
        print("No leads found in leads.xlsx -- run find_leads.py first.")
        return

    serper_quota_hit = False
    hunter_quota_hit = False
    serper_calls_used = 0
    updated = 0

    for lead in leads:
        if lead["email"] or lead["status"] not in (STATUS_NEW, ""):
            continue  # already has an email, or already processed

        email = None
        website = None
        email_source = ""
        domain_source = None  # "facebook" or "serper" -- which step found the domain

        # 1. Facebook: may directly yield an email, or a domain via `website`.
        if fb_token:
            print(f"Looking up Facebook page for: {lead['name']} ...")
            # Best-effort city hint for disambiguating search results --
            # leads.xlsx stores a full address, not a separate city column.
            fb_result = find_facebook_contact(fb_token, lead["name"], lead["address"])
            time.sleep(REQUEST_DELAY_SECONDS)
            email, website = fb_result["email"], fb_result["website"]
            if email:
                email_source = "facebook"

        domain = extract_domain(website)
        if domain:
            domain_source = "facebook"

        # 2. Serper.dev: only if we still have no domain to search.
        if not email and not domain and serper_key and not serper_quota_hit:
            print(f"  no domain yet -- trying Serper.dev search for {lead['name']} ...")
            try:
                domain = serper_find_domain(serper_key, lead["name"], lead["address"])
            except SerperQuotaExhausted as e:
                serper_quota_hit = True
                print(
                    f"  Serper.dev quota/auth issue -- skipping remaining Serper "
                    f"lookups this run ({e}).",
                    file=sys.stderr,
                )
            else:
                serper_calls_used += 1
                print(f"  (Serper.dev searches used this run: {serper_calls_used})")
                if domain:
                    domain_source = "serper"
                    print(f"  found candidate domain via Serper.dev: {domain}")
                else:
                    print("  no plausible domain found via Serper.dev search")
            time.sleep(SERPER_DELAY_SECONDS)

        # 3. Hunter.io: only if some step above found a domain but no email yet.
        if not email and domain and hunter_key and not hunter_quota_hit:
            print(f"  trying Hunter.io for domain {domain} ...")
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
                    # Distinguish a Facebook-sourced domain from a
                    # Serper-discovered one, per the addendum.
                    email_source = "hunter" if domain_source == "facebook" else "serper+hunter"

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
    if serper_key:
        print(f"Serper.dev searches used this run: {serper_calls_used}")


if __name__ == "__main__":
    run()
