"""
Full flow: Login (handle EUSA) → navigate to Hopewell → upload PDF → click Upload Drawings → monitor.
"""
import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright

AUTH_PATH = Path(r"C:\Agent Carol\data\config\togal_auth.json")
DRAWING_PATH = Path(r"C:\Agent Carol\data\projects\hopewell_elementary_phase2_gym\bid_docs\Drawings\2026 03 13 - HOPEWELL PHASE 2 100 CD SET.pdf")


async def login_with_eusa(page, auth):
    """Login and handle EUSA if it appears."""
    await page.fill('#email', auth["email"])
    await page.fill('#password', auth["password"])
    await page.wait_for_timeout(500)
    await page.click('#login')

    try:
        await page.wait_for_url(lambda url: "/auth/" not in url, timeout=10000)
        return True
    except:
        pass

    # Handle EUSA checkbox
    cb = page.locator('input[type="checkbox"]')
    if await cb.count() == 0:
        return False

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
        await page.mouse.click(pos['x'], pos['y'])
        await page.wait_for_timeout(1000)
        cont = page.locator('button:has-text("Continue")')
        if await cont.count() > 0 and not await cont.first.is_disabled():
            await cont.first.click()
            await page.wait_for_timeout(3000)
            # Re-login after EUSA
            email_input = page.locator('#email')
            if await email_input.count() > 0:
                await page.fill('#email', auth["email"])
                await page.fill('#password', auth["password"])
                await page.wait_for_timeout(500)
                await page.click('#login')
                try:
                    await page.wait_for_url(lambda url: "/auth/" not in url, timeout=15000)
                    return True
                except:
                    pass

    return "/auth/" not in page.url


async def main():
    auth = json.loads(AUTH_PATH.read_text())

    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="msedge", headless=False, slow_mo=200)
        context = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await context.new_page()

        # Login
        print("[1] Logging in...")
        await page.goto("https://app.togal.ai/auth/login", wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(2000)
        success = await login_with_eusa(page, auth)
        print(f"[2] Login {'OK' if success else 'FAILED'}: {page.url}")
        if not success:
            await browser.close()
            return

        await page.wait_for_timeout(2000)

        # Navigate to Hopewell project
        print("[3] Opening Hopewell project...")
        hopewell = page.locator('text=Hopewell')
        if await hopewell.count() > 0:
            await hopewell.first.click()
            await page.wait_for_timeout(5000)
        print(f"[4] At: {page.url}")

        # Click "Upload Drawings" nav button
        print("[5] Clicking 'Upload Drawings'...")
        await page.click('button:has-text("Upload Drawings")')
        await page.wait_for_timeout(3000)

        # Select file
        file_input = page.locator('input[type="file"]')
        fi_count = await file_input.count()
        print(f"[6] File inputs: {fi_count}")

        if fi_count > 0:
            file_size_mb = DRAWING_PATH.stat().st_size / 1024 / 1024
            print(f"[7] Selecting file: {DRAWING_PATH.name} ({file_size_mb:.1f} MB)...")
            await file_input.first.set_input_files(str(DRAWING_PATH))
            await page.wait_for_timeout(3000)
            await page.screenshot(path=r"C:\Agent Carol\data\togal_file_selected.png")

            # Now click the green "Upload Drawings" button in the dialog
            print("[8] Clicking green 'Upload Drawings' button in dialog...")
            # There are two "Upload Drawings" elements - one in nav, one in dialog
            # The dialog one is typically a different styled button
            upload_btns = page.locator('button:has-text("Upload Drawings")')
            btn_count = await upload_btns.count()
            print(f"    Found {btn_count} 'Upload Drawings' buttons")

            # Click the last one (dialog button, not nav button)
            if btn_count > 1:
                await upload_btns.last.click()
                print("    Clicked dialog Upload button!")
            elif btn_count == 1:
                await upload_btns.first.click()
                print("    Clicked Upload button!")

            # Monitor upload progress
            print("[9] Monitoring upload (190MB file, may take several minutes)...")
            for tick in range(120):  # Up to 20 minutes
                await page.wait_for_timeout(10000)

                # Check page state
                status = await page.evaluate("""(() => {
                    const t = document.body.innerText.toLowerCase();
                    const allText = document.body.innerText;
                    return {
                        uploading: t.includes('uploading'),
                        processing: t.includes('processing'),
                        complete: t.includes('upload complete') || t.includes('successfully'),
                        error: t.includes('error') || t.includes('failed'),
                        hasPages: t.includes('page'),
                        snippet: allText.substring(0, 300),
                    };
                })()""")

                parts = [f"tick {tick+1} ({(tick+1)*10}s)"]
                if status.get('uploading'): parts.append("UPLOADING")
                if status.get('processing'): parts.append("PROCESSING")
                if status.get('complete'): parts.append("COMPLETE!")
                if status.get('error'): parts.append("ERROR")
                print(f"    {' | '.join(parts)}")

                # Check if the upload dialog is gone (upload complete)
                dialog = page.locator('text=Choose a file')
                dialog_visible = await dialog.count() > 0
                if not dialog_visible and tick > 2:
                    print("    Upload dialog closed — upload likely complete!")
                    break

                if status.get('complete'):
                    break

                if status.get('error') and tick > 5:
                    print(f"    Error detected! Snippet: {status.get('snippet', '')[:200]}")
                    break

                if tick % 12 == 11:  # Every 2 min
                    await page.screenshot(path=r"C:\Agent Carol\data\togal_upload_progress.png")
                    print(f"    Screenshot saved")

            await page.screenshot(path=r"C:\Agent Carol\data\togal_upload_final.png")
            print("[10] Upload monitoring complete")

            # Check if pages are now visible in the project
            await page.wait_for_timeout(5000)
            page_text = await page.evaluate("document.body.innerText.substring(0, 1000)")
            print(f"[11] Current page state:\n{page_text[:500]}")

        else:
            print("[FAILED] No file input found")

        await page.wait_for_timeout(3000)
        await browser.close()
        print("[DONE]")


if __name__ == "__main__":
    asyncio.run(main())
