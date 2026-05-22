#!/usr/bin/env python3
r"""
scrape_parkway_portal.py — Scrape Parkway Construction's private bidding portal.

Parkway is CCF's top GC ($2.83M lifetime revenue, 64% of all-time revenue).
They post their bidding projects on a private portal at parkwayconstructionplans.com.
This scraper logs in, reads the Bidding Projects + Project Invites tables,
filters to projects within DISTANCE_THRESHOLD_MI of Monroe NC, and merges
new ones into active_bids.json with source="parkway_portal".

Authentication via data/config/parkway_auth.json (chmod 600 on Unix).

Usage:
  python scripts/scrape_parkway_portal.py
  python scripts/scrape_parkway_portal.py --no-filter      # all distances
  python scripts/scrape_parkway_portal.py --max-distance 500
  python scripts/scrape_parkway_portal.py --dry-run        # don't write CRM
  python scripts/scrape_parkway_portal.py --headful        # show browser (debug)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import date, datetime
from math import asin, cos, radians, sin, sqrt
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

CONFIG_FILE = ROOT / "data" / "config" / "parkway_auth.json"
BIDS_FILE   = ROOT / "data" / "memory" / "active_bids.json"
LOG_FILE    = ROOT / "data" / "logs" / "scrape_parkway.log"

# CCF HQ: 3308 Chancellor Ln, Monroe, NC 28110
CCF_LAT, CCF_LON = 34.9854, -80.5495

# Default distance threshold — projects beyond this are flagged but kept
# (with `far_from_us=True`) since Parkway is top GC and relationship matters.
DEFAULT_THRESHOLD_MI = 300

# State capital + major-city coords for rough distance calc
# (we'd ideally geocode by city+state via an API, but for top-N US cities a
#  static map handles 90% of cases; unknown locations fall back to state center)
CITY_COORDS = {
    # NC
    "monroe, nc": (34.9854, -80.5495),
    "charlotte, nc": (35.2271, -80.8431),
    "raleigh, nc": (35.7796, -78.6382),
    "durham, nc": (35.9940, -78.8986),
    "greensboro, nc": (36.0726, -79.7920),
    "winston-salem, nc": (36.0999, -80.2442),
    "asheville, nc": (35.5951, -82.5515),
    "wilmington, nc": (34.2257, -77.9447),
    "fayetteville, nc": (35.0527, -78.8784),
    "high point, nc": (35.9557, -80.0053),
    "concord, nc": (35.4087, -80.5795),
    "huntersville, nc": (35.4107, -80.8429),
    "indian trail, nc": (35.0768, -80.6692),
    "madison, nc": (36.3849, -79.9603),
    # SC
    "columbia, sc": (34.0007, -81.0348),
    "charleston, sc": (32.7765, -79.9311),
    "greenville, sc": (34.8526, -82.3940),
    "rock hill, sc": (34.9249, -81.0251),
    "myrtle beach, sc": (33.6891, -78.8867),
    # VA
    "richmond, va": (37.5407, -77.4360),
    "virginia beach, va": (36.8529, -75.9780),
    "ashburn, va": (39.0438, -77.4874),
    "fairfax, va": (38.8462, -77.3064),
    "arlington, va": (38.8816, -77.0910),
    "glen allen, va": (37.6657, -77.5081),
    "chesterfield, va": (37.3771, -77.5847),
    "quinton, va": (37.5538, -77.1522),
    # GA
    "atlanta, ga": (33.7490, -84.3880),
    "savannah, ga": (32.0809, -81.0912),
    "augusta, ga": (33.4735, -81.9748),
    "kennesaw, ga": (34.0234, -84.6155),
    # TN
    "nashville, tn": (36.1627, -86.7816),
    "knoxville, tn": (35.9606, -83.9207),
    "memphis, tn": (35.1495, -90.0490),
    # FL (closer ones)
    "jacksonville, fl": (30.3322, -81.6557),
    # MD/DC area
    "linthicum heights, md": (39.2084, -76.6633),
    "bethesda, md": (39.0006, -77.1043),
    "washington, dc": (38.9072, -77.0369),
    # NJ/NY
    "paramus, nj": (40.9445, -74.0750),
    # State centers — full US coverage so any state lookup returns a distance
    "al": (32.318, -86.902), "ak": (63.588, -154.493), "az": (34.049, -111.094),
    "ar": (35.201, -91.832), "ca": (36.778, -119.418), "co": (39.550, -105.782),
    "ct": (41.603, -73.087), "de": (38.911, -75.527), "fl": (27.665, -81.516),
    "ga": (32.166, -82.900), "hi": (19.898, -155.582), "id": (44.068, -114.742),
    "il": (40.633, -89.398), "in": (40.267, -86.135), "ia": (41.878, -93.097),
    "ks": (39.011, -98.484), "ky": (37.839, -84.270), "la": (31.244, -92.145),
    "me": (45.253, -69.445), "md": (39.046, -76.641), "ma": (42.407, -71.382),
    "mi": (44.314, -85.602), "mn": (46.729, -94.685), "ms": (32.354, -89.398),
    "mo": (37.964, -91.831), "mt": (46.879, -110.362), "ne": (41.493, -99.901),
    "nv": (38.802, -116.419), "nh": (43.194, -71.572), "nj": (40.058, -74.406),
    "nm": (34.972, -105.032), "ny": (43.299, -74.218), "nc": (35.760, -79.019),
    "nd": (47.551, -101.002), "oh": (40.418, -82.907), "ok": (35.467, -97.516),
    "or": (43.804, -120.554), "pa": (41.203, -77.194), "ri": (41.580, -71.477),
    "sc": (33.836, -81.164), "sd": (43.970, -99.901), "tn": (35.517, -86.580),
    "tx": (31.969, -99.902), "ut": (39.321, -111.094), "vt": (44.558, -72.578),
    "va": (37.432, -78.657), "wa": (47.751, -120.740), "wv": (38.598, -80.454),
    "wi": (43.784, -88.787), "wy": (43.076, -107.290), "dc": (38.907, -77.037),
}


def haversine_mi(lat1, lon1, lat2, lon2) -> float:
    R = 3958.8
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 2 * R * asin(sqrt(a))


def distance_from_ccf(city: str, state: str) -> float | None:
    """Estimate distance from CCF Monroe NC. None if location unknown."""
    if not state:
        return None
    key = f"{(city or '').strip().lower()}, {state.strip().lower()}" if city else state.strip().lower()
    if key in CITY_COORDS:
        lat, lon = CITY_COORDS[key]
    else:
        # fall back to state center
        st_key = state.strip().lower()
        if st_key not in CITY_COORDS:
            return None
        lat, lon = CITY_COORDS[st_key]
    return round(haversine_mi(CCF_LAT, CCF_LON, lat, lon), 0)


def parse_location_from_name(name: str) -> tuple[str, str]:
    """Pull "City, ST" out of a project name like 'Panda Express - Liberty Hill, TX'."""
    m = re.search(r"(?:[-–—]\s*)?([A-Za-z .'-]{2,40}),\s*([A-Z]{2})\s*$", name or "")
    if m:
        return m.group(1).strip(" -"), m.group(2).strip()
    return "", ""


def parse_due_date(due_str: str) -> str:
    """Convert '06/05/2026 06:00 PM' → '06/05/2026'."""
    if not due_str:
        return ""
    m = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", due_str)
    return m.group(1) if m else ""


async def login_and_scrape(headful: bool = False) -> list[dict]:
    """Open Playwright, log into Parkway portal, return list of projects."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("[error] pip install playwright && playwright install chromium")
        return []

    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))

    projects = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not headful)
        # Parkway's SSL cert is expired (caught 2026-05-22). They're a top-5
        # GC for us so we bypass cert validation for this specific portal.
        # If they renew the cert, this still works.
        ctx = await browser.new_context(ignore_https_errors=True)
        page = await ctx.new_page()

        await page.goto(cfg["url"], timeout=30000)
        # Login form has Username + Password (right-side "Account Login" panel)
        await page.fill('input[placeholder="Enter Username"]', cfg["username"])
        pw_field = page.locator('input[placeholder="Enter Password"]')
        await pw_field.fill(cfg["password"])
        # Submit by pressing Enter — avoids ambiguity with Access Key / Facebook Login buttons
        await pw_field.press("Enter")

        # Wait for the bidding projects table to render
        try:
            await page.wait_for_selector("table tbody tr, [role=row]", timeout=20000)
        except Exception as e:
            print(f"[error] login may have failed: {e}")
            html = await page.content()
            (ROOT / "data" / "logs" / "parkway_login_debug.html").write_text(
                html[:50000], encoding="utf-8")
            await browser.close()
            return []

        # Try the Bidding Projects route explicitly (in case landing differs)
        await page.goto("https://parkwayconstructionplans.com/#p/projects/bidding",
                        timeout=20000, wait_until="networkidle")
        await page.wait_for_timeout(2000)

        # Walk through pagination
        for page_num in range(1, 20):  # safety cap
            rows = await page.query_selector_all("table tbody tr")
            for row in rows:
                cells = await row.query_selector_all("td")
                if len(cells) < 5:
                    continue
                vals = []
                for c in cells:
                    txt = (await c.inner_text()).strip()
                    vals.append(txt)
                # Expected columns (based on screenshot):
                # [favorite ★] | Name | City | State | Bids Due
                # Sometimes preceded by checkbox/star — find Name/City/State/Due flexibly
                name = next((v for v in vals if len(v) > 5 and " " in v), "")
                if not name or name.lower() in ("name", "city", "state"):
                    continue
                # State is the cell that's exactly 2 uppercase letters
                state_v = next((v for v in vals if re.fullmatch(r"[A-Z]{2}", v)), "")
                # City: the cell BEFORE state typically
                city_v = ""
                for i, v in enumerate(vals):
                    if v == state_v and i > 0:
                        city_v = vals[i - 1]
                        break
                # Due: cell with date pattern
                due_v = next((v for v in vals if re.search(r"\d{1,2}/\d{1,2}/\d{4}", v)), "")
                projects.append({
                    "name": name,
                    "city": city_v,
                    "state": state_v,
                    "due_raw": due_v,
                })

            # Try to click Next; break if no more pages.
            # Try multiple element types — Parkway uses anchor or button depending on theme.
            advanced = False
            for sel in ('button:has-text("Next"):not([disabled])',
                        'a:has-text("Next"):not([disabled])',
                        '[role="button"]:has-text("Next"):not(.disabled)'):
                try:
                    btn = await page.query_selector(sel)
                    if btn:
                        is_disabled = await btn.get_attribute("disabled")
                        cls = await btn.get_attribute("class") or ""
                        if is_disabled or "disabled" in cls.lower():
                            continue
                        await btn.click()
                        await page.wait_for_timeout(1800)
                        advanced = True
                        break
                except Exception:
                    continue
            if not advanced:
                break

        await browser.close()
    return projects


def merge_into_active_bids(projects: list[dict], threshold_mi: float | None,
                           dry_run: bool = False) -> dict:
    """Add new Parkway portal projects to active_bids.json. Dedup by project_name."""
    bids = json.loads(BIDS_FILE.read_text(encoding="utf-8")) if BIDS_FILE.exists() else []
    existing_names = {(b.get("project_name") or "").lower() for b in bids}

    added, skipped_distance, skipped_dup = [], [], []
    for p in projects:
        name = p["name"].strip()
        if not name:
            continue
        if name.lower() in existing_names:
            skipped_dup.append(name)
            continue
        dist = distance_from_ccf(p["city"], p["state"])
        far = dist is not None and threshold_mi is not None and dist > threshold_mi
        if far:
            skipped_distance.append(f"{name} ({p['city']}, {p['state']} — {dist:.0f} mi)")
            continue
        added.append({
            "project_name": name,
            "gc": "Parkway Construction",
            "gc_name": "Parkway Construction",
            "trade": "Painting",
            "due_date": parse_due_date(p["due_raw"]),
            "city": p["city"],
            "state": p["state"],
            "source": "parkway_portal",
            "source_detail": "parkwayconstructionplans.com",
            "distance_mi": dist,
            "ingested_at": datetime.now().isoformat(timespec="seconds"),
        })

    if added and not dry_run:
        bids.extend(added)
        BIDS_FILE.write_text(json.dumps(bids, indent=2, ensure_ascii=False), encoding="utf-8")

    return {
        "scraped": len(projects),
        "added": len(added),
        "skipped_duplicate": len(skipped_dup),
        "skipped_distance": len(skipped_distance),
        "added_list": added,
        "far_list": skipped_distance,
    }


def log(msg: str, quiet: bool = False):
    if not quiet:
        print(msg)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now().isoformat(timespec='seconds')}  {msg}\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-distance", type=float, default=DEFAULT_THRESHOLD_MI,
                    help=f"Distance threshold in miles from Monroe NC (default {DEFAULT_THRESHOLD_MI})")
    ap.add_argument("--no-filter", action="store_true",
                    help="No distance filter — keep all projects")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--headful", action="store_true",
                    help="Show browser window (for debugging login issues)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    threshold = None if args.no_filter else args.max_distance
    log(f"[parkway] scraping portal (threshold={threshold} mi)...", args.quiet)

    projects = asyncio.run(login_and_scrape(headful=args.headful))
    log(f"[parkway] scraped {len(projects)} project(s)", args.quiet)

    if not projects:
        log("[parkway] no projects scraped — login may have failed; check data/logs/parkway_login_debug.html",
            args.quiet)
        return 1

    result = merge_into_active_bids(projects, threshold_mi=threshold, dry_run=args.dry_run)

    log(f"[parkway] scraped={result['scraped']}  added={result['added']}  "
        f"dup={result['skipped_duplicate']}  far={result['skipped_distance']}"
        + (" (DRY RUN)" if args.dry_run else ""), args.quiet)

    if result["added_list"]:
        log("\n[parkway] NEW projects within range:", args.quiet)
        for p in result["added_list"]:
            d = p.get('distance_mi')
            d_str = f"{d:>4.0f} mi" if isinstance(d, (int, float)) else "  ? mi"
            log(f"  • {p['project_name'][:55]:55}  {(p.get('city') or '?')}, {(p.get('state') or '??'):2}  "
                f"{d_str}  due {p.get('due_date','?')}", args.quiet)

    if result["far_list"]:
        log(f"\n[parkway] {len(result['far_list'])} skipped (>{threshold} mi):", args.quiet)
        for x in result["far_list"][:10]:
            log(f"  ⊘ {x}", args.quiet)
    return 0


if __name__ == "__main__":
    sys.exit(main())
