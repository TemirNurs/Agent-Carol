#!/usr/bin/env python3
"""Download bid docs from BC/CC for tomorrow's projects and upload to Google Drive."""

import asyncio
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "ccf-estimator" / "scripts"))
from gdrive_manager import upload_file

DOWNLOAD_DIR = Path(__file__).resolve().parent.parent / "data" / "projects" / "_downloads"

# Tomorrow's projects with their Drive folder IDs
PROJECTS = {
    "VS 228 - Charlotte NC": {
        "search": "Victoria",
        "drive_id": "1XalDBsWN38iQKEAKMFh8KhhlE8BiLAFT",
    },
    "CPCC Fire Training - Huntersville NC": {
        "search": "CPCC",
        "drive_id": "1MjN0fkpC6qOBuEy4L3HkYAhMMWA_Kgmd",
    },
    "USPS VMF - Florence SC": {
        "search": "Postal Service VMF - Florence",
        "drive_id": "1MUuXe1PPaBbChxHeqf7keE7IBX25UPqN",
    },
    "Fort Bragg SOF Hangar - Pope Field NC": {
        "search": "Fort Bragg SOF Hangar",
        "drive_id": "1AJ4ERNr8xwsJx1wddVBqt_YaShSgt-yO",
    },
    "Food Lion 0591 - Graham NC": {
        "search": "Food Lion",
        "drive_id": "1lvVucaz4IZ_P_4p9zHxv_bryi46L749-",
        "source": "email",  # This one is from Rick Shipman plans portal
    },
    "Franklin Plaza Office - Louisburg NC": {
        "search": "Franklin Plaza",
        "drive_id": "10Cmygnlz3edbFql_ftPOlVx0690nkOQ2",
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


async def download_bc_project_docs(page, project_name, search_key):
    """Navigate to a BC project and download its files."""
    proj_dir = DOWNLOAD_DIR / re.sub(r'[^a-zA-Z0-9 _-]', '', project_name)
    proj_dir.mkdir(parents=True, exist_ok=True)

    # We're on the bid board. Find and click the project.
    # Use the search/find feature
    rows = await page.query_selector_all("[role=row]")
    matched_row = None
    for row in rows[1:]:
        text = (await row.inner_text()).strip()
        parts = [p.strip() for p in text.split("\n") if p.strip()]
        if len(parts) >= 2 and search_key.lower() in parts[0].lower():
            trade = parts[1].lower()
            if "paint" in trade or "finish" in trade or "wall" in trade:
                matched_row = row
                break

    if not matched_row:
        return {"project": project_name, "status": "not_found_on_bc", "files": []}

    # Click the project name link
    link = await matched_row.query_selector("a")
    if not link:
        return {"project": project_name, "status": "no_link", "files": []}

    href = await link.get_attribute("href")
    if href and not href.startswith("http"):
        href = f"https://app.buildingconnected.com{href}"

    print(f"  Navigating to: {href}")
    await page.goto(href, timeout=30000)
    await asyncio.sleep(5)

    # Look for Files tab
    files_tab = await page.query_selector('[data-testid="files-tab"], a:has-text("Files"), button:has-text("Files")')
    if files_tab:
        await files_tab.click()
        await asyncio.sleep(3)

    # Scan page for downloadable files
    body_text = await page.inner_text("body")

    # Try "Download All" button first
    dl_all = await page.query_selector(
        'button:has-text("Download All"), button:has-text("Download all"), '
        'a:has-text("Download All"), [aria-label*="Download all"]'
    )

    downloaded_files = []

    if dl_all:
        print(f"  Found 'Download All' button")
        try:
            async with page.expect_download(timeout=120000) as dl_info:
                await dl_all.click()
            download = await dl_info.value
            fp = proj_dir / download.suggested_filename
            await download.save_as(str(fp))
            downloaded_files.append(str(fp))
            print(f"  Downloaded: {download.suggested_filename} ({fp.stat().st_size // 1024} KB)")
        except Exception as e:
            print(f"  Download All failed: {e}")

    if not downloaded_files:
        # Try individual file links
        all_links = await page.query_selector_all("a")
        for a_link in all_links:
            href_val = await a_link.get_attribute("href") or ""
            text_val = (await a_link.inner_text()).strip()
            if any(ext in href_val.lower() or ext in text_val.lower() for ext in [".pdf", ".zip", ".dwg", "download"]):
                try:
                    async with page.expect_download(timeout=30000) as dl_info:
                        await a_link.click()
                    download = await dl_info.value
                    fp = proj_dir / download.suggested_filename
                    await download.save_as(str(fp))
                    downloaded_files.append(str(fp))
                    print(f"  Downloaded: {download.suggested_filename}")
                except Exception:
                    pass

    if not downloaded_files:
        # Take screenshot for debug
        safe_name = re.sub(r'[^a-zA-Z0-9]', '_', project_name)[:30]
        await page.screenshot(path=f"tests/bc_files_{safe_name}.png")
        print(f"  No files downloaded. Screenshot saved.")

    return {"project": project_name, "status": "ok" if downloaded_files else "no_files", "files": downloaded_files}


async def main():
    from playwright.async_api import async_playwright

    config_bc = json.load(open("data/config/bc_auth.json"))
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0.0.0",
            viewport={"width": 1280, "height": 900},
            accept_downloads=True,
        )
        page = await ctx.new_page()

        # Login to BC
        print("=" * 60)
        print("STEP 1: Logging into BuildingConnected")
        print("=" * 60)
        await page.goto("https://app.buildingconnected.com/login", timeout=30000)
        await asyncio.sleep(3)
        await page.fill("#emailField", config_bc["email"])
        await page.click('button:has-text("NEXT")')
        await asyncio.sleep(4)
        pwd = await page.query_selector("#passwordField, input[type=password]")
        if pwd:
            await pwd.fill(config_bc["password"])
            btns = await page.query_selector_all("button")
            for btn in btns:
                txt = (await btn.inner_text()).strip().upper()
                if "SIGN" in txt or "LOG" in txt or "NEXT" in txt:
                    await btn.click()
                    break
            await asyncio.sleep(8)
        print(f"Logged in: {page.url}")

        # Download docs for each project
        print()
        print("=" * 60)
        print("STEP 2: Downloading project documents")
        print("=" * 60)

        all_results = []
        for proj_name, info in PROJECTS.items():
            if info.get("source") == "email":
                print(f"\n--- {proj_name} (from email, skip BC) ---")
                all_results.append({"project": proj_name, "status": "email_source", "files": []})
                continue

            print(f"\n--- {proj_name} ---")
            result = await download_bc_project_docs(page, proj_name, info["search"])
            all_results.append(result)

            # Go back to bid board for next project
            await page.goto("https://app.buildingconnected.com/opportunities/pipeline", timeout=30000)
            await asyncio.sleep(3)

        await browser.close()

    # Upload downloaded files to Google Drive
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

        drive_id = PROJECTS.get(proj_name, {}).get("drive_id")
        if not drive_id:
            print(f"  {proj_name}: No Drive folder ID")
            continue

        for fp in files:
            print(f"  Uploading {Path(fp).name} -> {proj_name}/")
            upload_result = upload_file(fp, drive_id)
            if "error" in upload_result:
                print(f"    Error: {upload_result['error']}")
            else:
                print(f"    Uploaded: {upload_result.get('name')} ({upload_result.get('url', '')[:60]})")

    # Summary
    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    for r in all_results:
        status = r["status"]
        count = len(r.get("files", []))
        print(f"  {r['project']}: {status} ({count} files)")


if __name__ == "__main__":
    asyncio.run(main())
