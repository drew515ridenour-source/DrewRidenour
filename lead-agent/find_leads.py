"""
find_leads.py

Step 1 of the pipeline: find local businesses in a configured search area
that do NOT currently have a website, using the Google Places API (New).

Usage:
    python find_leads.py

Edit CATEGORIES in config.py to change what kinds of businesses are
searched. Edit SEARCH_CENTER_LAT / SEARCH_CENTER_LNG / SEARCH_RADIUS_METERS
in .env to change *where* -- no code changes needed to move to a new city
or widen/narrow the search radius. See README.md for how to find lat/lng
for a new city.

Each lead found is written to leads.xlsx immediately (not batched at the
end), so an interrupted run doesn't lose progress.
"""

import sys
import time

import requests

from common import append_lead_row, dedup_key, existing_dedup_keys, require_env
from config import (
    CATEGORIES,
    SEARCH_CENTER_LAT,
    SEARCH_CENTER_LNG,
    SEARCH_LOCATION_MODE,
    SEARCH_RADIUS_METERS,
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

MAX_PAGES_PER_SEARCH = 3  # Text Search (New) returns up to 20 results/page


def _location_circle():
    return {
        "circle": {
            "center": {"latitude": SEARCH_CENTER_LAT, "longitude": SEARCH_CENTER_LNG},
            "radius": SEARCH_RADIUS_METERS,
        }
    }


def text_search(api_key, query, page_token=None):
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": TEXT_SEARCH_FIELD_MASK,
    }
    body = {"textQuery": query}

    # locationRestriction is a hard cutoff (results outside the circle are
    # excluded); locationBias is a soft preference. Configurable via
    # SEARCH_LOCATION_MODE in .env -- see config.py.
    if SEARCH_LOCATION_MODE == "restriction":
        body["locationRestriction"] = _location_circle()
    else:
        body["locationBias"] = _location_circle()

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


def find_leads_for(api_key, category):
    """Yield lead dicts for one category search that lack a website."""
    page_token = None
    pages_fetched = 0

    while True:
        places, next_token = text_search(api_key, category, page_token)
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
                "email_source": "",
                "draft_email": "",
                "status": "new",
            }

        if not next_token or pages_fetched >= MAX_PAGES_PER_SEARCH:
            break
        page_token = next_token
        # Google requires a short delay before a page token becomes valid.
        time.sleep(2)


def run(categories):
    import os

    require_env(PLACES_API_KEY_ENV)
    api_key = os.environ[PLACES_API_KEY_ENV]

    seen = existing_dedup_keys()
    added = 0

    print(
        f"Searching within {SEARCH_RADIUS_METERS:.0f}m of "
        f"({SEARCH_CENTER_LAT}, {SEARCH_CENTER_LNG}) [{SEARCH_LOCATION_MODE}] ...\n"
    )

    for category in categories:
        print(f"Searching category: {category!r} ...")
        for lead in find_leads_for(api_key, category):
            key = dedup_key(lead["name"], lead["phone"])
            if key in seen:
                continue
            seen.add(key)
            # Write immediately -- a long run that gets interrupted partway
            # through shouldn't lose the leads already found.
            append_lead_row(lead)
            added += 1
            print(f"  + {lead['name']} ({lead['phone'] or 'no phone'}) -- no website found")

    print(f"\nDone. Added {added} new lead(s) to leads.xlsx.")


if __name__ == "__main__":
    run(CATEGORIES)
