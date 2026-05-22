#!/usr/bin/env python3
"""Test portal connections for ConstructConnect and BuildingConnected."""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "ccf-estimator" / "scripts"))


async def test_constructconnect():
    """Test ConstructConnect login via Playwright."""
    print("=" * 60)
    print("TESTING: ConstructConnect")
    print("=" * 60)

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("ERROR: Playwright not installed")
        return False

    config_file = Path(__file__).resolve().parent.parent / "data" / "config" / "cc_auth.json"
    with open(config_file) as f:
        config = json.load(f)

    print(f"Email: {config['username']}")
    print(f"Portal: {config['portal_url']}")
    print()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        try:
            print("[1] Navigating to ConstructConnect login...")
            await page.goto("https://app.constructconnect.com/login", timeout=30000)
            await page.wait_for_load_state("networkidle", timeout=15000)

            title = await page.title()
            print(f"    Page title: {title}")
            url = page.url
            print(f"    URL: {url}")

            # Try to find email input
            print("[2] Looking for login form...")
            email_input = await page.query_selector('input[type="email"], input[name="email"], input[name="username"], input#email, input#username')
            if not email_input:
                # Try broader selectors
                email_input = await page.query_selector('input[type="text"]')

            if email_input:
                print("    Found email input, filling credentials...")
                await email_input.fill(config["username"])

                pwd_input = await page.query_selector('input[type="password"]')
                if pwd_input:
                    await pwd_input.fill(config["password"])
                    print("    Filled password")

                    # Find and click login button
                    login_btn = await page.query_selector('button[type="submit"], button:has-text("Log In"), button:has-text("Sign In"), input[type="submit"]')
                    if login_btn:
                        print("[3] Clicking login button...")
                        await login_btn.click()
                        await page.wait_for_load_state("networkidle", timeout=20000)
                        await asyncio.sleep(3)

                        new_url = page.url
                        new_title = await page.title()
                        print(f"    After login URL: {new_url}")
                        print(f"    After login title: {new_title}")

                        # Check if we're logged in (not on login page anymore)
                        if "login" not in new_url.lower():
                            print("    SUCCESS: Logged into ConstructConnect!")

                            # Try to find projects
                            print("[4] Looking for projects/bids...")
                            content = await page.content()
                            if "project" in content.lower() or "bid" in content.lower():
                                print("    Found project/bid content on page")

                            # Take a snapshot of the page text
                            text = await page.inner_text("body")
                            lines = [l.strip() for l in text.split("\n") if l.strip()][:20]
                            print("    Page content (first 20 lines):")
                            for line in lines:
                                print(f"      {line[:100]}")

                            await browser.close()
                            return True
                        else:
                            print("    FAILED: Still on login page. Check credentials.")
                            # Get any error messages
                            errors = await page.query_selector_all('[class*="error"], [class*="alert"], [role="alert"]')
                            for err in errors:
                                err_text = await err.inner_text()
                                print(f"    Error message: {err_text}")
                    else:
                        print("    Could not find login button")
                else:
                    print("    Could not find password input")
            else:
                print("    Could not find email input")
                # Print page HTML for debugging
                content = await page.content()
                print(f"    Page HTML snippet: {content[:500]}")

        except Exception as e:
            print(f"    ERROR: {str(e)}")
        finally:
            await browser.close()

    return False


async def test_buildingconnected():
    """Test BuildingConnected login via Playwright."""
    print()
    print("=" * 60)
    print("TESTING: BuildingConnected")
    print("=" * 60)

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("ERROR: Playwright not installed")
        return False

    config_file = Path(__file__).resolve().parent.parent / "data" / "config" / "bc_auth.json"
    with open(config_file) as f:
        config = json.load(f)

    print(f"Email: {config['email']}")
    print()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        try:
            print("[1] Navigating to BuildingConnected login...")
            await page.goto("https://app.buildingconnected.com/login", timeout=30000)
            await page.wait_for_load_state("networkidle", timeout=15000)

            title = await page.title()
            url = page.url
            print(f"    Page title: {title}")
            print(f"    URL: {url}")

            # BC may redirect to Autodesk SSO
            print("[2] Looking for email input...")
            email_input = await page.query_selector('input[type="email"], input[name="email"], input#userName, input[name="userName"]')
            if not email_input:
                email_input = await page.query_selector('input[type="text"]')

            if email_input:
                print("    Found email input, filling...")
                await email_input.fill(config["email"])

                # Click Next/Continue
                next_btn = await page.query_selector('button[type="submit"], button:has-text("Next"), button:has-text("Continue"), button:has-text("Sign in"), button#btn_submit')
                if next_btn:
                    print("[3] Clicking Next...")
                    await next_btn.click()
                    await page.wait_for_load_state("networkidle", timeout=15000)
                    await asyncio.sleep(3)

                    new_url = page.url
                    print(f"    URL after Next: {new_url}")

                    # Look for password field
                    pwd_input = await page.query_selector('input[type="password"]')
                    if pwd_input:
                        print("    Found password input, filling...")
                        await pwd_input.fill(config["password"])

                        submit_btn = await page.query_selector('button[type="submit"], button:has-text("Sign in"), button:has-text("Log in"), button#btn_submit')
                        if submit_btn:
                            print("[4] Clicking Sign In...")
                            await submit_btn.click()
                            await page.wait_for_load_state("networkidle", timeout=20000)
                            await asyncio.sleep(5)

                            final_url = page.url
                            final_title = await page.title()
                            print(f"    Final URL: {final_url}")
                            print(f"    Final title: {final_title}")

                            if "login" not in final_url.lower() and "signin" not in final_url.lower():
                                print("    SUCCESS: Logged into BuildingConnected!")

                                text = await page.inner_text("body")
                                lines = [l.strip() for l in text.split("\n") if l.strip()][:20]
                                print("    Page content (first 20 lines):")
                                for line in lines:
                                    print(f"      {line[:100]}")

                                await browser.close()
                                return True
                            else:
                                print("    FAILED: Still on login/signin page")
                                errors = await page.query_selector_all('[class*="error"], [class*="alert"], [role="alert"]')
                                for err in errors:
                                    err_text = await err.inner_text()
                                    print(f"    Error: {err_text}")
                        else:
                            print("    Could not find submit button after password")
                    else:
                        print("    No password field found after email step")
                        # Might be SSO redirect
                        print(f"    Current URL: {page.url}")
                        text = await page.inner_text("body")
                        lines = [l.strip() for l in text.split("\n") if l.strip()][:10]
                        for line in lines:
                            print(f"      {line[:100]}")
                else:
                    print("    Could not find Next/Continue button")
            else:
                print("    Could not find email input")
                content = await page.content()
                print(f"    HTML snippet: {content[:500]}")

        except Exception as e:
            print(f"    ERROR: {str(e)}")
        finally:
            await browser.close()

    return False


async def main():
    print("CCF Portal Connection Test")
    print(f"{'=' * 60}")
    print()

    cc_ok = await test_constructconnect()
    bc_ok = await test_buildingconnected()

    print()
    print("=" * 60)
    print("RESULTS")
    print("=" * 60)
    print(f"  ConstructConnect: {'PASS' if cc_ok else 'FAIL'}")
    print(f"  BuildingConnected: {'PASS' if bc_ok else 'FAIL'}")


if __name__ == "__main__":
    asyncio.run(main())
