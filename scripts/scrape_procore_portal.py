#!/usr/bin/env python3
r"""
scrape_procore_portal.py — Scrape Procore's Sub Bid Board for CCF.

Procore is a major construction-management platform — many GCs invite subs
to bid through it. CCF's account is `estimates@carolinacommercialfinishes.com`.
This scraper logs in, walks the bid invitations / sub-bid-board pages, and
adds new opportunities to active_bids.json with source="procore".

Auth: data/config/procore_auth.json

Usage:
  python scripts/scrape_procore_portal.py
  python scripts/scrape_procore_portal.py --no-filter
  python scripts/scrape_procore_portal.py --max-distance 500
  python scripts/scrape_procore_portal.py --dry-run
  python scripts/scrape_procore_portal.py --headful   # show browser (debug)
  python scripts/scrape_procore_portal.py --setup     # interactive first-run
                                                       # (lets you handle 2FA)
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

CONFIG_FILE = ROOT / "data" / "config" / "procore_auth.json"
BIDS_FILE   = ROOT / "data" / "memory" / "active_bids.json"
LOG_FILE    = ROOT / "data" / "logs" / "scrape_procore.log"
STATE_DIR   = ROOT / "data" / "config" / "procore_state"  # for storage_state cookies

# CCF HQ
CCF_LAT, CCF_LON = 34.9854, -80.5495
DEFAULT_THRESHOLD_MI = 300

# Reuse the same state-center coords as the Parkway scraper
# (kept here so this file works standalone if the other is removed)
STATE_COORDS = {
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

# Specific cities (for finer accuracy when name has city)
CITY_COORDS = {
    "monroe, nc": (34.9854, -80.5495),
    "charlotte, nc": (35.2271, -80.8431),
    "raleigh, nc": (35.7796, -78.6382),
    "greensboro, nc": (36.0726, -79.7920),
    "durham, nc": (35.9940, -78.8986),
    "winston-salem, nc": (36.0999, -80.2442),
    "rock hill, sc": (34.9249, -81.0251),
    "columbia, sc": (34.0007, -81.0348),
    "atlanta, ga": (33.7490, -84.3880),
    "richmond, va": (37.5407, -77.4360),
    "ashburn, va": (39.0438, -77.4874),
    "linthicum heights, md": (39.2084, -76.6633),
    # VA Food Lion clusters
    "quinton, va": (37.5538, -77.1522),
    "chester, va": (37.3543, -77.4411),
    "chesterfield, va": (37.3771, -77.5847),
    "aylett, va": (37.7793, -77.1247),
    "madison, nc": (36.3849, -79.9603),
    "high point, nc": (35.9557, -80.0053),
    "huntersville, nc": (35.4107, -80.8429),
    "monroe, nc": (34.9854, -80.5495),
    "indian trail, nc": (35.0768, -80.6692),
}


def haversine_mi(lat1, lon1, lat2, lon2) -> float:
    R = 3958.8
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 2 * R * asin(sqrt(a))


_GEO_CACHE_FILE = ROOT / "data" / "config" / "city_geocode_cache.json"
_GEO_CACHE = None


def _geocode_cache() -> dict:
    """Accurate per-city coords maintained by the geocode_distances daemon job.
    Keyed 'city|STATE' (lowercase city, uppercase state) -> [lat, lon]."""
    global _GEO_CACHE
    if _GEO_CACHE is None:
        try:
            _GEO_CACHE = json.loads(_GEO_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            _GEO_CACHE = {}
    return _GEO_CACHE


def distance_from_ccf(city: str, state: str) -> float | None:
    """Distance from CCF Monroe NC. Resolution order (most → least accurate):
      1. city_geocode_cache.json — real geocoded coords ('city|STATE').
      2. static CITY_COORDS city-level entry.
      3. STATE_COORDS state-center fallback — APPROXIMATE; over-estimates
         border cities (e.g. Harrogate, TN read 342mi vs the true ~206mi)."""
    if not state:
        return None
    c = (city or "").strip()
    st = state.strip()
    # 1. Accurate geocode cache
    if c:
        hit = _geocode_cache().get(f"{c.lower()}|{st.upper()}")
        if isinstance(hit, (list, tuple)) and len(hit) == 2:
            return round(haversine_mi(CCF_LAT, CCF_LON, hit[0], hit[1]), 0)
    # 2. Static city-level coords
    key = f"{c.lower()}, {st.lower()}" if c else None
    if key and key in CITY_COORDS:
        lat, lon = CITY_COORDS[key]
        return round(haversine_mi(CCF_LAT, CCF_LON, lat, lon), 0)
    # 3. State-center fallback (approximate)
    st_key = st.lower()
    if st_key not in STATE_COORDS:
        return None
    lat, lon = STATE_COORDS[st_key]
    return round(haversine_mi(CCF_LAT, CCF_LON, lat, lon), 0)


def parse_due_date(s: str) -> str:
    if not s: return ""
    m = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", s)
    if m: return m.group(1)
    # Procore sometimes shows "May 15, 2026"
    m = re.search(r"([A-Z][a-z]+\s+\d{1,2},\s+\d{4})", s)
    if m:
        try:
            from datetime import datetime
            return datetime.strptime(m.group(1), "%B %d, %Y").strftime("%m/%d/%Y")
        except ValueError:
            return s.strip()
    return s.strip()[:25]


def parse_location(text: str) -> tuple[str, str]:
    """Pull City, ST out of a project string. Procore project names look like
    '2219 Food Lion Quinton, VA' — we want city='Quinton', state='VA'.

    Strategy: find ", ST" anchor, walk backwards through capitalized words,
    stop at common chain words (Food, Lion, Mart, etc.) so we don't slurp them in.
    """
    if not text: return "", ""
    m = re.search(r",\s*([A-Z]{2})\b", text)
    if not m: return "", ""
    state = m.group(1)
    before = text[:m.start()].rstrip()
    # Take last word(s) before the comma — typically just the city
    # Stop at common chain/category words that aren't city names
    STOP_WORDS = {"food", "lion", "mart", "express", "store", "restaurant",
                  "bagels", "barn", "hill", "beauty", "secret", "panda",
                  "starbucks", "warehouse", "kura", "sushi", "men's",
                  "morgan", "truck", "body", "merrill", "gardens", "truewood",
                  "varcity", "tamu", "senior", "living", "jollibee", "remodel",
                  "level99", "garden", "state", "plaza", "vet", "care",
                  "chewy", "einstein", "brothers", "bagels"}
    parts = re.findall(r"[A-Z][a-zA-Z'.-]+", before)
    city_words = []
    for w in reversed(parts):
        if w.lower() in STOP_WORDS:
            break
        city_words.insert(0, w)
        if len(city_words) >= 3:
            break
    city = " ".join(city_words).strip()
    return city, state


async def login_and_scrape(setup_mode: bool = False, headful: bool = False) -> list[dict]:
    """Log in, walk the bid-invitations table, return list of project dicts."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("[error] pip install playwright && playwright install chromium")
        return []

    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state_path = STATE_DIR / "state.json"

    projects = []
    async with async_playwright() as pw:
        # Use headful in setup mode so user can complete 2FA
        browser = await pw.chromium.launch(headless=not (setup_mode or headful))
        ctx_kwargs = {}
        if state_path.exists():
            ctx_kwargs["storage_state"] = str(state_path)
        ctx = await browser.new_context(**ctx_kwargs)
        page = await ctx.new_page()

        # Land on the main dashboard first; we'll discover the bid-board URL
        # from there. Procore changes URL paths frequently.
        target_url = "https://app.procore.com/"
        await page.goto(target_url, timeout=30000)
        # Procore is heavy React — wait for either nav OR login form to render
        try:
            await page.wait_for_selector("nav, [data-qa='global-nav'], input[type='email'], input[type='password']", timeout=20000)
        except Exception:
            pass
        await page.wait_for_timeout(2500)

        if "login" in page.url.lower():
            # Need to authenticate
            try:
                # Step 1: email
                email_input = page.locator('input[type="email"], input[name="user[login]"]').first
                await email_input.fill(cfg["email"], timeout=10000)
                await page.click('button:has-text("Continue"), button[type="submit"]')
                await page.wait_for_timeout(2500)
                # Step 2: password
                pw_input = page.locator('input[type="password"]').first
                await pw_input.fill(cfg["password"], timeout=10000)
                await pw_input.press("Enter")
                # Wait for either bid-invitations table or 2FA prompt
                await page.wait_for_timeout(4000)
            except Exception as e:
                print(f"[error] login flow exception: {e}")

            if "two_factor" in page.url.lower() or "verify" in page.url.lower():
                if setup_mode:
                    print("\n>>> 2FA required. Complete the verification in the browser window.")
                    print(">>> When you're back to the Procore dashboard, press Enter here.")
                    input(">>> Press Enter when done... ")
                else:
                    print("[error] 2FA required but running headless. Re-run with --setup once to authenticate.")
                    html = await page.content()
                    (ROOT / "data" / "logs" / "procore_2fa_debug.html").write_text(
                        html[:50000], encoding="utf-8")
                    await browser.close()
                    return []

            # After login, land back on dashboard
            await page.goto(target_url, timeout=30000)
            try:
                await page.wait_for_selector("nav, [data-qa='global-nav']", timeout=20000)
            except Exception:
                pass
            await page.wait_for_timeout(3000)

        # Persist cookies for next time
        await ctx.storage_state(path=str(state_path))

        # Wait for the dashboard React app to render
        await page.wait_for_timeout(6000)

        # Try the Procore Construction Network bid board (classic sub-side URL)
        bid_urls_to_try = [
            "https://app.procore.com/web/bid_board",
            "https://app.procore.com/web/sub-bid-board",
            "https://app.procore.com/sub_bidding/bid_board",
        ]
        # If user stored a known URL, try it first
        try:
            cfg2 = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if cfg2.get("bid_board_url"):
                bid_urls_to_try.insert(0, cfg2["bid_board_url"])
        except Exception:
            pass

        for url in bid_urls_to_try:
            try:
                await page.goto(url, timeout=30000)
                await page.wait_for_timeout(5000)
                if "404" not in (await page.title()).lower():
                    log(f"[procore] using bid board url: {url}", False)
                    break
            except Exception:
                continue
        else:
            log("[procore] couldn't reach a bid-board URL. Falling back to home-page invitations.", False)
            await page.goto("https://app.procore.com/", timeout=30000)
            await page.wait_for_timeout(5000)

        # Save a screenshot + HTML for first-run inspection — Procore's table
        # layout varies by company; we want to see what we're scraping
        debug_dir = ROOT / "data" / "logs"
        debug_dir.mkdir(parents=True, exist_ok=True)
        try:
            await page.screenshot(path=str(debug_dir / "procore_landing.png"), full_page=True)
            html = await page.content()
            (debug_dir / "procore_landing.html").write_text(html[:200000], encoding="utf-8")
        except Exception:
            pass

        # Procore's sub-side has multiple layouts:
        #  - Bid Board uses table rows with [data-qa='bid-invitation-row']
        #  - Home page "Review Your Invitations" uses cards with project links
        # Try table first; fall back to scanning the whole page text for invitations.
        rows = await page.query_selector_all(
            "table tbody tr, [data-qa='bid-invitation-row'], "
            "[data-qa*='invitation'], [data-testid*='invitation']"
        )
        log(f"[procore] found {len(rows)} table-style rows", False)

        for row in rows:
            cells = await row.query_selector_all("td, [data-qa]")
            texts = []
            for c in cells:
                try:
                    t = (await c.inner_text()).strip()
                except Exception:
                    continue
                if t:
                    texts.append(t)
            if not texts:
                continue
            joined = " | ".join(texts[:8])
            name = texts[0] if len(texts[0]) > 4 else (texts[1] if len(texts) > 1 else "")
            if not name or name.lower() in ("name", "project", "bid"):
                continue
            city, state = parse_location(joined)
            due = next((t for t in texts if re.search(
                r"\d{1,2}/\d{1,2}/\d{4}|\b[A-Z][a-z]+\s+\d{1,2},\s+\d{4}", t)), "")
            projects.append({
                "name": name,
                "city": city,
                "state": state,
                "due_raw": due,
                "raw_row": joined[:200],
            })

        # Fallback: scrape the home-page "Review Your Invitations" panel via text
        if not projects:
            log("[procore] no rows — trying home-page text-scan fallback", False)
            try:
                page_text = await page.inner_text("body")
            except Exception:
                page_text = ""
            # Pattern: "Project Name City, ST" + "Due M/D/YYYY"
            # Extract anything that looks like "<Name>, <ST>" with nearby "Due m/d/yyyy"
            invitation_lines = re.findall(
                r"(\d+\s+[A-Z][\w &.'\-]+?\s+(?:[A-Z][a-z]+,\s*[A-Z]{2}|[A-Z][a-z]+\s+[A-Z]{2}))\s*(?:[\s\S]{0,100}?Due\s+(\d{1,2}/\d{1,2}/\d{4}))?",
                page_text,
            )
            for name_loc, due in invitation_lines[:20]:
                name = name_loc.strip()
                city, state = parse_location(name)
                projects.append({
                    "name": name,
                    "city": city,
                    "state": state,
                    "due_raw": due or "",
                    "raw_row": name_loc[:200],
                })
            if projects:
                log(f"[procore] text-scan found {len(projects)} invitations on home page", False)

        await browser.close()
    return projects


def _normalize_name(n: str) -> str:
    """Lowercase + strip trailing punctuation + collapse whitespace.
    Used for cross-source dedup (email '2541 Food Lion Chester, VA:' should
    match Procore '2541 Food Lion Chester, VA')."""
    if not n: return ""
    n = re.sub(r"\s+", " ", str(n).strip())
    n = re.sub(r"[:\s.]+$", "", n)
    return n.lower()


def merge_into_active_bids(projects: list[dict], threshold_mi: float | None,
                           dry_run: bool) -> dict:
    bids = json.loads(BIDS_FILE.read_text(encoding="utf-8")) if BIDS_FILE.exists() else []
    existing = {_normalize_name(b.get("project_name")) for b in bids}
    added, far, dup = [], [], []
    seen_in_batch = set()  # in-batch dedup
    for p in projects:
        name = (p["name"] or "").strip()
        if not name:
            continue
        nl = _normalize_name(name)
        if nl in existing or nl in seen_in_batch:
            dup.append(name)
            continue
        seen_in_batch.add(nl)
        dist = distance_from_ccf(p["city"], p["state"])
        if (threshold_mi is not None and dist is not None and dist > threshold_mi):
            far.append(f"{name} ({p['city']}, {p['state']} — {dist:.0f} mi)")
            continue
        added.append({
            "project_name": name,
            "trade": "Painting",
            "due_date": parse_due_date(p["due_raw"]),
            "city": p["city"],
            "state": p["state"],
            "source": "procore",
            "source_detail": "procore.com",
            "distance_mi": dist,
            "ingested_at": datetime.now().isoformat(timespec="seconds"),
        })
    if added and not dry_run:
        bids.extend(added)
        BIDS_FILE.write_text(json.dumps(bids, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"scraped": len(projects), "added": len(added),
            "skipped_duplicate": len(dup), "skipped_distance": len(far),
            "added_list": added, "far_list": far}


def log(msg: str, quiet: bool):
    if not quiet:
        print(msg)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now().isoformat(timespec='seconds')}  {msg}\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-distance", type=float, default=DEFAULT_THRESHOLD_MI)
    ap.add_argument("--no-filter", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--headful", action="store_true")
    ap.add_argument("--setup", action="store_true",
                    help="First-run mode — opens browser visibly so you can complete 2FA")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    threshold = None if args.no_filter else args.max_distance
    log(f"[procore] scraping (threshold={threshold} mi, setup={args.setup})...", args.quiet)
    projects = asyncio.run(login_and_scrape(setup_mode=args.setup, headful=args.headful))
    log(f"[procore] scraped {len(projects)} project(s)", args.quiet)
    if not projects:
        log("[procore] nothing scraped — check data/logs/procore_landing.png + .html", args.quiet)
        log("[procore] if first run, try: python scripts/scrape_procore_portal.py --setup", args.quiet)
        return 1
    result = merge_into_active_bids(projects, threshold_mi=threshold, dry_run=args.dry_run)
    log(f"[procore] scraped={result['scraped']}  added={result['added']}  "
        f"dup={result['skipped_duplicate']}  far={result['skipped_distance']}"
        + (" (DRY RUN)" if args.dry_run else ""), args.quiet)
    if result["added_list"]:
        log("\n[procore] NEW projects within range:", args.quiet)
        for p in result["added_list"]:
            d = p.get("distance_mi")
            d_str = f"{d:>4.0f} mi" if isinstance(d, (int, float)) else "  ? mi"
            log(f"  • {p['project_name'][:55]:55}  {(p.get('city') or '?')}, "
                f"{(p.get('state') or '??'):2}  {d_str}  due {p.get('due_date','?')}", args.quiet)
    return 0


if __name__ == "__main__":
    sys.exit(main())
