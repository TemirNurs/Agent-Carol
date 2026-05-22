"""
Togal AI Web UI Network Intercept - v10
========================================
Uses msedge channel (non-headless) like togal_upload.py.
Handles EUSA + re-login flow. Captures upload API calls.
"""

import asyncio
import json
import os
import sys
from datetime import datetime
from playwright.async_api import async_playwright

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

AUTH_FILE = r"C:\Agent Carol\data\config\togal_auth.json"
PDF_PATH = r"C:\Agent Carol\data\projects\sally-beauty-3622-cary-nc\drawings\3622 Cary Existing Conditions MEP Evaluation Letter_Sally Beauty_20260209.pdf"
TOGAL_URL = "https://app.togal.ai"

with open(AUTH_FILE, "r") as f:
    auth = json.load(f)

EMAIL = auth["email"]
PASSWORD = auth["password"]
PROJECT_ID = auth.get("current_project_id", "")
SET_ID = auth.get("current_set_id", "")

captured = []
upload_phase = False

SKIP = ["amplitude", "sentry.io", "google", "doubleclick",
        "clarity.ms", "facebook", "hotjar", "intercom", "segment",
        "hubspot", "fullstory", "launchdarkly", "googletagmanager",
        "nr-data", "google-analytics"]

STATIC_EXT = {".js", ".css", ".png", ".jpg", ".svg", ".ico", ".woff",
              ".woff2", ".ttf", ".map", ".gif", ".wasm", ".otf"}


def should_capture(url):
    if any(s in url for s in SKIP):
        return False
    base = url.split("?")[0]
    if any(base.endswith(ext) for ext in STATIC_EXT):
        return False
    return True


async def handle_route(route, request):
    url = request.url
    method = request.method

    if should_capture(url):
        headers = dict(request.headers)
        body = request.post_data or ""
        if len(body) > 5000:
            body = f"<{len(body)} bytes>"

        entry = {
            "method": method,
            "url": url,
            "session": headers.get("session", ""),
            "content_type": headers.get("content-type", ""),
            "body": body,
            "phase": "upload" if upload_phase else "pre-upload",
        }
        captured.append(entry)

        if method in ("POST", "PUT", "PATCH", "DELETE"):
            tag = " [UPLOAD]" if upload_phase else ""
            safe = body.encode('ascii', errors='replace').decode('ascii') if body else ""
            print(f"\n>>> {method} {url}{tag}")
            if entry["session"] and entry["session"] != "null":
                print(f"    session: {entry['session'][:60]}")
            if safe and len(safe) < 800:
                print(f"    body: {safe[:600]}")

    await route.continue_()


async def handle_response(response):
    url = response.url
    if not should_capture(url):
        return

    status = response.status
    method = response.request.method

    resp = ""
    try:
        resp = await response.text()
    except:
        pass

    for entry in reversed(captured):
        if entry["url"] == url and entry["method"] == method and "status" not in entry:
            entry["status"] = status
            entry["response"] = resp[:5000] if resp else ""
            break

    if method in ("POST", "PUT", "PATCH", "DELETE"):
        print(f"  <<< {status} {method} {url}")
        if resp and len(resp) < 2000:
            try:
                parsed = json.loads(resp)
                print(f"      {json.dumps(parsed, indent=2)[:1200]}")
            except:
                safe = resp[:300].encode('ascii', errors='replace').decode('ascii')
                if safe.strip():
                    print(f"      {safe}")
    elif ("api" in url or "www-prod" in url or "amazonaws" in url) and status < 400:
        if resp and len(resp) < 2000:
            try:
                parsed = json.loads(resp)
                resp_str = json.dumps(parsed, indent=2)
                if len(resp_str) < 1500:
                    print(f"  < {status} {method} {url}")
                    print(f"    {resp_str[:1200]}")
            except:
                pass
    elif ("api" in url or "www-prod" in url) and status >= 400:
        print(f"  < {status} {method} {url}")


async def login_with_eusa(page, email, password):
    """Handle Togal login including EUSA acceptance and re-login."""

    # Fill and submit login form
    await page.fill('#email', email)
    await page.fill('#password', password)
    await page.wait_for_timeout(500)
    await page.click('#login')

    # Wait for navigation away from login
    try:
        await page.wait_for_url(lambda url: "/auth/" not in url, timeout=15000)
        print("  Login succeeded (no EUSA)")
        return True
    except:
        pass

    # Check for EUSA checkbox
    print("  Checking for EUSA...")
    cb_count = await page.locator('input[type="checkbox"]').count()
    if cb_count == 0:
        print("  No checkbox found")
        return "/auth/" not in page.url

    # Click checkbox using coordinate-based approach (Togal custom checkbox)
    pos = await page.evaluate("""(() => {
        const els = document.querySelectorAll('[class*="Checkbox"]');
        for (const el of els) {
            if (el.tagName !== 'INPUT' && el.offsetHeight > 0 && el.offsetWidth > 0) {
                const r = el.getBoundingClientRect();
                return {x: r.x + r.width/2, y: r.y + r.height/2};
            }
        }
        return null;
    })()""")

    if pos:
        print(f"  Clicking checkbox at ({pos['x']}, {pos['y']})")
        await page.mouse.click(pos['x'], pos['y'])
        await page.wait_for_timeout(1000)

        cont = page.locator('button:has-text("Continue")')
        if await cont.count() > 0 and not await cont.first.is_disabled():
            print("  Clicking Continue...")
            await cont.first.click()
            await page.wait_for_timeout(3000)

            # After EUSA, app may redirect back to login - need to re-login
            email_input = page.locator('#email')
            if await email_input.count() > 0:
                print("  Re-logging in after EUSA...")
                await page.fill('#email', email)
                await page.fill('#password', password)
                await page.wait_for_timeout(500)
                await page.click('#login')
                try:
                    await page.wait_for_url(lambda url: "/auth/" not in url, timeout=15000)
                    print("  Re-login succeeded!")
                    return True
                except:
                    print(f"  Re-login timeout. URL: {page.url}")
    else:
        print("  Could not find checkbox position")

    return "/auth/" not in page.url


async def main():
    global upload_phase

    print(f"=== Togal Intercept v10 (msedge, headed) ===\n")

    async with async_playwright() as p:
        # Use msedge like the working togal_upload.py script
        browser = await p.chromium.launch(
            channel="msedge",
            headless=False,
            slow_mo=100,
        )
        context = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await context.new_page()

        # Set up network capture
        await page.route("**/*", handle_route)
        page.on("response", handle_response)

        # ===== LOGIN =====
        print("=== LOGIN ===")
        await page.goto(f"{TOGAL_URL}/auth/login", wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(2000)

        success = await login_with_eusa(page, EMAIL, PASSWORD)
        print(f"Login result: {'OK' if success else 'FAILED'}")
        print(f"URL: {page.url}")
        await page.screenshot(path=r"C:\Agent Carol\data\debug\togal_v10_login.png")

        if not success:
            print("Login failed. Aborting.")
            await browser.close()
            return

        await page.wait_for_timeout(3000)

        # ===== NAVIGATE TO PROJECT =====
        print(f"\n=== NAVIGATE TO PROJECT ===")

        # Try to find Sally Beauty project on the dashboard
        try:
            project_link = page.get_by_text(PROJECT_NAME := "Sally Beauty Test 0412", exact=False).first
            if await project_link.is_visible(timeout=5000):
                print(f"Found '{PROJECT_NAME}' on dashboard, clicking...")
                await project_link.click()
                await page.wait_for_timeout(5000)
            else:
                # Navigate directly
                url = f"{TOGAL_URL}/project/{PROJECT_ID}/set/{SET_ID}"
                print(f"Navigating to: {url}")
                await page.goto(url, wait_until="networkidle", timeout=30000)
                await page.wait_for_timeout(3000)
        except:
            url = f"{TOGAL_URL}/project/{PROJECT_ID}/set/{SET_ID}"
            print(f"Navigating to: {url}")
            await page.goto(url, wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(3000)

        print(f"URL: {page.url}")
        await page.screenshot(path=r"C:\Agent Carol\data\debug\togal_v10_project.png")

        # ===== UPLOAD =====
        print(f"\n=== UPLOAD PDF ===")
        upload_phase = True
        pre = len(captured)

        # List buttons for debugging
        btns = page.locator("button:visible")
        bc = await btns.count()
        print(f"Visible buttons ({bc}):")
        for i in range(min(bc, 25)):
            try:
                t = await btns.nth(i).inner_text()
                if t.strip():
                    print(f"  '{t.strip()[:60]}'")
            except:
                pass

        # Look for upload mechanism
        fc = await page.locator('input[type="file"]').count()
        print(f"\nFile inputs: {fc}")

        if fc > 0:
            print("Setting file on existing input...")
            await page.locator('input[type="file"]').first.set_input_files(PDF_PATH)
            print("File set!")
        else:
            # Click Upload Drawings or similar button
            upload_triggered = False
            for text in ["Upload Drawings", "Upload", "Add Pages", "Add Plans", "Import", "Add Drawing", "Add"]:
                try:
                    btn = page.locator(f'button:has-text("{text}")').first
                    if await btn.is_visible(timeout=2000):
                        print(f"Clicking '{text}'...")
                        await btn.click()
                        await page.wait_for_timeout(3000)

                        fc2 = await page.locator('input[type="file"]').count()
                        if fc2 > 0:
                            print(f"File input appeared. Setting file...")
                            await page.locator('input[type="file"]').first.set_input_files(PDF_PATH)
                            upload_triggered = True
                            print("File set!")
                            break
                except:
                    continue

            if not upload_triggered:
                print("Could not find upload mechanism")
                await page.screenshot(path=r"C:\Agent Carol\data\debug\togal_v10_no_upload.png")

        # The upload dialog has a green "Upload Drawings" button in top-right
        # Wait a moment for the file to be processed client-side
        print("\nWaiting 3s for client-side PDF processing...")
        await page.wait_for_timeout(3000)
        await page.screenshot(path=r"C:\Agent Carol\data\debug\togal_v10_pre_upload.png")

        # Click the "Upload Drawings" button in the dialog
        print("Looking for 'Upload Drawings' button in dialog...")
        try:
            # The dialog has its own "Upload Drawings" button (not the nav bar one)
            upload_btns = page.locator('button:has-text("Upload Drawings")')
            btn_count = await upload_btns.count()
            print(f"Found {btn_count} 'Upload Drawings' buttons")
            if btn_count > 1:
                # The second one is likely the dialog button
                await upload_btns.nth(btn_count - 1).click()
                print("Clicked dialog 'Upload Drawings' button!")
            elif btn_count == 1:
                await upload_btns.first.click()
                print("Clicked 'Upload Drawings' button!")
        except Exception as e:
            print(f"Error clicking Upload Drawings: {e}")

        # Wait for upload to complete
        print("\nWaiting 30s for upload and processing...")
        await page.wait_for_timeout(15000)
        await page.screenshot(path=r"C:\Agent Carol\data\debug\togal_v10_uploading.png")
        await page.wait_for_timeout(15000)
        await page.screenshot(path=r"C:\Agent Carol\data\debug\togal_v10_done.png")

        upload_count = len(captured) - pre
        print(f"\nUpload-phase requests captured: {upload_count}")

        # ===== RESULTS =====
        print("\n" + "=" * 70)
        print("ALL TOGAL API REQUESTS")
        print("=" * 70)

        # Print only the important ones (API calls, not static/analytics)
        api_requests = [r for r in captured if "api" in r["url"] or "www-prod" in r["url"]
                        or "amazonaws" in r["url"] or "togal" in r["url"]]

        print(f"\nTotal captured: {len(captured)}")
        print(f"API requests: {len(api_requests)}")
        print(f"Upload phase: {upload_count}")

        for i, req in enumerate(api_requests):
            m = req["method"]
            url = req["url"]
            phase = f" [{req['phase'].upper()}]"
            important = m in ("POST", "PUT", "PATCH", "DELETE")
            star = "***" if important else "   "

            try:
                print(f"\n{star} [{i+1}]{phase} {m} {url}")
                if req.get("session") and req["session"] not in ("null", ""):
                    print(f"    session: {req['session'][:60]}")
                if important and req.get("body"):
                    safe = req["body"].encode('ascii', errors='replace').decode('ascii')
                    print(f"    body: {safe[:800]}")
                if req.get("status"):
                    print(f"    status: {req['status']}")
                if req.get("response"):
                    try:
                        parsed = json.loads(req["response"])
                        resp_str = json.dumps(parsed, indent=2)
                        print(f"    response: {resp_str[:1000]}")
                    except:
                        safe = req["response"][:300].encode('ascii', errors='replace').decode('ascii')
                        if safe.strip() and len(safe) < 200:
                            print(f"    response: {safe}")
            except:
                pass

        # Highlight the upload-phase POST/PUT calls
        upload_posts = [r for r in captured if r.get("phase") == "upload" and r["method"] in ("POST", "PUT", "PATCH")]
        if upload_posts:
            print("\n" + "=" * 70)
            print("UPLOAD FLOW - POST/PUT REQUESTS ONLY")
            print("=" * 70)
            for i, req in enumerate(upload_posts):
                try:
                    print(f"\n  [{i+1}] {req['method']} {req['url']}")
                    if req.get("session") and req["session"] not in ("null", ""):
                        print(f"      session: {req['session'][:60]}")
                    if req.get("body"):
                        safe = req["body"].encode('ascii', errors='replace').decode('ascii')
                        print(f"      body: {safe[:800]}")
                    if req.get("status"):
                        print(f"      status: {req['status']}")
                    if req.get("response"):
                        try:
                            parsed = json.loads(req["response"])
                            print(f"      response: {json.dumps(parsed, indent=2)[:1000]}")
                        except:
                            pass
                except:
                    pass

        # Save full capture
        out = r"C:\Agent Carol\data\debug\togal_api_capture.json"
        with open(out, "w", encoding="utf-8") as f:
            json.dump(captured, f, indent=2, default=str, ensure_ascii=True)
        print(f"\nSaved to: {out}")

        await browser.close()

    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
