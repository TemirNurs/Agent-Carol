#!/usr/bin/env python3
"""Download Food Lion 0047A and 0440A docs from ConstructConnect Bid Center."""

import asyncio
import json
import re
import sys
from pathlib import Path

DL_47 = Path(__file__).resolve().parent.parent / "data" / "projects" / "_downloads" / "Food Lion 0047A - Asheboro"
DL_40 = Path(__file__).resolve().parent.parent / "data" / "projects" / "_downloads" / "Food Lion 0440A - Greensboro"
CONFIG = Path(__file__).resolve().parent.parent / "data" / "config" / "cc_auth.json"


async def main():
    from playwright.async_api import async_playwright

    DL_47.mkdir(parents=True, exist_ok=True)
    DL_40.mkdir(parents=True, exist_ok=True)
    config = json.load(open(CONFIG))

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0.0.0",
            viewport={"width": 1600, "height": 900},
            accept_downloads=True,
        )
        page = await ctx.new_page()

        # Login to CC
        print("Logging into CC...", flush=True)
        await page.goto("https://app.constructconnect.com", timeout=30000)
        await asyncio.sleep(3)
        await page.fill("#email-input", config["username"])
        await page.fill("#password-input", config["password"])
        await page.click('button:has-text("Log In")')
        await asyncio.sleep(8)

        # Go to Bid Center
        await page.goto("https://app.constructconnect.com/bidcenter/tabs/inbox", timeout=30000)
        await asyncio.sleep(8)

        # Collect Food Lion project URLs from ALL pages
        food_lions = {}
        for pg in range(1, 5):
            print(f"Page {pg}...", flush=True)
            rows = await page.query_selector_all("table tbody tr")
            for row in rows:
                cells = await row.query_selector_all("td")
                if len(cells) < 6:
                    continue
                texts = [(await c.inner_text()).strip() for c in cells]
                name = texts[1] if len(texts) > 1 else ""
                if not name or "Moved" in name:
                    continue
                if "0047" in name or "0440" in name:
                    link = await row.query_selector("a")
                    href = (await link.get_attribute("href")) if link else ""
                    print(f"  FOUND: {name} -> {href}", flush=True)
                    food_lions[name] = href

            if len(food_lions) >= 2:
                print(f"  Found both projects, stopping pagination", flush=True)
                break

            # Click next page using JS to avoid visibility issues
            next_js = """
            () => {
                const btns = Array.from(document.querySelectorAll('button'));
                const pagBtns = btns.filter(b => {
                    const rect = b.getBoundingClientRect();
                    return rect.top > 500 && !b.disabled && b.textContent.trim().length <= 1;
                });
                for (let i = pagBtns.length - 1; i >= 0; i--) {
                    if (!pagBtns[i].disabled) {
                        pagBtns[i].click();
                        return true;
                    }
                }
                return false;
            }
            """
            next_clicked = await page.evaluate(next_js)
            print(f"  Next page clicked: {next_clicked}", flush=True)
            if not next_clicked:
                break
            await asyncio.sleep(5)

        print(f"\nFound {len(food_lions)} Food Lion projects", flush=True)

        # Download docs for each project
        for name, href in food_lions.items():
            is_47 = "0047" in name
            store = "0047A" if is_47 else "0440A"
            dl_dir = DL_47 if is_47 else DL_40

            print(f"\n=== Food Lion {store} ===", flush=True)

            # Go to the project page first
            full_url = f"https://app.constructconnect.com{href}" if not href.startswith("http") else href
            print(f"  Project page: {full_url}", flush=True)
            await page.goto(full_url, timeout=30000)
            await asyncio.sleep(5)

            body = await page.inner_text("body")
            print(f"  Page preview: {body[:300]}", flush=True)

            # Extract project ID
            pid_match = re.search(r"project/(\d+)", href)
            if pid_match:
                proj_id = pid_match.group(1)
                print(f"  Project ID: {proj_id}", flush=True)

                # Try document viewer with sourceType 6 (SmartBid) and 2 (CC)
                for st in [6, 2]:
                    for doc_type in ["plans", "specs"]:
                        viewer_url = f"https://webtakeoff.takeoff.constructconnect.com/docviewer?projId={proj_id}&docType={doc_type}&sourceType={st}"
                        await page.goto(viewer_url, timeout=30000)
                        await asyncio.sleep(4)
                        vbody = await page.inner_text("body")
                        has_docs = "Download All" in vbody and ".pdf" in vbody.lower()
                        if has_docs:
                            pdf_count = vbody.lower().count(".pdf")
                            print(f"  Found {doc_type} (sourceType={st}): ~{pdf_count} PDFs", flush=True)
                            dl_btn = await page.query_selector('button:has-text("Download All")')
                            if dl_btn:
                                try:
                                    async with page.expect_download(timeout=180000) as dl_info:
                                        await dl_btn.click()
                                    download = await dl_info.value
                                    fn = f"{store}_{doc_type}_{download.suggested_filename}"
                                    fp = dl_dir / fn
                                    await download.save_as(str(fp))
                                    sz = fp.stat().st_size / (1024 * 1024)
                                    print(f"  Downloaded: {fn} ({sz:.1f} MB)", flush=True)
                                except Exception as e:
                                    print(f"  Download error: {e}", flush=True)

        await browser.close()

    # Summary
    for label, d in [("0047A", DL_47), ("0440A", DL_40)]:
        files = list(d.glob("*"))
        print(f"\nFood Lion {label}: {len(files)} files", flush=True)
        for f in files:
            print(f"  {f.name} ({f.stat().st_size / (1024 * 1024):.1f} MB)", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
