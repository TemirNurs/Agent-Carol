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
import asyncio
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / "data" / "config" / "bc_storage_state.json"


async def main():
    from playwright.async_api import async_playwright

    print("=" * 70)
    print("BC LOGIN CAPTURE")
    print("=" * 70)
    print()
    print("A Chrome window will open in 3 seconds. Steps:")
    print()
    print("  1. The window will land on the BuildingConnected login page.")
    print("  2. Log in normally — use Setmankg@gmail.com + your Autodesk")
    print("     password, solve any captcha, complete MFA if asked.")
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
