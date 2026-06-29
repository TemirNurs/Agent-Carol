#!/usr/bin/env python3
"""
bc_login_capture.py - One-time interactive BC login capture.

Opens a REAL (non-headless) Chromium browser window. You manually log in to
BuildingConnected through Autodesk SSO — solve any captcha, complete MFA,
whatever. Once you reach the Bid Board page, press Enter in this terminal
and we save the session state (cookies + localStorage) to:

  data/config/bc_storage_state.json

After that, scrape_bc_inbox.py reuses this state — no more login, no captcha.
The session typically lasts 30 days; when it expires, re-run this script.

Usage:
  python scripts/bc_login_capture.py
"""
from __future__ import annotations
import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / "data" / "config" / "bc_storage_state.json"


def _logged_in(url: str) -> bool:
    """True once we're on an authenticated BuildingConnected app page (i.e. off
    the login screen and off the Autodesk SSO domain)."""
    u = (url or "").lower()
    return "app.buildingconnected.com" in u and "/login" not in u


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--auto", action="store_true",
                    help="Auto-detect the Bid Board (poll URL) instead of waiting "
                         "for an Enter keypress — lets the agent pop the window.")
    ap.add_argument("--timeout", type=int, default=480,
                    help="Max seconds to wait for login in --auto mode (default 480)")
    args = ap.parse_args()

    from playwright.async_api import async_playwright

    print("=" * 70)
    print("BC LOGIN CAPTURE")
    print("=" * 70)
    print()
    print("A Chrome window will open in 3 seconds. Steps:")
    print()
    print("  1. The window will land on the BuildingConnected login page.")
    _bc_login = os.environ.get("BC_LOGIN_EMAIL", "<your portal login email>")
    print(f"  2. Log in normally — use {_bc_login} + your Autodesk")
    print("     password, solve any captcha, complete MFA if asked.")
    if args.auto:
        print("  3. Once you reach the Bid Board / BC home page, STOP — we detect")
        print("     it automatically and save the session. No terminal needed.")
    else:
        print("  3. Once you see the Bid Board (or BC home page) loaded,")
        print("     come back to THIS terminal and press Enter.")
    print("  4. We'll save your session cookies so the scraper never has")
    print("     to log in again (until cookies expire ~30 days).")
    print()
    print("Starting in 3 seconds...")
    await asyncio.sleep(3)

    async with async_playwright() as p:
        # Headed Chromium (real visible window), with realistic user agent
        browser = await p.chromium.launch(
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--start-maximized",
            ],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            viewport=None,  # follows window size
        )
        # Make playwright less detectable
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            window.chrome = window.chrome || {runtime: {}};
        """)
        page = await context.new_page()
        await page.goto("https://app.buildingconnected.com/login")
        print()
        print("Browser is open. Log in manually now.")
        print()

        if args.auto:
            # Poll the URL until we land on an authenticated BC page (off /login
            # and off the Autodesk SSO domain). Require it stable for a few checks
            # so a mid-SSO redirect doesn't trigger a premature save.
            print(f"Auto-detect mode — watching for the Bid Board (up to {args.timeout}s)...")
            deadline = time.monotonic() + args.timeout
            stable = 0
            detected = False
            while time.monotonic() < deadline:
                try:
                    cur = page.url
                except Exception:
                    cur = ""
                if _logged_in(cur):
                    stable += 1
                    if stable >= 3:           # ~6s of staying logged-in
                        detected = True
                        break
                else:
                    stable = 0
                await asyncio.sleep(2)
            if detected:
                print(f"✓ Detected authenticated BC page: {page.url}")
            else:
                print("⚠ Timed out before detecting the Bid Board — saving whatever "
                      "session exists (may be incomplete; re-run if the scraper still "
                      "reports EXPIRED).")
        else:
            # Wait for user confirmation
            try:
                await asyncio.get_event_loop().run_in_executor(
                    None, input, "Press Enter once you see the Bid Board loaded > "
                )
            except (KeyboardInterrupt, EOFError):
                print("\nCancelled.")
                await browser.close()
                return 1

        # Capture session state
        current_url = page.url
        print(f"\nCurrent URL: {current_url}")
        if "buildingconnected.com" not in current_url:
            print("WARNING: not on a BuildingConnected page. Saving state anyway.")

        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        await context.storage_state(path=str(STATE_FILE))
        print(f"\n✓ Saved session to: {STATE_FILE}")
        print(f"  (Cookies + localStorage. Usable for ~30 days.)")
        print()
        print("Next steps:")
        print("  1. Test that the scraper can use it:")
        print("     python scripts/scrape_bc_inbox.py --dry-run")
        print("  2. The daemon will pick up the new session automatically.")
        print()

        await browser.close()
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
