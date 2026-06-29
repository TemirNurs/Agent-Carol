#!/usr/bin/env python3
"""
BuildingConnected Bid Board Scraper
Logs in via Playwright and extracts all bid invitations with GC names and contacts.
Filters to Painting & Wallcovering trades only.

Usage:
  python bc_scraper.py --all          # All undecided bids (painting only)
  python bc_scraper.py --due-date 3/31/2026  # Bids due on specific date
  python bc_scraper.py --due-tomorrow # Bids due tomorrow
  python bc_scraper.py --due-week     # Bids due this week
"""

import asyncio
import argparse
import json
import re
import sys
from datetime import datetime, date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from trade_filter import is_our_trade

CONFIG_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data" / "config"
AUTH_FILE = CONFIG_DIR / "bc_auth.json"


def _parse_row_parts(parts):
    """Parse a BC bid board row from its text parts.

    Row structure (13 parts without size, 14-15 with size):
    [0] Project Name
    [1] Trade
    [2] Due date countdown or date string
    [3] Due time
    [4] Size (or dash) — sometimes present
    [5] "sq. ft." — only if size present
    [...] City
    [...] State
    [...] dash (separator)
    [...] GC/Client Name
    [...] Contact Name
    [...] dash (separator)
    [...] "Bidding"
    [...] "Decline"
    """
    if len(parts) < 10:
        return None

    project_name = parts[0]
    trade = parts[1]
    due_raw = parts[2]
    due_time = parts[3]

    # Determine if there's a size field
    # If parts[4] is a number with commas, it's a size
    has_size = bool(re.match(r'^[\d,]+$', parts[4])) if len(parts) > 4 else False

    if has_size:
        size_sf = parts[4].replace(",", "")
        # sq. ft. at [5], city at [6], state at [7], dash at [8], GC at [9], contact at [10]
        offset = 6
        city = parts[offset] if len(parts) > offset else ""
        state = parts[offset + 1] if len(parts) > offset + 1 else ""
        gc_name = parts[offset + 3] if len(parts) > offset + 3 else ""
        gc_contact = parts[offset + 4] if len(parts) > offset + 4 else ""
    else:
        size_sf = ""
        # No size: city at [4] or [5], state at [5] or [6]
        # Check if parts[4] is a dash/icon
        offset = 4
        if parts[offset] in ("–", "—", "\u2013", "\u2014") or len(parts[offset]) <= 1:
            offset = 5

        city = parts[offset] if len(parts) > offset else ""
        state = parts[offset + 1] if len(parts) > offset + 1 else ""
        gc_name = parts[offset + 3] if len(parts) > offset + 3 else ""
        gc_contact = parts[offset + 4] if len(parts) > offset + 4 else ""

    # Clean up GC name — remove icons
    gc_name = gc_name.strip()
    if len(gc_name) <= 2:
        gc_name = ""
    gc_contact = gc_contact.strip()
    if gc_contact in ("Bidding", "Decline", "") or len(gc_contact) <= 2:
        gc_contact = ""

    # Parse due date
    due_date_str = ""
    if re.match(r'^\d{1,2}/\d{1,2}/\d{4}$', due_raw):
        due_date_str = due_raw
    elif re.match(r'^\d+h', due_raw):
        # Countdown — calculate actual date
        hours = int(re.match(r'^(\d+)h', due_raw).group(1))
        due_dt = datetime.now() + timedelta(hours=hours)
        due_date_str = due_dt.strftime("%-m/%-d/%Y") if sys.platform != "win32" else due_dt.strftime("%#m/%#d/%Y")

    return {
        "project_name": project_name,
        "trade": trade,
        "due_date": due_date_str,
        "due_time": due_time,
        "size_sf": size_sf,
        "city": city,
        "state": state,
        "location": f"{city}, {state}" if city and state else city or state,
        "gc": gc_name,
        "gc_contact": gc_contact,
        "source": "buildingconnected",
    }


async def scrape_bid_board(filter_trades=True):
    """Login to BC and scrape the full bid board."""
    from playwright.async_api import async_playwright

    if not AUTH_FILE.exists():
        return {"error": f"No auth config. Create {AUTH_FILE}"}

    with open(AUTH_FILE) as f:
        config = json.load(f)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0.0.0",
            viewport={"width": 1600, "height": 900},
        )
        page = await ctx.new_page()

        # Login
        await page.goto("https://app.buildingconnected.com/login", timeout=30000)
        await asyncio.sleep(3)
        await page.fill("#emailField", config["email"])
        await page.click('button:has-text("NEXT")')
        await asyncio.sleep(4)
        pwd = await page.query_selector("#passwordField, input[type=password]")
        if pwd:
            await pwd.fill(config["password"])
            btns = await page.query_selector_all("button")
            for btn in btns:
                txt = (await btn.inner_text()).strip().upper()
                if "SIGN" in txt or "LOG" in txt or "NEXT" in txt:
                    await btn.click()
                    break
            await asyncio.sleep(8)

        # Scrape all rows
        rows = await page.query_selector_all("[role=row]")
        bids = []

        for i in range(1, len(rows)):  # skip header
            row = rows[i]
            full_text = (await row.inner_text()).strip()
            parts = [pt.strip() for pt in full_text.split("\n") if pt.strip()]

            if len(parts) < 8:
                continue

            bid = _parse_row_parts(parts)
            if bid is None:
                continue

            # Filter trades
            if filter_trades and not is_our_trade(bid["trade"]):
                continue

            bids.append(bid)

        await browser.close()

    return {"bids": bids, "total": len(bids)}


def main():
    parser = argparse.ArgumentParser(description="BuildingConnected Bid Board Scraper")
    parser.add_argument("--all", action="store_true", help="All painting/wallcovering bids")
    parser.add_argument("--no-filter", action="store_true", help="Include all trades (no filter)")
    parser.add_argument("--due-date", default=None, help="Filter by due date (M/D/YYYY)")
    parser.add_argument("--due-tomorrow", action="store_true")
    parser.add_argument("--due-week", action="store_true")
    args = parser.parse_args()

    result = asyncio.run(scrape_bid_board(filter_trades=not args.no_filter))

    if "error" in result:
        print(json.dumps(result, indent=2))
        return

    bids = result["bids"]

    # Date filtering
    if args.due_tomorrow:
        tomorrow = (date.today() + timedelta(days=1)).strftime("%-m/%-d/%Y") if sys.platform != "win32" else (date.today() + timedelta(days=1)).strftime("%#m/%#d/%Y")
        bids = [b for b in bids if b["due_date"] == tomorrow]
    elif args.due_date:
        bids = [b for b in bids if b["due_date"] == args.due_date]
    elif args.due_week:
        today = date.today()
        week_end = today + timedelta(days=7)
        filtered = []
        for b in bids:
            try:
                d = datetime.strptime(b["due_date"], "%m/%d/%Y").date()
                if today <= d <= week_end:
                    filtered.append(b)
            except (ValueError, TypeError):
                pass
        bids = filtered

    print(json.dumps({"bids": bids, "total": len(bids)}, indent=2))


if __name__ == "__main__":
    main()
