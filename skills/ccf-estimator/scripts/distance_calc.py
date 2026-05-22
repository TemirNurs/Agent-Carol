#!/usr/bin/env python3
"""
CCF Distance Calculator
Calculates straight-line distance from CCF office to project locations.
Uses a built-in NC/SC/VA/GA/TN city coordinate database — no API calls needed.

CCF Office: 3308 Chancellor Ln, Monroe, NC 28110
Coordinates: 34.9854, -80.5495

Usage:
  python distance_calc.py --city "Charlotte" --state "NC"
  python distance_calc.py --location "Charlotte, North Carolina"
  python distance_calc.py --sort-bids bids.json
"""

import argparse
import json
import math
import re
import sys
from pathlib import Path

# CCF Office coordinates
CCF_LAT = 34.9854
CCF_LON = -80.5495

# City coordinates database — NC, SC, VA, GA, TN and surrounding states
# lat, lon for common project cities
CITY_COORDS = {
    # North Carolina
    "charlotte, nc": (35.2271, -80.8431),
    "charlotte, north carolina": (35.2271, -80.8431),
    "monroe, nc": (34.9854, -80.5495),
    "monroe, north carolina": (34.9854, -80.5495),
    "raleigh, nc": (35.7796, -78.6382),
    "raleigh, north carolina": (35.7796, -78.6382),
    "durham, nc": (35.9940, -78.8986),
    "durham, north carolina": (35.9940, -78.8986),
    "greensboro, nc": (36.0726, -79.7920),
    "greensboro, north carolina": (36.0726, -79.7920),
    "winston-salem, nc": (36.0999, -80.2442),
    "winston-salem, north carolina": (36.0999, -80.2442),
    "winston salem, nc": (36.0999, -80.2442),
    "winston salem, north carolina": (36.0999, -80.2442),
    "fayetteville, nc": (35.0527, -78.8784),
    "fayetteville, north carolina": (35.0527, -78.8784),
    "wilmington, nc": (34.2257, -77.9447),
    "wilmington, north carolina": (34.2257, -77.9447),
    "asheville, nc": (35.5951, -82.5515),
    "asheville, north carolina": (35.5951, -82.5515),
    "hickory, nc": (35.7332, -81.3412),
    "hickory, north carolina": (35.7332, -81.3412),
    "salisbury, nc": (35.6710, -80.4742),
    "salisbury, north carolina": (35.6710, -80.4742),
    "concord, nc": (35.4088, -80.5795),
    "concord, north carolina": (35.4088, -80.5795),
    "huntersville, nc": (35.4107, -80.8429),
    "huntersville, north carolina": (35.4107, -80.8429),
    "pineville, nc": (35.0832, -80.8923),
    "pineville, north carolina": (35.0832, -80.8923),
    "asheboro, nc": (35.7079, -79.8136),
    "asheboro, north carolina": (35.7079, -79.8136),
    "graham, nc": (36.0688, -79.4006),
    "graham, north carolina": (36.0688, -79.4006),
    "louisburg, nc": (36.0990, -78.3011),
    "louisburg, north carolina": (36.0990, -78.3011),
    "pope field, nc": (35.1709, -79.0145),
    "pope field, north carolina": (35.1709, -79.0145),
    "fort bragg, nc": (35.1390, -79.0064),
    "fort bragg, north carolina": (35.1390, -79.0064),
    "elizabeth city, nc": (36.2946, -76.2510),
    "elizabeth city, north carolina": (36.2946, -76.2510),
    "chapel hill, nc": (35.9132, -79.0558),
    "chapel hill, north carolina": (35.9132, -79.0558),
    "wilson, nc": (35.7212, -77.9155),
    "wilson, north carolina": (35.7212, -77.9155),
    "butner, nc": (36.1321, -78.7567),
    "butner, north carolina": (36.1321, -78.7567),
    "stem, nc": (36.1987, -78.7172),
    "stem, north carolina": (36.1987, -78.7172),
    "wake forest, nc": (35.9799, -78.5097),
    "wake forest, north carolina": (35.9799, -78.5097),
    "clemmons, nc": (36.0213, -80.3820),
    "clemmons, north carolina": (36.0213, -80.3820),
    "troy, nc": (35.3569, -79.8942),
    "troy, north carolina": (35.3569, -79.8942),
    "holly ridge, nc": (34.4944, -77.5536),
    "holly ridge, north carolina": (34.4944, -77.5536),
    "havelock, nc": (34.8791, -76.9014),
    "havelock, north carolina": (34.8791, -76.9014),
    "nags head, nc": (35.9574, -75.6241),
    "nags head, north carolina": (35.9574, -75.6241),
    "wendell, nc": (35.7810, -78.3697),
    "wendell, north carolina": (35.7810, -78.3697),
    "wesley chapel, nc": (35.0071, -80.6748),
    "wesley chapel, north carolina": (35.0071, -80.6748),
    "eden, nc": (36.4888, -79.7667),
    "eden, north carolina": (36.4888, -79.7667),
    "new london, nc": (35.3802, -80.2131),
    "new london, north carolina": (35.3802, -80.2131),
    "camp lejeune, nc": (34.6200, -77.3900),
    "camp lejeune, north carolina": (34.6200, -77.3900),
    "hillsborough, nc": (36.0754, -79.0998),
    "hillsborough, north carolina": (36.0754, -79.0998),
    "greenville, nc": (35.6127, -77.3664),
    "greenville, north carolina": (35.6127, -77.3664),
    "weaverville, nc": (35.6971, -82.5607),
    "weaverville, north carolina": (35.6971, -82.5607),
    "mooresville, nc": (35.5849, -80.8101),
    "mooresville, north carolina": (35.5849, -80.8101),
    # South Carolina
    "columbia, sc": (34.0007, -81.0348),
    "columbia, south carolina": (34.0007, -81.0348),
    "florence, sc": (34.1954, -79.7626),
    "florence, south carolina": (34.1954, -79.7626),
    "charleston, sc": (32.7765, -79.9311),
    "charleston, south carolina": (32.7765, -79.9311),
    "greenville, sc": (34.8526, -82.3940),
    "greenville, south carolina": (34.8526, -82.3940),
    "mount pleasant, sc": (32.7941, -79.8626),
    "mount pleasant, south carolina": (32.7941, -79.8626),
    "summerville, sc": (33.0185, -80.1756),
    "summerville, south carolina": (33.0185, -80.1756),
    "conway, sc": (33.8360, -79.0478),
    "conway, south carolina": (33.8360, -79.0478),
    "orangeburg, sc": (33.4918, -80.8556),
    "orangeburg, south carolina": (33.4918, -80.8556),
    "ft. mills, sc": (34.9924, -80.9254),
    "ft. mills, south carolina": (34.9924, -80.9254),
    "fort mill, sc": (34.9924, -80.9254),
    "fort mill, south carolina": (34.9924, -80.9254),
    # Virginia
    "richmond, va": (37.5407, -77.4360),
    "richmond, virginia": (37.5407, -77.4360),
    "midlothian, va": (37.5021, -77.6490),
    "midlothian, virginia": (37.5021, -77.6490),
    "chantilly, va": (38.8943, -77.4311),
    "chantilly, virginia": (38.8943, -77.4311),
    # Georgia
    "augusta, ga": (33.4735, -81.9748),
    "augusta, georgia": (33.4735, -81.9748),
    "martinez, ga": (33.5174, -82.0757),
    "martinez, georgia": (33.5174, -82.0757),
    # Tennessee
    "chattanooga, tn": (35.0456, -85.3097),
    "chattanooga, tennessee": (35.0456, -85.3097),
    "morristown, tn": (36.2140, -83.2949),
    "morristown, tennessee": (36.2140, -83.2949),
    "cleveland, tn": (35.1595, -84.8766),
    "cleveland, tennessee": (35.1595, -84.8766),
    "ooltewah, tn": (35.0784, -85.0588),
    "ooltewah, tennessee": (35.0784, -85.0588),
    "new market, tn": (36.0987, -83.5527),
    "new market, tennessee": (36.0987, -83.5527),
    "kimberlin heights, tn": (35.8954, -83.8585),
    "kimberlin heights, tennessee": (35.8954, -83.8585),
    "knoxville, tn": (35.9606, -83.9207),
    "knoxville, tennessee": (35.9606, -83.9207),
    # Additional NC
    "kinston, nc": (35.2627, -77.5816),
    "kinston, north carolina": (35.2627, -77.5816),
    "cary, nc": (35.7915, -78.7811),
    "cary, north carolina": (35.7915, -78.7811),
    "morrisville, nc": (35.8235, -78.8256),
    "morrisville, north carolina": (35.8235, -78.8256),
    "morehead city, nc": (34.7230, -76.7262),
    "morehead city, north carolina": (34.7230, -76.7262),
    # Additional SC
    "spartanburg, sc": (34.9496, -81.9320),
    "spartanburg, south carolina": (34.9496, -81.9320),
    "rock hill, sc": (34.9249, -81.0251),
    "rock hill, south carolina": (34.9249, -81.0251),
    "pendleton, sc": (34.6518, -82.7838),
    "pendleton, south carolina": (34.6518, -82.7838),
    "longs, sc": (33.9141, -78.7203),
    "longs, south carolina": (33.9141, -78.7203),
    "myrtle beach, sc": (33.6891, -78.8867),
    "myrtle beach, south carolina": (33.6891, -78.8867),
    # Additional VA
    "tysons, va": (38.9187, -77.2311),
    "tysons, virginia": (38.9187, -77.2311),
    "dumfries, va": (38.5679, -77.3286),
    "dumfries, virginia": (38.5679, -77.3286),
    # Additional GA
    "ringgold, ga": (34.9162, -85.1091),
    "ringgold, georgia": (34.9162, -85.1091),
}


def haversine_miles(lat1, lon1, lat2, lon2):
    """Calculate straight-line distance in miles between two coordinates."""
    R = 3959  # Earth radius in miles
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    c = 2 * math.asin(math.sqrt(a))
    return round(R * c, 1)


def get_distance(city, state=None):
    """Get distance from CCF office to a city.
    Returns distance in miles, or None if city not found."""
    if not city:
        return None

    # Normalize
    location = city.strip().lower()
    if state:
        state_clean = state.strip().lower()
        # Try full: "charlotte, north carolina"
        key1 = f"{location}, {state_clean}"
        if key1 in CITY_COORDS:
            lat, lon = CITY_COORDS[key1]
            return haversine_miles(CCF_LAT, CCF_LON, lat, lon)

        # Try abbreviation: "charlotte, nc"
        state_abbrev = {"north carolina": "nc", "south carolina": "sc", "virginia": "va",
                        "georgia": "ga", "tennessee": "tn"}.get(state_clean, state_clean)
        key2 = f"{location}, {state_abbrev}"
        if key2 in CITY_COORDS:
            lat, lon = CITY_COORDS[key2]
            return haversine_miles(CCF_LAT, CCF_LON, lat, lon)

    # Try just the location string as-is
    if location in CITY_COORDS:
        lat, lon = CITY_COORDS[location]
        return haversine_miles(CCF_LAT, CCF_LON, lat, lon)

    # Try parsing "City, State" from location
    match = re.match(r'(.+?),\s*(.+)', location)
    if match:
        c, s = match.group(1).strip(), match.group(2).strip()
        key = f"{c}, {s}"
        if key in CITY_COORDS:
            lat, lon = CITY_COORDS[key]
            return haversine_miles(CCF_LAT, CCF_LON, lat, lon)

    return None


def add_distance_to_bids(bids):
    """Add distance_miles field to each bid and sort by distance (closest first)."""
    for bid in bids:
        city = bid.get("city", "")
        state = bid.get("state", "")
        location = bid.get("location", "")

        dist = None
        if city and state:
            dist = get_distance(city, state)
        elif location:
            dist = get_distance(location)

        bid["distance_miles"] = dist

    # Sort: bids with distance first (closest to farthest), then unknown distance at end
    bids.sort(key=lambda b: b.get("distance_miles") if b.get("distance_miles") is not None else 99999)

    return bids


def main():
    parser = argparse.ArgumentParser(description="CCF Distance Calculator")
    parser.add_argument("--city", default=None)
    parser.add_argument("--state", default=None)
    parser.add_argument("--location", default=None)
    parser.add_argument("--sort-bids", default=None, help="JSON file of bids to sort by distance")
    args = parser.parse_args()

    if args.sort_bids:
        with open(args.sort_bids) as f:
            data = json.load(f)
        bids = data if isinstance(data, list) else data.get("bids", [])
        sorted_bids = add_distance_to_bids(bids)
        print(json.dumps(sorted_bids, indent=2))
    elif args.location:
        dist = get_distance(args.location)
        print(json.dumps({"location": args.location, "distance_miles": dist}))
    elif args.city:
        dist = get_distance(args.city, args.state)
        print(json.dumps({"city": args.city, "state": args.state, "distance_miles": dist}))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
