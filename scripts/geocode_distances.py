#!/usr/bin/env python3
r"""
geocode_distances.py — Backfill `distance_miles` on every active_bids.json
entry where it's missing.

CCF office: 3308 Chancellor Lane, Monroe, NC 28110  (≈ 34.9854 N, -80.4203 W)

Two-tier geocoding:
  1. Local hardcoded city table (covers ~95% of our SE US pipeline — instant)
  2. Nominatim (OpenStreetMap) fallback for unknown cities — rate-limited
     1 req/sec per their ToS, results cached forever

Cache: data/config/city_geocode_cache.json — written every run.

Usage:
  python scripts/geocode_distances.py              # backfill + save
  python scripts/geocode_distances.py --dry-run    # preview, no write
  python scripts/geocode_distances.py --refresh    # re-geocode even cached
"""
from __future__ import annotations
import argparse, json, math, re, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIDS = ROOT / "data" / "memory" / "active_bids.json"
CACHE = ROOT / "data" / "config" / "city_geocode_cache.json"
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

CCF = (34.9854, -80.4203)  # Monroe NC

# Cities we actually see in the pipeline (Eastern US). Verified coordinates.
CITIES = {
    # NC
    ("monroe", "NC"):           (34.9854, -80.4203),
    ("charlotte", "NC"):        (35.2271, -80.8431),
    ("greensboro", "NC"):       (36.0726, -79.7920),
    ("raleigh", "NC"):          (35.7796, -78.6382),
    ("wilmington", "NC"):       (34.2257, -77.9447),
    ("durham", "NC"):           (35.9940, -78.8986),
    ("winston-salem", "NC"):    (36.0999, -80.2442),
    ("winston salem", "NC"):    (36.0999, -80.2442),
    ("salem", "NC"):            (36.0999, -80.2442),  # = Winston-Salem area
    ("fayetteville", "NC"):     (35.0527, -78.8784),
    ("mebane", "NC"):           (36.0959, -79.2670),
    ("belmont", "NC"):          (35.2429, -81.0376),
    ("sanford", "NC"):          (35.4799, -79.1803),
    ("knightdale", "NC"):       (35.7882, -78.4805),
    ("lincolnton", "NC"):       (35.4732, -81.2543),
    ("jacksonville", "NC"):     (34.7541, -77.4302),
    ("roxboro", "NC"):          (36.3935, -78.9831),
    ("angier", "NC"):           (35.5074, -78.7378),
    ("clinton", "NC"):          (35.0010, -78.3236),
    ("clayton", "NC"):          (35.6507, -78.4564),
    ("dallas", "NC"):           (35.3148, -81.1762),
    ("oak ridge", "NC"):        (36.1740, -79.9764),
    ("carthage", "NC"):         (35.3457, -79.4170),
    ("ft. bragg", "NC"):        (35.1395, -78.9978),
    ("fort bragg", "NC"):       (35.1395, -78.9978),
    ("randleman", "NC"):        (35.8268, -79.8086),
    ("leland", "NC"):           (34.2562, -78.0445),
    ("matthews", "NC"):         (35.1168, -80.7237),
    ("fuquay-varina", "NC"):    (35.5843, -78.8000),
    ("fuquay varina", "NC"):    (35.5843, -78.8000),
    ("havelock", "NC"):         (34.8791, -76.9013),
    ("elizabeth city", "NC"):   (36.2946, -76.2511),
    ("huntersville", "NC"):     (35.4107, -80.8428),
    ("concord", "NC"):          (35.4088, -80.5795),
    ("hickory", "NC"):          (35.7344, -81.3445),
    ("rock hill", "SC"):        (34.9249, -81.0251),
    # SC
    ("lexington", "SC"):        (33.9815, -81.2362),
    ("conway", "SC"):           (33.8360, -79.0478),
    ("charleston", "SC"):       (32.7765, -79.9311),
    ("north charleston", "SC"): (32.8546, -79.9748),
    ("little river", "SC"):     (33.8771, -78.6378),
    ("west columbia", "SC"):    (33.9935, -81.0739),
    ("hardeeville", "SC"):      (32.2871, -81.0801),
    # VA
    ("quinton", "VA"):          (37.5343, -77.1689),
    ("dinwiddie", "VA"):        (37.0763, -77.5874),
    ("petersburg", "VA"):       (37.2279, -77.4019),
    ("chester", "VA"):          (37.3568, -77.4419),
    ("chesterfield", "VA"):     (37.3719, -77.5197),
    ("vienna", "VA"):           (38.9012, -77.2653),
    ("hampton", "VA"):          (37.0299, -76.3452),
    ("roanoke", "VA"):          (37.2710, -79.9414),
    ("aylett", "VA"):           (37.7868, -77.1497),
    ("mclean", "VA"):           (38.9342, -77.1775),
    ("woodbridge", "VA"):       (38.6582, -77.2497),
    # KY
    ("bowling green", "KY"):    (36.9685, -86.4808),
    ("mount sterling", "KY"):   (38.0567, -83.9433),
    # TN
    ("maryville", "TN"):        (35.7565, -83.9705),
    ("elizabethton", "TN"):     (36.3487, -82.2107),
    ("ooltewah", "TN"):         (35.0807, -85.0658),
    # GA / FL / etc.
    ("tallahassee", "FL"):      (30.4383, -84.2807),
    ("atlanta", "GA"):          (33.7490, -84.3880),
    ("kennesaw", "GA"):         (34.0234, -84.6155),
    ("jefferson", "GA"):        (34.1170, -83.5752),
    ("hattiesburg", "MS"):      (31.3271, -89.2903),
    ("herriman", "UT"):         (40.5141, -112.0330),
    ("salt lake city", "UT"):   (40.7608, -111.8910),
    ("denham springs", "LA"):   (30.4830, -90.9559),
}
# State name aliases → 2-letter
ST = {"north carolina":"NC","south carolina":"SC","virginia":"VA","west virginia":"WV",
      "georgia":"GA","tennessee":"TN","kentucky":"KY","alabama":"AL","florida":"FL",
      "maryland":"MD","ohio":"OH","texas":"TX","new york":"NY","mississippi":"MS",
      "utah":"UT","louisiana":"LA"}


def norm_state(s):
    s = (s or "").strip()
    return ST.get(s.lower(), (s[:2].upper() if s else ""))


def haversine(lat1, lon1, lat2, lon2):
    R = 3958.8  # miles
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return round(R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a)), 1)


def load_cache():
    if CACHE.exists():
        try: return json.loads(CACHE.read_text(encoding="utf-8"))
        except Exception: pass
    return {}


def save_cache(c):
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(c, indent=2, sort_keys=True), encoding="utf-8")


def geocode_nominatim(city, st_code):
    """Last-resort lookup against OpenStreetMap. Rate-limited 1/sec per ToS."""
    try:
        from urllib.request import Request, urlopen
        from urllib.parse import quote
        q = quote(f"{city}, {st_code}, USA")
        req = Request(
            f"https://nominatim.openstreetmap.org/search?q={q}&format=json&limit=1",
            headers={"User-Agent": "CCF-Carol-Estimating/1.0 (estimates@carolinacommercialfinishes.com)"},
        )
        with urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
        time.sleep(1.05)  # Nominatim ToS: 1 req/sec
        if data:
            return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception as e:
        print(f"  [nominatim err] {city}, {st_code}: {e}")
    return None


def coords_for(city, st_code, cache, refresh=False):
    # Require a real city — state-only lookups return the state centroid
    # which is a misleading "average distance" (e.g. 91mi to NC's middle)
    # that has nothing to do with the actual project location.
    if not city or len(city) < 3:
        return None
    key = f"{city.strip().lower()}|{st_code}"
    if not refresh and key in cache:
        v = cache[key]
        return tuple(v) if v else None
    # Local table first
    local = CITIES.get((city.strip().lower(), st_code))
    if local:
        cache[key] = list(local)
        return local
    # Fallback to Nominatim
    print(f"  Nominatim lookup: {city}, {st_code}")
    pt = geocode_nominatim(city, st_code)
    cache[key] = list(pt) if pt else None
    return pt


# Known entities whose city isn't in the row OR the name (resolved by hand).
KNOWN_ENTITY = {
    "winthrop": ("rock hill", "SC"),                 # Winthrop University
    "wilson county ems": ("wilson", "NC"),
    "women with children recovery": ("greensboro", "NC"),  # Guilford County facility
    "guilford county": ("greensboro", "NC"),
    "catoosa county": ("ringgold", "GA"),
    "bertie county": ("windsor", "NC"),
    "craven county": ("new bern", "NC"),
    "roseboro": ("roseboro", "NC"),
    "north wake landfill": ("raleigh", "NC"),
    "sosc buck hall": ("brevard", "NC"),
}
_NAME_CS = re.compile(r'[-–,]\s*([A-Za-z][A-Za-z .]{2,30}),\s*([A-Z]{2})\b')
def parse_city_state(name):
    """Pull 'City, ST' out of a project name when the row has no city field."""
    n = name or ""
    low = n.lower()
    for key, cs in KNOWN_ENTITY.items():
        if key in low:
            return cs
    ms = _NAME_CS.findall(n)
    if ms:
        city, st = ms[-1]                            # last 'City, ST' wins (most specific)
        city = re.sub(r'#?\d+|store|remodel|reno(vation)?|new', '', city, flags=re.I).strip()
        if len(city) >= 3:
            return (city.lower(), st)
    return (None, None)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--refresh", action="store_true",
                    help="Re-geocode + recompute even if cached")
    args = ap.parse_args()

    bids = json.load(open(BIDS, encoding="utf-8"))
    cache = load_cache()

    updated = unknown = 0
    sample = []
    for b in bids:
        if not args.refresh and isinstance(b.get("distance_miles"), (int, float)):
            continue
        city = (b.get("city") or "").strip()
        st_code = norm_state(b.get("state") or "")
        if not city or len(city) < 3:   # row has no city — parse it from the project name
            pc, ps = parse_city_state(b.get("project_name") or b.get("name") or "")
            if pc:
                city, st_code = pc, (ps or st_code)
        pt = coords_for(city, st_code, cache)
        if pt:
            d = haversine(CCF[0], CCF[1], pt[0], pt[1])
            b["distance_miles"] = d
            updated += 1
            if len(sample) < 8:
                sample.append((b.get("project_name", "")[:38], city, st_code, d))
        else:
            unknown += 1

    save_cache(cache)
    print(f"[geocode] updated {updated} bids · {unknown} still unknown (no city, or geocoder miss)")
    for pn, c, s, d in sample:
        print(f"  → {pn:<38} {c}, {s} → {d}mi")

    if args.dry_run:
        print("[geocode] dry-run: not writing active_bids.json")
        return
    if updated:
        BIDS.write_text(json.dumps(bids, indent=2), encoding="utf-8")
        print(f"[geocode] wrote {BIDS}")


if __name__ == "__main__":
    main()
