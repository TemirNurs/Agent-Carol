#!/usr/bin/env python3
"""
BuildingConnected Bid Board Scraper
Logs into BC, scrapes the Bid Board (Undecided tab), and merges results into active_bids.json.

Usage:
  python scrape_bc_inbox.py              # Scrape and save to active_bids.json
  python scrape_bc_inbox.py --dry-run    # Print results without saving
  python scrape_bc_inbox.py --no-filter  # Include all trades (not just painting)
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
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "ccf-estimator" / "scripts"))
from trade_filter import is_our_trade

# Paths
BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "data" / "config"
AUTH_FILE = CONFIG_DIR / "bc_auth.json"
BIDS_FILE = BASE_DIR / "data" / "memory" / "active_bids.json"

# CCF Office: 3308 Chancellor Ln, Monroe, NC 28110
CCF_LAT = 34.9854
CCF_LON = -80.5495

# City coordinates for distance calculation
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
    "fort bragg, nc": (35.1390, -79.0064),
    "elizabeth city, nc": (36.2946, -76.2510),
    "wilson, nc": (35.7212, -77.9155),
    "butner, nc": (36.1321, -78.7567),
    "stem, nc": (36.1987, -78.7172),
    "wake forest, nc": (35.9799, -78.5097),
    "havelock, nc": (34.8791, -76.9014),
    "nags head, nc": (35.9574, -75.6241),
    "wendell, nc": (35.7810, -78.3697),
    "hillsborough, nc": (36.0754, -79.0998),
    "greenville, nc": (35.6127, -77.3664),
    "weaverville, nc": (35.6971, -82.5607),
    "mooresville, nc": (35.5849, -80.8101),
    "burlington, nc": (36.0957, -79.4378),
    "kinston, nc": (35.2627, -77.5816),
    "cary, nc": (35.7915, -78.7811),
    "morrisville, nc": (35.8235, -78.8256),
    "morehead city, nc": (34.7230, -76.7262),
    "lake norman of catawba, nc": (35.5849, -80.8901),
    "gastonia, nc": (35.2621, -81.1873),
    "kannapolis, nc": (35.4874, -80.6217),
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
    "pendleton, sc": (34.6518, -82.7838),
    "myrtle beach, sc": (33.6891, -78.8867),
    "longs, sc": (33.9141, -78.7203),
    # Virginia
    "richmond, va": (37.5407, -77.4360),
    "midlothian, va": (37.5021, -77.6490),
    "chantilly, va": (38.8943, -77.4311),
    "tysons, va": (38.9187, -77.2311),
    "dumfries, va": (38.5679, -77.3286),
    # Georgia
    "augusta, ga": (33.4735, -81.9748),
    "ringgold, ga": (34.9162, -85.1091),
    # Tennessee
    "chattanooga, tn": (35.0456, -85.3097),
    "morristown, tn": (36.2140, -83.2949),
    "knoxville, tn": (35.9606, -83.9207),
    "cleveland, tn": (35.1595, -84.8766),
    "ooltewah, tn": (35.0784, -85.0588),
    "new market, tn": (36.0987, -83.5527),
    "kimberlin heights, tn": (35.8954, -83.8585),
}

# State name to abbreviation
STATE_ABBREV = {
    "north carolina": "nc", "south carolina": "sc", "virginia": "va",
    "georgia": "ga", "tennessee": "tn", "florida": "fl",
    "maryland": "md", "west virginia": "wv",
}


def haversine_miles(lat1, lon1, lat2, lon2):
    R = 3959
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return round(R * 2 * math.asin(math.sqrt(a)), 1)


def calc_distance(city, state):
    if not city:
        return None
    city_clean = city.strip().lower()
    state_clean = state.strip().lower() if state else ""
    abbr = STATE_ABBREV.get(state_clean, state_clean[:2])
    key = f"{city_clean}, {abbr}"
    if key in CITY_COORDS:
        lat, lon = CITY_COORDS[key]
        return haversine_miles(CCF_LAT, CCF_LON, lat, lon)
    return None


def parse_due_date(raw_text):
    """Parse BC due date from countdown or date string."""
    raw = raw_text.strip()
    # Already a date: "4/9/2026"
    if re.match(r'^\d{1,2}/\d{1,2}/\d{4}$', raw):
        return raw
    # Countdown: "4h 13m", "3d", "23h"
    hours = 0
    h_match = re.search(r'(\d+)h', raw)
    d_match = re.search(r'(\d+)d', raw)
    m_match = re.search(r'(\d+)m', raw)
    if d_match:
        hours += int(d_match.group(1)) * 24
    if h_match:
        hours += int(h_match.group(1))
    if m_match:
        hours += int(m_match.group(1)) / 60
    if hours > 0:
        due_dt = datetime.now() + timedelta(hours=hours)
        return due_dt.strftime("%#m/%#d/%Y")
    return ""


async def scrape_bc_board(filter_trades=True):
    """Login to BC and scrape the full Undecided bid board."""
    from playwright.async_api import async_playwright

    if not AUTH_FILE.exists():
        print(f"  ERROR: No auth config at {AUTH_FILE}")
        return []

    config = json.load(open(AUTH_FILE))

    # Try to use playwright-stealth if available — evades basic bot detection
    # like the "browser not supported" banner Autodesk SSO throws. Best-effort:
    # if the package isn't installed we silently fall back to vanilla.
    try:
        from playwright_stealth import Stealth
        stealth = Stealth()
        use_stealth = True
    except Exception:
        use_stealth = False

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1600, "height": 900},
        )
        page = await ctx.new_page()
        if use_stealth:
            try:
                await stealth.apply_stealth_async(page)
            except Exception:
                pass

        # Intercept the pipeline API. BC's API gives us EVERYTHING (project name,
        # GC company name, location, due date, trade, opportunity ID) — much more
        # reliable than parsing the HTML row text. We use API data as the source
        # of truth and fall back to row parsing only if API misses something.
        api_opportunities = {}  # name (lowercase) -> dict of fields

        async def capture_pipeline_response(response):
            if "/api/opportunities/v2/pipeline" in response.url and response.status == 200:
                try:
                    data = await response.json()
                    for item in data.get("results", []):
                        opp_id = item.get("_id", "")
                        name = (item.get("name", "") or "").strip()
                        if not (opp_id and name):
                            continue
                        # GC company from client.company.name
                        client = item.get("client") or {}
                        company = client.get("company") or {}
                        gc_name = (company.get("name", "") or "").strip()
                        # Location
                        loc = item.get("location") or {}
                        city = (loc.get("city", "") or "").strip()
                        state = (loc.get("state", "") or "").strip()
                        # Due date (ISO -> mm/dd/yyyy)
                        due_iso = item.get("dateDue", "")
                        due_str = ""
                        if due_iso:
                            try:
                                from datetime import datetime as _dt
                                d = _dt.fromisoformat(due_iso.replace("Z", "+00:00"))
                                due_str = d.strftime("%m/%d/%Y")
                            except Exception:
                                due_str = due_iso[:10]
                        api_opportunities[name.lower()] = {
                            "id": opp_id,
                            "name": name,
                            "gc": gc_name,
                            "city": city,
                            "state": state,
                            "due_date": due_str,
                            "size_sf": str(item.get("sqFt", "") or ""),
                            "trade": item.get("tradeName", "") or "",
                            "date_invited": item.get("dateInvited", ""),
                            "request_type": item.get("requestType", ""),
                            "workflow_state": item.get("workflowState", ""),
                        }
                except Exception as e:
                    print(f"  API capture error: {e}")

        page.on("response", capture_pipeline_response)

        # Login strategy:
        # 1. If data/config/bc_storage_state.json exists, USE IT — that's a
        #    captured session from a manual login. No captcha, no SSO walk.
        # 2. Else fall back to the automated Autodesk SSO flow (which is
        #    currently blocked by hCaptcha — see bc_login_capture.py).
        storage_state_file = BASE_DIR / "data" / "config" / "bc_storage_state.json"
        if storage_state_file.exists():
            print("  Using saved session (data/config/bc_storage_state.json) — skipping login")
            # Replace the context with one that has the storage state loaded
            await ctx.close()
            ctx = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0.0.0 Safari/537.36"
                ),
                storage_state=str(storage_state_file),
            )
            page = await ctx.new_page()
            if use_stealth:
                try: await stealth.apply_stealth_async(page)
                except Exception: pass
            # Re-bind the API response capture to the new page
            page.on("response", capture_pipeline_response)
            await page.goto("https://app.buildingconnected.com/opportunities/pipeline",
                            timeout=30000)
            await asyncio.sleep(4)
            # If we got redirected to login, session is expired
            if "/login" in page.url or "signin.autodesk" in page.url:
                print("  Saved session EXPIRED — re-run scripts/bc_login_capture.py")
                # Also log to activity so user sees this in Telegram
                try:
                    import sys as _sys
                    _sys.path.insert(0, str(BASE_DIR / "scripts"))
                    from log_activity import log_activity
                    log_activity("⚠️ Scraper alert",
                        "BC session expired — run `python scripts/bc_login_capture.py` to re-capture cookies.")
                except Exception: pass
                await browser.close()
                return []
            print(f"  Session valid. URL: {page.url[:70]}")
            logged_in_via_session = True
        else:
            logged_in_via_session = False

        # Login: BC migrated to Autodesk SSO (May 2026). The flow now is:
        #   1. BC login page (#emailField) — enter the Autodesk-side email
        #   2. Click NEXT → redirect to signin.autodesk.com
        #   3. Autodesk page (#userName) — re-enter the Autodesk email
        #   4. Click #verify_user_btn (Next) → password page reveals
        #   5. Enter password in revealed field, submit
        #   6. Redirect back to BC bid board
        if not logged_in_via_session:
            print("  Logging into BuildingConnected (via Autodesk SSO)...")
            autodesk_email = config.get("autodesk_email") or config["email"]
            autodesk_pwd   = config.get("autodesk_password") or config["password"]
            await page.goto("https://app.buildingconnected.com/login", timeout=30000)
            await asyncio.sleep(2)
            try:
                # Step 1+2: BC email field -> NEXT -> redirect to Autodesk
                await page.wait_for_selector("#emailField", state="visible", timeout=15000)
                await page.fill("#emailField", autodesk_email)
                await page.click('button:has-text("NEXT")')
                # Wait for Autodesk SSO page
                await page.wait_for_url("**signin.autodesk.com**", timeout=20000)
                print(f"  -> redirected to Autodesk SSO: {page.url[:70]}")
                await asyncio.sleep(2)

                # Step 3+4: Autodesk userName field -> Next
                await page.wait_for_selector("#userName", state="visible", timeout=15000)
                await page.fill("#userName", autodesk_email)
                await page.click("#verify_user_btn")
                await asyncio.sleep(4)

                # Step 5: Autodesk password page — find the password input that's now visible
                pwd_field = None
                for selector in ("#password", "#userPassword", "input[type=password][name=password]",
                                 "input[type=password]:visible", "input[type=password]"):
                    try:
                        await page.wait_for_selector(selector, state="visible", timeout=8000)
                        pwd_field = await page.query_selector(selector)
                        if pwd_field:
                            break
                    except Exception:
                        continue
                if not pwd_field:
                    raise RuntimeError("Autodesk password field not found")
                await pwd_field.fill(autodesk_pwd)
                # Submit
                submitted = False
                for sel in ("#btnSubmit", "#submit_btn", 'button:has-text("Sign in")',
                            'button:has-text("Sign In")', 'button[type=submit]'):
                    try:
                        btn = await page.query_selector(sel)
                        if btn:
                            await btn.click()
                            submitted = True
                            break
                    except Exception:
                        continue
                if not submitted:
                    await pwd_field.press("Enter")
                print("  Submitted password — waiting for BC bid board...")
                await asyncio.sleep(10)
                try: await page.screenshot(path=str(BASE_DIR / "data" / "logs" / "bc_after_login.png"))
                except: pass
            except Exception as e:
                print(f"  Login failed: {e}")
                print(f"  Try: python scripts/bc_login_capture.py  (one-time manual login)")
                try: await page.screenshot(path=str(BASE_DIR / "data" / "logs" / "bc_login_fail.png"))
                except: pass
                await browser.close()
                return []

        # Wait for bid board to load
        print("  Waiting for Bid Board to load...")
        try:
            await page.wait_for_selector("[role=row]", timeout=20000)
        except:
            print("  WARNING: Bid board rows not found, trying to navigate...")
            await page.goto("https://app.buildingconnected.com/opportunities/pipeline", timeout=30000)
            await asyncio.sleep(5)
            try:
                await page.wait_for_selector("[role=row]", timeout=20000)
            except:
                print("  ERROR: Could not load bid board")
                await browser.close()
                return []

        # Wait a moment for API response to be captured
        await asyncio.sleep(2)
        print(f"  Captured {len(api_opportunities)} opportunity IDs from API")

        # Scroll down to load all rows (BC uses virtual scrolling)
        print("  Loading all bid board rows...")
        prev_count = 0
        for scroll_attempt in range(20):
            rows = await page.query_selector_all("[role=row]")
            current_count = len(rows)
            if current_count == prev_count and scroll_attempt > 2:
                break
            prev_count = current_count
            await page.evaluate("window.scrollBy(0, 800)")
            await asyncio.sleep(0.5)

        rows = await page.query_selector_all("[role=row]")
        print(f"  Found {len(rows)} rows (including header)")

        bids = []
        for i in range(1, len(rows)):  # skip header
            row = rows[i]
            try:
                full_text = (await row.inner_text()).strip()
            except:
                continue
            parts = [pt.strip() for pt in full_text.split("\n") if pt.strip()]

            if len(parts) < 8:
                continue

            # Parse row
            project_name = parts[0]
            trade = parts[1]

            # Filter trades
            if filter_trades and not is_our_trade(trade):
                continue

            due_raw = parts[2]
            due_time = parts[3] if len(parts) > 3 else ""

            # Determine if there's a size field
            has_size = bool(re.match(r'^[\d,]+$', parts[4])) if len(parts) > 4 else False

            if has_size:
                size_sf = parts[4].replace(",", "")
                offset = 6  # skip "sq. ft."
            else:
                size_sf = ""
                offset = 4
                if len(parts) > offset and parts[offset] in ("–", "—", "\u2013", "\u2014", "-") or (len(parts) > offset and len(parts[offset]) <= 1):
                    offset = 5

            city = parts[offset] if len(parts) > offset else ""
            state = parts[offset + 1] if len(parts) > offset + 1 else ""

            # GC name and contact (skip dash separator)
            gc_name = parts[offset + 3] if len(parts) > offset + 3 else ""
            gc_contact = parts[offset + 4] if len(parts) > offset + 4 else ""

            # Clean up
            gc_name = gc_name.strip()
            if len(gc_name) <= 2:
                gc_name = ""
            gc_contact = gc_contact.strip()
            if gc_contact in ("Bidding", "Decline", "", "View client info") or len(gc_contact) <= 2:
                gc_contact = ""

            due_date_str = parse_due_date(due_raw)
            distance = calc_distance(city, state)

            # Prefer API data when available — much more reliable than row text
            api_match = api_opportunities.get(project_name.strip().lower())
            if api_match:
                # API takes precedence for GC, city, state, due_date
                if api_match.get("gc"):       gc_name = api_match["gc"]
                if api_match.get("city"):     city = api_match["city"]
                if api_match.get("state"):    state = api_match["state"]
                if api_match.get("due_date"): due_date_str = api_match["due_date"]
                if api_match.get("size_sf"): size_sf = api_match["size_sf"]
                distance = calc_distance(city, state)

            bid = {
                "project_name": project_name,
                "trade": trade,
                "due_date": due_date_str,
            }
            if size_sf:
                bid["size_sf"] = size_sf
            bid["city"] = city
            bid["state"] = state
            bid["gc"] = gc_name
            if gc_contact:
                bid["gc_contact"] = gc_contact
            bid["source"] = "buildingconnected"
            bid["distance_miles"] = distance

            if api_match:
                bid["opportunity_id"] = api_match["id"]
                bid["portal_url"] = f"https://app.buildingconnected.com/opportunities/{api_match['id']}/files"

            bids.append(bid)

        await browser.close()

    return bids


def merge_bids(bc_bids, existing_bids):
    """Merge freshly scraped BC bids with existing bids.

    Strategy:
    1. Keep ALL CC bids (never remove ConstructConnect bids from BC scraper)
    2. Replace existing BC bids with fresh scrape data
    3. Keep old BC bids that are still before due date but weren't in fresh scrape
    4. Add new BC bids from fresh scrape
    5. Sort by distance
    """
    today = date.today()

    # Separate by source. Preserve anything not from BC (CC, email, manual, etc.)
    cc_bids = [b for b in existing_bids if b.get("source") == "constructconnect"]
    old_bc_bids = [b for b in existing_bids if b.get("source") == "buildingconnected"]
    other_bids = [b for b in existing_bids
                  if b.get("source") not in ("constructconnect", "buildingconnected")]

    # Build lookup of new BC bids by project name ONLY (not name+gc). The
    # old name+gc key would fail to match when a stale row had no GC and a
    # fresh row has one, leaving both = duplicate. Name alone is the bid's
    # identity within BC.
    new_bc_keys = set()
    for b in bc_bids:
        key = b["project_name"].lower().strip()[:60]
        new_bc_keys.add(key)

    # Keep old BC bids that aren't in fresh scrape and haven't expired.
    # Anything in the fresh scrape OVERWRITES the old (we trust fresh data).
    kept_old = []
    for b in old_bc_bids:
        key = b["project_name"].lower().strip()[:60]
        if key in new_bc_keys:
            continue  # fresh scrape has this — drop the stale one
        try:
            due = datetime.strptime(b["due_date"], "%m/%d/%Y").date()
            if due >= today:
                kept_old.append(b)
        except (ValueError, TypeError, KeyError):
            kept_old.append(b)  # keep if date is unparseable

    merged = cc_bids + bc_bids + kept_old + other_bids

    # Sort by distance (nulls last)
    merged.sort(key=lambda b: b.get("distance_miles") if b.get("distance_miles") is not None else 9999)

    return merged


def main():
    parser = argparse.ArgumentParser(description="BuildingConnected Bid Board Scraper")
    parser.add_argument("--dry-run", action="store_true", help="Print results without saving")
    parser.add_argument("--no-filter", action="store_true", help="Include all trades")
    args = parser.parse_args()

    W = 70
    print("=" * W)
    print("  BC BID BOARD SCRAPER - Carolina Commercial Finishes")
    print("=" * W)

    bc_bids = asyncio.run(scrape_bc_board(filter_trades=not args.no_filter))

    print(f"\n  SCRAPED {len(bc_bids)} PAINTING/WALLCOVERING PROJECTS FROM BC:")
    print("  " + "-" * (W - 4))
    print(f"  {'#':>3s}  {'Dist':>6s}  {'Project':<40s}  {'Location':<18s}  {'Due':<12s}  {'GC':<20s}")
    print("  " + "-" * (W - 4))
    for i, b in enumerate(bc_bids, 1):
        d = b.get("distance_miles")
        dist = f"{d:.0f} mi" if d else "? mi"
        loc = f"{b.get('city', '')}, {b.get('state', '')[:2]}"
        print(f"  {i:>3d}  {dist:>6s}  {b['project_name'][:40]:<40s}  {loc[:18]:<18s}  {b.get('due_date', ''):<12s}  {b.get('gc', '')[:20]}")

    if args.dry_run:
        print(f"\n  DRY RUN — not saving")
        return

    # Load existing and merge
    existing = []
    if BIDS_FILE.exists():
        existing = json.load(open(BIDS_FILE, encoding="utf-8"))

    merged = merge_bids(bc_bids, existing)

    bc_count = sum(1 for b in merged if b.get("source") == "buildingconnected")
    cc_count = sum(1 for b in merged if b.get("source") == "constructconnect")

    print(f"\n  Existing bids: {len(existing)} total ({sum(1 for b in existing if b.get('source')=='buildingconnected')} BC, {sum(1 for b in existing if b.get('source')=='constructconnect')} CC)")

    with open(BIDS_FILE, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)

    print(f"  Saved: {len(merged)} total bids ({bc_count} BC + {cc_count} CC) -> active_bids.json")
    print("=" * W)


if __name__ == "__main__":
    main()
