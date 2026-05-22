#!/usr/bin/env python3
"""Download bid docs from ConstructConnect for projects not on BC."""

import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "ccf-estimator" / "scripts"))
from gdrive_manager import upload_file

DOWNLOAD_DIR = Path(__file__).resolve().parent.parent / "data" / "projects" / "_downloads"

CC_PROJECTS = {
    "CPCC Fire Training - Huntersville NC": {
        "search": "CPCC Fire Training",
        "drive_id": "1MjN0fkpC6qOBuEy4L3HkYAhMMWA_Kgmd",
    },
    "Barton Chapel Elementary - Augusta GA": {
        "search": "Barton Chapel",
        "drive_id": "1sTiIU43sAbo_DyK0GnnNxOjLccg7C20G",
    },
    "NHCS Phase 5 - Salisbury NC": {
        "search": "NHCS",
        "drive_id": "1kMNnWlkOZ4BnH_mDwko9WqDHZOStA1zF",
    },
}


async def main():
    from playwright.async_api import async_playwright

    config_cc = json.load(open("data/config/cc_auth.json"))
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0.0.0",
            viewport={"width": 1280, "height": 900},
            accept_downloads=True,
        )
        page = await ctx.new_page()

        # Login to CC
        print("=" * 60)
        print("STEP 1: Logging into ConstructConnect")
        print("=" * 60)
        await page.goto("https://app.constructconnect.com", timeout=30000)
        await asyncio.sleep(3)
        await page.fill("#email-input", config_cc["username"])
        await page.fill("#password-input", config_cc["password"])
        await page.click('button:has-text("Log In")')
        await asyncio.sleep(8)
        print(f"Logged in: {page.url}")

        # Go to Bid Center
        print("\nNavigating to Bid Center...")
        await page.goto("https://app.constructconnect.com/bidcenter/tabs/inbox", timeout=30000)
        await asyncio.sleep(5)

        # Find each project and download docs
        print()
        print("=" * 60)
        print("STEP 2: Downloading project documents")
        print("=" * 60)

        all_results = []

        for proj_name, info in CC_PROJECTS.items():
            print(f"\n--- {proj_name} ---")
            search_key = info["search"]

            # Find matching row in bid center table
            rows = await page.query_selector_all("table tbody tr")
            matched_row = None
            for row in rows:
                text = (await row.inner_text()).strip()
                if search_key.lower() in text.lower():
                    matched_row = row
                    break

            if not matched_row:
                print(f"  Not found in Bid Center")
                all_results.append({"project": proj_name, "status": "not_found", "files": []})
                continue

            # Click on the project
            link = await matched_row.query_selector("a")
            if link:
                await link.click()
                await asyncio.sleep(5)
                print(f"  Opened project: {page.url}")

                # Look for Documents/Plans tab
                docs_tab = await page.query_selector('a:has-text("Documents"), button:has-text("Documents"), a:has-text("Plans"), [data-testid*="document"]')
                if docs_tab:
                    await docs_tab.click()
                    await asyncio.sleep(3)

                # Try Download All
                dl_all = await page.query_selector(
                    'button:has-text("Download All"), button:has-text("Download all"), '
                    'a:has-text("Download All"), button:has-text("Download Selected"), '
                    '[aria-label*="Download"]'
                )

                proj_dir = DOWNLOAD_DIR / re.sub(r'[^a-zA-Z0-9 _-]', '', proj_name)
                proj_dir.mkdir(parents=True, exist_ok=True)
                downloaded_files = []

                if dl_all:
                    print(f"  Found download button")
                    try:
                        async with page.expect_download(timeout=120000) as dl_info:
                            await dl_all.click()
                        download = await dl_info.value
                        fp = proj_dir / download.suggested_filename
                        await download.save_as(str(fp))
                        downloaded_files.append(str(fp))
                        print(f"  Downloaded: {download.suggested_filename} ({fp.stat().st_size // 1024} KB)")
                    except Exception as e:
                        print(f"  Download failed: {e}")

                if not downloaded_files:
                    # Try finding individual PDF links
                    pdf_links = await page.query_selector_all('a[href*=".pdf"], a[href*="download"]')
                    for pdf_link in pdf_links[:10]:
                        try:
                            async with page.expect_download(timeout=30000) as dl_info:
                                await pdf_link.click()
                            download = await dl_info.value
                            fp = proj_dir / download.suggested_filename
                            await download.save_as(str(fp))
                            downloaded_files.append(str(fp))
                            print(f"  Downloaded: {download.suggested_filename}")
                        except Exception:
                            pass

                if not downloaded_files:
                    safe_name = re.sub(r'[^a-zA-Z0-9]', '_', proj_name)[:30]
                    await page.screenshot(path=f"tests/cc_files_{safe_name}.png")
                    print(f"  No files downloaded. Screenshot saved.")

                all_results.append({"project": proj_name, "status": "ok" if downloaded_files else "no_files", "files": downloaded_files})

                # Go back to bid center
                await page.goto("https://app.constructconnect.com/bidcenter/tabs/inbox", timeout=30000)
                await asyncio.sleep(3)
            else:
                all_results.append({"project": proj_name, "status": "no_link", "files": []})

        await browser.close()

    # Upload to Drive
    print()
    print("=" * 60)
    print("STEP 3: Uploading to Google Drive")
    print("=" * 60)
    for result in all_results:
        proj_name = result["project"]
        files = result.get("files", [])
        if not files:
            print(f"  {proj_name}: No files to upload")
            continue

        drive_id = CC_PROJECTS.get(proj_name, {}).get("drive_id")
        if not drive_id:
            continue

        for fp in files:
            print(f"  Uploading {Path(fp).name} -> {proj_name}/")
            ul = upload_file(fp, drive_id)
            if "error" in ul:
                print(f"    Error: {ul['error']}")
            else:
                print(f"    Done: {ul.get('url', '')[:60]}")

    print()
    print("SUMMARY")
    for r in all_results:
        print(f"  {r['project']}: {r['status']} ({len(r.get('files', []))} files)")


if __name__ == "__main__":
    asyncio.run(main())
