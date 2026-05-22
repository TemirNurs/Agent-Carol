#!/usr/bin/env python3
"""Download Barton Chapel docs individually from CC - Download All crashes the browser."""

import asyncio
import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "ccf-estimator" / "scripts"))

DOWNLOAD_DIR = Path(__file__).resolve().parent.parent / "data" / "projects" / "_downloads" / "Barton Chapel Elementary - Augusta GA"
DRIVE_ID = "1sTiIU43sAbo_DyK0GnnNxOjLccg7C20G"


async def main():
    try:
        from playwright.async_api import async_playwright
        from gdrive_manager import upload_file

        config_path = Path(__file__).resolve().parent.parent / "data" / "config" / "cc_auth.json"
        config_cc = json.load(open(config_path))
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
            ctx = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0.0.0",
                viewport={"width": 1280, "height": 900},
                accept_downloads=True,
            )
            page = await ctx.new_page()

            # Login
            print("Logging in...", flush=True)
            await page.goto("https://app.constructconnect.com", timeout=30000)
            await asyncio.sleep(3)
            await page.fill("#email-input", config_cc["username"])
            await page.fill("#password-input", config_cc["password"])
            await page.click('button:has-text("Log In")')
            await asyncio.sleep(8)
            print(f"Logged in: {page.url}", flush=True)

            # Go to Barton Chapel plan viewer - try specs first (smaller)
            for doc_type in ["specs", "plans"]:
                url = f"https://webtakeoff.takeoff.constructconnect.com/docviewer?projId=6687723&docType={doc_type}&sourceType=2"
                print(f"\nGoing to {doc_type}: {url}", flush=True)
                await page.goto(url, timeout=60000)
                await asyncio.sleep(5)

                # Get list of all document names on the page
                body = await page.inner_text("body")
                print(f"Page content preview: {body[:500]}", flush=True)

                # Try Select All + Download Selected (smaller batches won't crash)
                # Or just click individual PDFs in the list
                # First let's see what's available
                all_elements = await page.query_selector_all("text=.pdf")
                pdf_names = []
                for el in all_elements:
                    name = (await el.inner_text()).strip()
                    if name.endswith(".pdf"):
                        pdf_names.append(name)

                print(f"Found {len(pdf_names)} PDFs in {doc_type}", flush=True)

                # For painting, we mainly need architectural plans (A sheets) and specs
                # Filter to relevant sheets
                relevant = []
                for name in pdf_names:
                    name_lower = name.lower()
                    # Painting relevant: architectural plans, finish schedules, cover/index, interior elevations
                    if any(prefix in name_lower for prefix in [
                        "a", "id", "cover", "index", "finish", "general", "g0", "g-", "gi",
                        "09", "section 09"  # Division 9 = Finishes
                    ]):
                        relevant.append(name)

                if not relevant:
                    relevant = pdf_names  # Download all if we can't filter

                print(f"Downloading {len(relevant)} relevant PDFs...", flush=True)

                downloaded = []
                for pdf_name in relevant:
                    try:
                        # Click on the PDF name in the list to select/download it
                        pdf_el = await page.query_selector(f'text="{pdf_name}"')
                        if not pdf_el:
                            continue

                        # Click to select, then look for a download option
                        await pdf_el.click()
                        await asyncio.sleep(1)

                        # Check if clicking triggered a download
                        # Or look for a download button after selection
                        dl_btn = await page.query_selector('button:has-text("Download"), a:has-text("Download")')
                        if dl_btn and "Download All" not in (await dl_btn.inner_text()):
                            try:
                                async with page.expect_download(timeout=60000) as dl_info:
                                    await dl_btn.click()
                                download = await dl_info.value
                                fp = DOWNLOAD_DIR / download.suggested_filename
                                await download.save_as(str(fp))
                                downloaded.append(str(fp))
                                print(f"  Downloaded: {download.suggested_filename}", flush=True)
                            except Exception:
                                pass
                    except Exception as e:
                        print(f"  Error with {pdf_name}: {e}", flush=True)

                if not downloaded:
                    # Alternative: try right-click > Save as on document items
                    print("Trying alternative download method...", flush=True)

                    # Take screenshot for debugging
                    await page.screenshot(path=f"tests/barton_{doc_type}_page.png")
                    print(f"Screenshot saved: tests/barton_{doc_type}_page.png", flush=True)

            await browser.close()

        # Check results
        files = list(DOWNLOAD_DIR.glob("*"))
        print(f"\nFiles downloaded: {len(files)}", flush=True)
        for f in files:
            size = f.stat().st_size / (1024 * 1024)
            print(f"  {f.name} ({size:.1f} MB)", flush=True)

        # Upload whatever we got
        if files:
            print("\nUploading to Drive...", flush=True)
            for f in files:
                result = upload_file(str(f), DRIVE_ID)
                if "error" in result:
                    print(f"  Upload error for {f.name}: {result['error']}", flush=True)
                else:
                    print(f"  Uploaded: {result.get('name')}", flush=True)

    except Exception as e:
        print(f"FATAL ERROR: {e}", flush=True)
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
