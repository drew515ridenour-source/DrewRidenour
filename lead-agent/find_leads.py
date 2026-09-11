"""
find_leads.py

Step 1 of the pipeline: find local businesses in a given (category, city)
that do NOT currently have a website, using the Google Places API (New).

Usage:
    python find_leads.py

Edit SEARCHES below (or import find_leads and call run(searches)) to change
which categories/cities are searched.

COMPLIANCE NOTE: this script only talks to Google's official Places API
endpoints (Text Search + Place Details) over HTTPS with an API key. It does
not scrape Google's HTML search results, which would violate Google's
Terms of Service.
"""

import sys
import time

import requests

from common import (
    LEADS_FIELDS,
    dedup_key,
    read_leads,
    require_env,
    write_leads,
)

PLACES_API_KEY_ENV = "GOOGLE_PLACES_API_KEY"

TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
PLACE_DETAILS_URL = "https://places.googleapis.com/v1/places/{place_id}"

# Fields we need out of Text Search: just enough to identify the place and
# page through results. Full details (phone/website/maps link) come from
# the separate Place Details call per spec.
TEXT_SEARCH_FIELD_MASK = "places.id,places.displayName,nextPageToken"

DETAILS_FIELD_MASK = "displayName,formattedAddress,nationalPhoneNumber,websiteUri,googleMapsUri"

# Small delay between Place Details calls so we stay well under Google's
# rate limits, not to evade anything -- just good citizenship on a shared API.
DETAILS_DELAY_SECONDS = 0.2

# (category, "City, ST") pairs to search. Iowa City first per the business
# context in the spec; add more rows as you expand to other cities/categories.
SEARCHES = [
    ("auto repair", "Iowa City, IA"),
    ("hair salon", "Iowa City, IA"),
]

MAX_PAGES_PER_SEARCH = 3  # Text Search (New) returns up to 20 results/page


def text_search(api_key, query, page_token=None):
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": TEXT_SEARCH_FIELD_MASK,
    }
    body = {"textQuery": query}
    if page_token:
        body["pageToken"] = page_token
    resp = requests.post(TEXT_SEARCH_URL, headers=headers, json=body, timeout=30)
    if resp.status_code != 200:
        print(f"  Text Search error ({resp.status_code}): {resp.text}", file=sys.stderr)
        return [], None
    data = resp.json()
    return data.get("places", []), data.get("nextPageToken")


def place_details(api_key, place_id):
    headers = {
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": DETAILS_FIELD_MASK,
    }
    url = PLACE_DETAILS_URL.format(place_id=place_id)
    resp = requests.get(url, headers=headers, timeout=30)
    if resp.status_code != 200:
        print(f"  Place Details error ({resp.status_code}): {resp.text}", file=sys.stderr)
        return None
    return resp.json()


def find_leads_for(api_key, category, city):
    """Yield lead dicts for one (category, city) search that lack a website."""
    query = f"{category} in {city}"
    page_token = None
    pages_fetched = 0

    while True:
        places, next_token = text_search(api_key, query, page_token)
        pages_fetched += 1

        for place in places:
            place_id = place.get("id")
            if not place_id:
                continue
            details = place_details(api_key, place_id)
            time.sleep(DETAILS_DELAY_SECONDS)
            if not details:
                continue

            website = (details.get("websiteUri") or "").strip()
            if website:
                continue  # has a website already -- not a lead

            name = (details.get("displayName", {}) or {}).get("text", "").strip()
            if not name:
                continue

            yield {
                "name": name,
                "address": details.get("formattedAddress", "").strip(),
                "phone": details.get("nationalPhoneNumber", "").strip(),
                "category": category,
                "google_maps_url": details.get("googleMapsUri", "").strip(),
                "facebook_url": "",
                "email": "",
                "draft_email": "",
                "status": "new",
            }

        if not next_token or pages_fetched >= MAX_PAGES_PER_SEARCH:
            break
        page_token = next_token
        # Google requires a short delay before a page token becomes valid.
        time.sleep(2)


def run(searches):
    import os

    require_env(PLACES_API_KEY_ENV)
    api_key = os.environ[PLACES_API_KEY_ENV]

    existing = read_leads()
    seen = {dedup_key(row["name"], row["phone"]) for row in existing}

    added = 0
    for category, city in searches:
        print(f"Searching: {category!r} in {city!r} ...")
        for lead in find_leads_for(api_key, category, city):
            key = dedup_key(lead["name"], lead["phone"])
            if key in seen:
                continue
            seen.add(key)
            existing.append(lead)
            added += 1
            print(f"  + {lead['name']} ({lead['phone'] or 'no phone'}) -- no website found")

    write_leads(existing)
    print(f"\nDone. Added {added} new lead(s). Total leads in leads.csv: {len(existing)}")


if __name__ == "__main__":
    run(SEARCHES)
