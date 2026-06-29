#!/usr/bin/env python3
"""
ConstructConnect Bid Center Inbox Scraper
Logs into CC, scrapes the Bid Center inbox, and merges results into active_bids.json.

Usage:
  python scrape_cc_inbox.py              # Scrape and save to active_bids.json
  python scrape_cc_inbox.py --dry-run    # Print results without saving
  python scrape_cc_inbox.py --pages 3    # Limit to N pages (default: all up to 10)
"""

import asyncio
import argparse
import json
import math
import re
import sys
from datetime import datetime, date, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Paths
BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "data" / "config"
AUTH_FILE = CONFIG_DIR / "cc_auth.json"
BIDS_FILE = BASE_DIR / "data" / "memory" / "active_bids.json"

# CCF Office: 3308 Chancellor Ln, Monroe, NC 28110
CCF_LAT = 34.9854
CCF_LON = -80.5495

# City coordinates for distance calculation (NC, SC, VA, GA, TN)
CITY_COORDS = {
    "charlotte, nc": (35.2271, -80.8431),
    "monroe, nc": (34.9854, -80.5495),
    "raleigh, nc": (35.7796, -78.6382),
    "durham, nc": (35.9940, -78.8986),
    "greensboro, nc": (36.0726, -79.7920),
    "winston-salem, nc": (36.0999, -80.2442),
    "winston salem, nc": (36.0999, -80.2442),
    "fayetteville, nc": (35.0527, -78.8784),
    "wilmington, nc": (34.2257, -77.9447),
    "asheville, nc": (35.5951, -82.5515),
    "hickory, nc": (35.7332, -81.3412),
    "salisbury, nc": (35.6710, -80.4742),
    "concord, nc": (35.4088, -80.5795),
    "huntersville, nc": (35.4107, -80.8429),
    "pineville, nc": (35.0832, -80.8923),
    "asheboro, nc": (35.7079, -79.8136),
    "graham, nc": (36.0688, -79.4006),
    "louisburg, nc": (36.0990, -78.3011),
    "fort bragg, nc": (35.1390, -79.0064),
    "elizabeth city, nc": (36.2946, -76.2510),
    "chapel hill, nc": (35.9132, -79.0558),
    "wilson, nc": (35.7212, -77.9155),
    "butner, nc": (36.1321, -78.7567),
    "stem, nc": (36.1987, -78.7172),
    "wake forest, nc": (35.9799, -78.5097),
    "clemmons, nc": (36.0213, -80.3820),
    "troy, nc": (35.3569, -79.8942),
    "holly ridge, nc": (34.4944, -77.5536),
    "havelock, nc": (34.8791, -76.9014),
    "nags head, nc": (35.9574, -75.6241),
    "wendell, nc": (35.7810, -78.3697),
    "wesley chapel, nc": (35.0071, -80.6748),
    "eden, nc": (36.4888, -79.7667),
    "new london, nc": (35.3802, -80.2131),
    "camp lejeune, nc": (34.6200, -77.3900),
    "hillsborough, nc": (36.0754, -79.0998),
    "greenville, nc": (35.6127, -77.3664),
    "weaverville, nc": (35.6971, -82.5607),
    "mooresville, nc": (35.5849, -80.8101),
    "gastonia, nc": (35.2621, -81.1873),
    "kannapolis, nc": (35.4874, -80.6217),
    "burlington, nc": (36.0957, -79.4378),
    "kernersville, nc": (36.1199, -80.0737),
    "indian trail, nc": (35.0768, -80.6692),
    "matthews, nc": (35.1168, -80.7237),
    "mint hill, nc": (35.1796, -80.6473),
    "lake norman of catawba, nc": (35.5849, -80.8901),
    # South Carolina
    "columbia, sc": (34.0007, -81.0348),
    "florence, sc": (34.1954, -79.7626),
    "charleston, sc": (32.7765, -79.9311),
    "greenville, sc": (34.8526, -82.3940),
    "spartanburg, sc": (34.9496, -81.9320),
    "mount pleasant, sc": (32.7941, -79.8626),
    "summerville, sc": (33.0185, -80.1756),
    "conway, sc": (33.8360, -79.0478),
    "orangeburg, sc": (33.4918, -80.8556),
    "ft. mills, sc": (34.9924, -80.9254),
    "fort mill, sc": (34.9924, -80.9254),
    "rock hill, sc": (34.9249, -81.0251),
    # Virginia
    "richmond, va": (37.5407, -77.4360),
    "midlothian, va": (37.5021, -77.6490),
    "chantilly, va": (38.8943, -77.4311),
    # Georgia
    "augusta, ga": (33.4735, -81.9748),
    "martinez, ga": (33.5174, -82.0757),
    # Tennessee
    "chattanooga, tn": (35.0456, -85.3097),
    "morristown, tn": (36.2140, -83.2949),
    "knoxville, tn": (35.9606, -83.9207),
    "cleveland, tn": (35.1595, -84.8766),
    # Other
    "myrtle beach, sc": (33.6891, -78.8867),
    "north myrtle beach, sc": (33.8160, -78.6800),
    "pendleton, sc": (34.6518, -82.7838),
    "richmond, va": (37.5407, -77.4360),
    "wake forest, nc": (35.9799, -78.5097),
    "ooltewah, tn": (35.0784, -85.0588),
    "kinston, nc": (35.2627, -77.5816),
    "kimberlin heights, tn": (35.8954, -83.8585),
    "cary, nc": (35.7915, -78.7811),
    "tysons, va": (38.9187, -77.2311),
    "morehead city, nc": (34.7230, -76.7262),
    "new market, tn": (36.0987, -83.5527),
    "morrisville, nc": (35.8235, -78.8256),
    "longs, sc": (33.9141, -78.7203),
    "ringgold, ga": (34.9162, -85.1091),
    "dumfries, va": (38.5679, -77.3286),
}


def haversine_miles(lat1, lon1, lat2, lon2):
    R = 3959
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return round(R * 2 * math.asin(math.sqrt(a)), 1)


_GEO_CACHE_FILE = BASE_DIR / "data" / "config" / "city_geocode_cache.json"
_GEO_CACHE = None


def _geocode_cache():
    """Accurate per-city coords maintained by the geocode_distances daemon job.
    Keyed 'city|STATE' (lowercase city, uppercase state) -> [lat, lon]."""
    global _GEO_CACHE
    if _GEO_CACHE is None:
        try:
            _GEO_CACHE = json.loads(_GEO_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            _GEO_CACHE = {}
    return _GEO_CACHE


def get_distance(city, state):
    if not city:
        return None
    city_clean = city.strip().lower()
    state_clean = state.strip().lower() if state else ""

    # Map full state names to abbreviations
    abbrevs = {
        "north carolina": "nc", "south carolina": "sc", "virginia": "va",
        "georgia": "ga", "tennessee": "tn",
    }
    state_abbr = abbrevs.get(state_clean, state_clean)

    # 1. Accurate geocode cache first (real coords) — covers cities not in the
    #    tiny static map, so we don't return None for e.g. Oak Ridge/Harrogate TN.
    hit = _geocode_cache().get(f"{city_clean}|{state_abbr.upper()}")
    if isinstance(hit, (list, tuple)) and len(hit) == 2:
        return haversine_miles(CCF_LAT, CCF_LON, hit[0], hit[1])

    key = f"{city_clean}, {state_abbr}"
    if key in CITY_COORDS:
        lat, lon = CITY_COORDS[key]
        return haversine_miles(CCF_LAT, CCF_LON, lat, lon)

    # Try full state name
    key2 = f"{city_clean}, {state_clean}"
    if key2 in CITY_COORDS:
        lat, lon = CITY_COORDS[key2]
        return haversine_miles(CCF_LAT, CCF_LON, lat, lon)

    return None


def parse_location(location_str):
    """Split 'Winston Salem, NC' into (city, state)."""
    if not location_str:
        return "", ""
    parts = location_str.rsplit(",", 1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return location_str.strip(), ""


STATE_ABBR_TO_FULL = {
    "NC": "North Carolina", "SC": "South Carolina", "VA": "Virginia",
    "GA": "Georgia", "TN": "Tennessee", "FL": "Florida", "AL": "Alabama",
    "MD": "Maryland", "DC": "District of Columbia", "WV": "West Virginia",
    "KY": "Kentucky", "OH": "Ohio", "PA": "Pennsylvania",
}


def parse_cc_date(date_str):
    """Convert 'Apr 7, 2026' to '4/7/2026' format."""
    if not date_str:
        return ""
    date_str = date_str.strip()
    # Already in M/D/YYYY format
    if re.match(r"^\d{1,2}/\d{1,2}/\d{4}$", date_str):
        return date_str
    # Try "Mon D, YYYY" format
    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            dt = datetime.strptime(date_str, fmt)
            return f"{dt.month}/{dt.day}/{dt.year}"
        except ValueError:
            continue
    return date_str


def clean_project_name(name):
    """Strip trade suffixes like ' - All Trades', ' - Main Trades'.
    Also dedup the 'X - Y - X - Y' double-concat pattern CC's inbox table
    produces (e.g. 'Food Lion 2118B - Dinwiddie - Food Lion 2118B - Dinwiddie')."""
    name = (name or "").strip()
    for suffix in [" - All Trades", " - Main Trades", " - All", " - Main"]:
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    name = name.strip()
    # Halve-and-compare: if first half == second half (with a "-" or "/" between),
    # keep only the first half.
    for sep in (" - ", " | ", " / "):
        if sep in name:
            mid = len(name) // 2
            # Find the separator instance closest to the middle
            best_idx = -1
            best_dist = 9e9
            i = 0
            while True:
                j = name.find(sep, i)
                if j < 0: break
                d = abs(j - mid)
                if d < best_dist:
                    best_dist = d
                    best_idx = j
                i = j + 1
            if best_idx > 0:
                left = name[:best_idx].strip()
                right = name[best_idx + len(sep):].strip()
                # Identical halves OR right starts with left
                if left and (left.lower() == right.lower()
                             or right.lower().startswith(left.lower())):
                    name = left
                    break
    return name.strip()


async def scrape_cc_inbox(max_pages=10):
    """Login to CC Bid Center and scrape the inbox table."""
    from playwright.async_api import async_playwright

    if not AUTH_FILE.exists():
        print(f"ERROR: No auth config at {AUTH_FILE}")
        return []

    config = json.load(open(AUTH_FILE))
    projects = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36",
            viewport={"width": 1600, "height": 900},
        )
        page = await context.new_page()

        try:
            # --- Login ---
            print("  Logging into ConstructConnect...")
            await page.goto("https://app.constructconnect.com", timeout=30000)
            await asyncio.sleep(3)

            email_inp = await page.query_selector("#email-input")
            if email_inp:
                await email_inp.fill(config["username"])
                pwd_inp = await page.query_selector("#password-input")
                if pwd_inp:
                    await pwd_inp.fill(config["password"])
                login_btn = await page.query_selector('button:has-text("Log In")')
                if login_btn:
                    await login_btn.click()
                    await asyncio.sleep(8)
            else:
                print("  WARNING: Login form not found. May already be logged in.")

            # Dismiss cookie banner
            cookie_btn = await page.query_selector('button:has-text("Accept and Continue")')
            if cookie_btn:
                await cookie_btn.click()
                await asyncio.sleep(1)

            # --- Navigate to Bid Center inbox ---
            print("  Opening Bid Center inbox...")
            await page.goto("https://app.constructconnect.com/bidcenter/tabs/inbox", timeout=30000)
            await asyncio.sleep(8)

            # Dismiss cookie banner again if needed
            cookie_btn = await page.query_selector('button:has-text("Accept and Continue")')
            if cookie_btn:
                await cookie_btn.click()
                await asyncio.sleep(1)

            # --- Scrape full inbox (no date filter -- get ALL bids) ---
            print("  Scraping full inbox (all dates)...")

            # Dismiss cookie banner again if it reappeared
            try:
                cookie_btn = await page.query_selector('button:has-text("Accept and Continue"):visible')
                if cookie_btn:
                    await cookie_btn.click()
                    await asyncio.sleep(1)
            except Exception:
                pass

            # Wait for table to load
            for _ in range(10):
                rows_check = await page.query_selector_all("table tbody tr")
                if len(rows_check) > 0:
                    break
                await asyncio.sleep(2)

            # --- Scrape table rows across all pages ---
            page_num = 0
            while page_num < max_pages:
                page_num += 1
                rows = await page.query_selector_all("table tbody tr")
                print(f"  Page {page_num}: {len(rows)} rows found")

                for row in rows:
                    cells = await row.query_selector_all("td")
                    if len(cells) < 6:
                        continue

                    texts = []
                    for cell in cells:
                        t = (await cell.inner_text()).strip()
                        texts.append(t)

                    # Get project link
                    link = await row.query_selector("a")
                    href = await link.get_attribute("href") if link else ""

                    # Table: [0]checkbox [1]Project Name [2]... [3]Status
                    #   [4]Assigned [5]Location [6]Bid Date [7]Internal ID [8]Source [9]Company
                    project_name = texts[1] if len(texts) > 1 else ""
                    if not project_name or project_name in ("Moved to", ""):
                        continue

                    location = texts[5] if len(texts) > 5 else ""
                    bid_date = texts[6] if len(texts) > 6 else ""
                    company = texts[9] if len(texts) > 9 else ""

                    # Clean and transform
                    clean_name = clean_project_name(project_name)
                    city, state_abbr = parse_location(location)
                    state_full = STATE_ABBR_TO_FULL.get(state_abbr, state_abbr)
                    due_date = parse_cc_date(bid_date)
                    dist = get_distance(city, state_abbr)

                    projects.append({
                        "project_name": clean_name,
                        "gc": company.strip(),
                        "city": city,
                        "state": state_full,
                        "due_date": due_date,
                        "source": "constructconnect",
                        "distance_miles": dist,
                        "portal_url": f"https://app.constructconnect.com{href}" if href and not href.startswith("http") else (href or ""),
                    })

                # --- Pagination: check for next page ---
                # Look for the ">" next page button
                next_btn = await page.query_selector('button[aria-label="Go to next page"]:not([disabled])')
                if next_btn:
                    await next_btn.click()
                    await asyncio.sleep(3)
                    continue

                # Try finding a ">" or "Next" button that's not disabled
                all_buttons = await page.query_selector_all("button")
                found_next = False
                for btn in all_buttons:
                    txt = (await btn.inner_text()).strip()
                    disabled = await btn.get_attribute("disabled")
                    aria = await btn.get_attribute("aria-label") or ""
                    if (txt in (">", "\u203a", "Next") or "next" in aria.lower()) and disabled is None:
                        await btn.click()
                        await asyncio.sleep(3)
                        found_next = True
                        break

                if not found_next:
                    break  # No more pages

        except Exception as e:
            print(f"  ERROR during scrape: {e}")
        finally:
            await browser.close()

    print(f"  Scraped {len(projects)} CC projects total")
    return projects


def merge_bids(cc_bids, existing_bids):
    """Merge CC bids into existing bids list. Adds new CC bids, updates existing ones, keeps all BC bids.
    Never removes a CC bid that's still within its due date."""
    from datetime import date as _date

    today = _date.today()

    # Index new CC bids by (project_name_lower, gc_lower) for matching
    new_cc_index = {}
    for b in cc_bids:
        key = (b["project_name"].lower().strip()[:40], b.get("gc", "").lower().strip()[:30])
        new_cc_index[key] = b

    merged = []
    seen_keys = set()

    # Keep all BC bids
    for b in existing_bids:
        if b.get("source") != "constructconnect":
            merged.append(b)
            continue

        # For existing CC bids: check if there's an updated version from new scrape
        key = (b["project_name"].lower().strip()[:40], b.get("gc", "").lower().strip()[:30])

        if key in new_cc_index:
            # Use the fresh version
            merged.append(new_cc_index[key])
            seen_keys.add(key)
        else:
            # Not in new scrape -- keep it if due date hasn't passed yet
            due = b.get("due_date", "")
            try:
                parts = due.split("/")
                due_date = _date(int(parts[2]), int(parts[0]), int(parts[1]))
                if due_date >= today:
                    merged.append(b)  # still active, keep it
                # else: expired, drop it
            except (ValueError, IndexError):
                merged.append(b)  # can't parse date, keep it to be safe

    # Add new CC bids that weren't already in the list
    for key, b in new_cc_index.items():
        if key not in seen_keys:
            merged.append(b)

    # Sort by distance (closest first), nulls last
    merged.sort(key=lambda b: b.get("distance_miles") if b.get("distance_miles") is not None else 99999)

    return merged


def main():
    parser = argparse.ArgumentParser(description="ConstructConnect Bid Center Scraper")
    parser.add_argument("--dry-run", action="store_true", help="Print results without saving")
    parser.add_argument("--pages", type=int, default=10, help="Max pages to scrape (default: 10)")
    args = parser.parse_args()

    print("=" * 70)
    print("  CC BID CENTER SCRAPER - Carolina Commercial Finishes")
    print("=" * 70)

    # Scrape
    cc_bids = asyncio.run(scrape_cc_inbox(max_pages=args.pages))

    if not cc_bids:
        print("\n  No bids scraped. Check login credentials or network.")
        return

    # Print scraped bids
    print(f"\n  SCRAPED {len(cc_bids)} PROJECTS FROM CONSTRUCTCONNECT:")
    print("  " + "-" * 66)
    print(f"  {'#':>2s}  {'Dist':>6s}  {'Project':<35s}  {'Location':<18s}  {'Due':<12s}  {'GC':<20s}")
    print("  " + "-" * 66)
    for i, b in enumerate(sorted(cc_bids, key=lambda x: x.get("distance_miles") or 999), 1):
        d = b.get("distance_miles")
        dist = f"{d:.0f} mi" if d else "? mi"
        loc = f"{b['city']}, {b['state'][:2]}" if b.get("state") else b.get("city", "")
        print(f"  {i:>2d}  {dist:>6s}  {b['project_name'][:35]:<35s}  {loc[:18]:<18s}  {b['due_date']:<12s}  {b.get('gc','')[:20]}")

    if args.dry_run:
        print("\n  DRY RUN - not saving to active_bids.json")
        print(f"\n  JSON output:\n{json.dumps(cc_bids, indent=2, ensure_ascii=False)}")
        return

    # Merge with existing bids
    existing = []
    if BIDS_FILE.exists():
        existing = json.load(open(BIDS_FILE, encoding="utf-8"))
        bc_count = sum(1 for b in existing if b.get("source") == "buildingconnected")
        cc_old = sum(1 for b in existing if b.get("source") == "constructconnect")
        print(f"\n  Existing bids: {len(existing)} total ({bc_count} BC, {cc_old} CC)")

    merged = merge_bids(cc_bids, existing)

    # Save
    BIDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(BIDS_FILE, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)

    bc_final = sum(1 for b in merged if b.get("source") == "buildingconnected")
    cc_final = sum(1 for b in merged if b.get("source") == "constructconnect")
    print(f"  Saved: {len(merged)} total bids ({bc_final} BC + {cc_final} CC) -> {BIDS_FILE.name}")
    print("=" * 70)


if __name__ == "__main__":
    main()
