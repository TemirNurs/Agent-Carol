#!/usr/bin/env python3
"""Debug portal connections with screenshots."""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "ccf-estimator" / "scripts"))
TESTS_DIR = Path(__file__).resolve().parent


async def test_constructconnect():
    print("=" * 60)
    print("TESTING: ConstructConnect")
    print("=" * 60)

    from playwright.async_api import async_playwright

    config_file = Path(__file__).resolve().parent.parent / "data" / "config" / "cc_auth.json"
    with open(config_file) as f:
        config = json.load(f)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()

        try:
            # Step 1: Go to login
            print("[1] Loading login page...")
            await page.goto("https://login.io.constructconnect.com/login", timeout=30000)
            await asyncio.sleep(3)
            await page.screenshot(path=str(TESTS_DIR / "cc_step1_login.png"))
            print(f"    URL: {page.url}")
            print(f"    Title: {await page.title()}")

            # Step 2: List all inputs
            inputs = await page.query_selector_all("input")
            print(f"    Found {len(inputs)} input fields:")
            for inp in inputs:
                inp_type = await inp.get_attribute("type") or "?"
                inp_name = await inp.get_attribute("name") or "?"
                inp_id = await inp.get_attribute("id") or "?"
                inp_ph = await inp.get_attribute("placeholder") or "?"
                print(f"      type={inp_type} name={inp_name} id={inp_id} placeholder={inp_ph}")

            # Step 3: Fill email
            print("[2] Filling email...")
            email_sel = await page.query_selector('input[name="email"], input[type="email"], input[id*="email"], input[id*="user"], input[placeholder*="email" i]')
            if not email_sel:
                # Try first text input
                email_sel = await page.query_selector('input[type="text"]')
            if email_sel:
                await email_sel.fill(config["username"])
                print("    Email filled")
            else:
                print("    ERROR: No email field found")
                await browser.close()
                return

            # Step 4: Fill password
            print("[3] Filling password...")
            pwd_sel = await page.query_selector('input[type="password"]')
            if pwd_sel:
                await pwd_sel.fill(config["password"])
                print("    Password filled")
            else:
                print("    ERROR: No password field")
                await browser.close()
                return

            await page.screenshot(path=str(TESTS_DIR / "cc_step2_filled.png"))

            # Step 5: Click login
            print("[4] Looking for login button...")
            buttons = await page.query_selector_all("button")
            print(f"    Found {len(buttons)} buttons:")
            for btn in buttons:
                btn_text = (await btn.inner_text()).strip()
                btn_type = await btn.get_attribute("type") or "?"
                print(f"      text='{btn_text}' type={btn_type}")

            login_btn = await page.query_selector('button[type="submit"]')
            if not login_btn:
                login_btn = await page.query_selector('button:has-text("Log In")')
            if not login_btn:
                login_btn = await page.query_selector('button:has-text("Sign In")')
            if not login_btn:
                # try any button
                login_btn = (await page.query_selector_all("button"))[0] if buttons else None

            if login_btn:
                btn_text = await login_btn.inner_text()
                print(f"    Clicking button: '{btn_text.strip()}'")
                await login_btn.click()
                print("    Waiting for navigation...")
                await asyncio.sleep(8)
                await page.screenshot(path=str(TESTS_DIR / "cc_step3_after_login.png"))
                print(f"    URL: {page.url}")
                print(f"    Title: {await page.title()}")

                # Check for errors
                errors = await page.query_selector_all('[class*="error"], [class*="alert"], [class*="Error"], [role="alert"]')
                for err in errors:
                    t = await err.inner_text()
                    if t.strip():
                        print(f"    Error on page: {t.strip()[:200]}")

                # Show page content
                body_text = await page.inner_text("body")
                lines = [l.strip() for l in body_text.split("\n") if l.strip()][:30]
                print("    Page content:")
                for line in lines:
                    print(f"      {line[:120]}")
            else:
                print("    ERROR: No login button found")

        except Exception as e:
            print(f"    ERROR: {e}")
            await page.screenshot(path=str(TESTS_DIR / "cc_error.png"))
        finally:
            await browser.close()


async def test_buildingconnected():
    print()
    print("=" * 60)
    print("TESTING: BuildingConnected")
    print("=" * 60)

    from playwright.async_api import async_playwright

    config_file = Path(__file__).resolve().parent.parent / "data" / "config" / "bc_auth.json"
    with open(config_file) as f:
        config = json.load(f)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()

        try:
            print("[1] Loading BuildingConnected...")
            await page.goto("https://app.buildingconnected.com/login", timeout=45000)
            await asyncio.sleep(5)
            await page.screenshot(path=str(TESTS_DIR / "bc_step1_login.png"))
            print(f"    URL: {page.url}")
            print(f"    Title: {await page.title()}")

            # List all inputs
            inputs = await page.query_selector_all("input")
            print(f"    Found {len(inputs)} input fields:")
            for inp in inputs:
                inp_type = await inp.get_attribute("type") or "?"
                inp_name = await inp.get_attribute("name") or "?"
                inp_id = await inp.get_attribute("id") or "?"
                inp_ph = await inp.get_attribute("placeholder") or "?"
                print(f"      type={inp_type} name={inp_name} id={inp_id} placeholder={inp_ph}")

            # Fill email
            print("[2] Looking for email field...")
            email_sel = await page.query_selector('input[type="email"], input[name="userName"], input[id="userName"], input[name="email"]')
            if not email_sel:
                email_sel = await page.query_selector('input[type="text"]')
            if not email_sel:
                email_sel = await page.query_selector('input')

            if email_sel:
                await email_sel.fill(config["email"])
                print("    Email filled")
                await page.screenshot(path=str(TESTS_DIR / "bc_step2_email.png"))

                # Click Next
                print("[3] Looking for Next button...")
                buttons = await page.query_selector_all("button")
                print(f"    Found {len(buttons)} buttons:")
                for btn in buttons:
                    btn_text = (await btn.inner_text()).strip()
                    print(f"      '{btn_text}'")

                next_btn = await page.query_selector('button:has-text("NEXT"), button:has-text("Next"), button:has-text("Continue"), button[type="submit"]')
                if next_btn:
                    print("    Clicking Next...")
                    await next_btn.click()
                    await asyncio.sleep(5)
                    await page.screenshot(path=str(TESTS_DIR / "bc_step3_after_next.png"))
                    print(f"    URL: {page.url}")

                    # Look for password
                    pwd_sel = await page.query_selector('input[type="password"]')
                    if pwd_sel:
                        print("[4] Found password field, filling...")
                        await pwd_sel.fill(config["password"])

                        submit_btn = await page.query_selector('button[type="submit"], button:has-text("Sign in"), button:has-text("Log in"), button:has-text("SIGN IN")')
                        if submit_btn:
                            print("    Clicking Sign In...")
                            await submit_btn.click()
                            await asyncio.sleep(8)
                            await page.screenshot(path=str(TESTS_DIR / "bc_step4_after_signin.png"))
                            print(f"    Final URL: {page.url}")
                            print(f"    Final title: {await page.title()}")

                            body_text = await page.inner_text("body")
                            lines = [l.strip() for l in body_text.split("\n") if l.strip()][:20]
                            print("    Page content:")
                            for line in lines:
                                print(f"      {line[:120]}")
                        else:
                            print("    No sign-in button found")
                    else:
                        print("    No password field after Next")
                        body_text = await page.inner_text("body")
                        lines = [l.strip() for l in body_text.split("\n") if l.strip()][:15]
                        for line in lines:
                            print(f"      {line[:120]}")
                else:
                    print("    No Next button found")
            else:
                print("    No email input found")

        except Exception as e:
            print(f"    ERROR: {e}")
            try:
                await page.screenshot(path=str(TESTS_DIR / "bc_error.png"))
            except:
                pass
        finally:
            await browser.close()


async def main():
    await test_constructconnect()
    await test_buildingconnected()

if __name__ == "__main__":
    asyncio.run(main())
