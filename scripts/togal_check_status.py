"""
Check Togal processing status and run AI takeoff when ready.
"""
import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright

AUTH_PATH = Path(r"C:\Agent Carol\data\config\togal_auth.json")


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
        if not success:
            print("[FAILED] Login failed")
            await browser.close()
            return
        print(f"[2] Logged in: {page.url}")
        await page.wait_for_timeout(2000)

        # Navigate to Hopewell
        hopewell = page.locator('text=Hopewell')
        if await hopewell.count() > 0:
            await hopewell.first.click()
            await page.wait_for_timeout(5000)
        print(f"[3] At: {page.url}")

        # Check processing status
        page_text = await page.evaluate("document.body.innerText")
        processing = "processing" in page_text.lower()
        print(f"[4] Processing: {processing}")

        # Count how many pages are still processing
        if processing:
            import re
            match = re.search(r'(\d+)\s+left', page_text.lower())
            if match:
                print(f"    Pages remaining: {match.group(1)}")

        # Get list of all drawing sheets
        sheets = await page.evaluate("""(() => {
            // Find sheet name elements
            const items = document.querySelectorAll('[class*="DrawingItem"], [class*="PageItem"], [class*="page-name"], [class*="PageName"]');
            if (items.length > 0) {
                return Array.from(items).map(i => i.textContent.trim().substring(0, 80));
            }
            // Fallback: look for the sheet list in the page text
            const text = document.body.innerText;
            const lines = text.split('\\n').filter(l => /^[A-Z]\d/.test(l.trim()) || /^25013/.test(l.trim()));
            return lines.map(l => l.trim().substring(0, 80));
        })()""")
        print(f"[5] Drawing sheets found: {len(sheets)}")
        for s in sheets[:20]:
            print(f"    {s}")
        if len(sheets) > 20:
            print(f"    ... and {len(sheets) - 20} more")

        await page.screenshot(path=r"C:\Agent Carol\data\togal_status.png")

        # Check for any AI Takeoff button
        buttons = await page.evaluate("""(() => {
            const btns = document.querySelectorAll('button, [role="button"]');
            return Array.from(btns).filter(b => b.offsetHeight > 0 && b.textContent.trim().length > 0)
                .map(b => b.textContent.trim().substring(0, 60));
        })()""")
        print(f"[6] Buttons: {buttons}")

        # If not processing, look for AI Takeoff option
        if not processing:
            # Try clicking on a sheet to see takeoff options
            sheet_item = page.locator('text=A101').first
            if await sheet_item.count() > 0:
                await sheet_item.click()
                await page.wait_for_timeout(3000)
                await page.screenshot(path=r"C:\Agent Carol\data\togal_sheet_view.png")
                print("[7] Clicked on A101 sheet")

                # Look for AI takeoff
                ai_btns = await page.evaluate("""(() => {
                    const btns = document.querySelectorAll('button, [role="button"]');
                    return Array.from(btns).filter(b => b.offsetHeight > 0)
                        .map(b => b.textContent.trim()).filter(t => t.length > 0);
                })()""")
                print(f"    Buttons: {ai_btns}")

        await page.wait_for_timeout(3000)
        await browser.close()
        print("[DONE]")


if __name__ == "__main__":
    asyncio.run(main())
