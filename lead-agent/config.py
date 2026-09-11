"""
Search configuration for find_leads.py.

- CATEGORIES: what kinds of businesses to search for. Edit this list
  directly -- it's code, not secret/environment-specific, so it belongs
  in version control.
- Geographic search area (center point + radius): configured via .env
  instead, so you can move to a new city or widen/narrow the radius
  without touching any code. See .env.example and README.md.
"""

import os

CATEGORIES = [
    "auto repair",
    "hair salon",
    "tire shop",
    "dentist",
    "restaurant",
]

# Defaults below = Iowa City, IA, ~5 mile radius. Override in .env.
SEARCH_CENTER_LAT = float(os.environ.get("SEARCH_CENTER_LAT", "41.6611"))
SEARCH_CENTER_LNG = float(os.environ.get("SEARCH_CENTER_LNG", "-91.5302"))
SEARCH_RADIUS_METERS = float(os.environ.get("SEARCH_RADIUS_METERS", "8000"))

# "bias" (default): Google may still surface a strong match outside the
#   circle -- a soft preference, generally gives more/better results.
# "restriction": a hard cutoff -- results outside the circle are excluded
#   entirely, even if otherwise relevant.
SEARCH_LOCATION_MODE = os.environ.get("SEARCH_LOCATION_MODE", "bias").strip().lower()
