#!/usr/bin/env python3
"""
Download documents from ConstructConnect SmartBid Plan Room.
Logs in, navigates to project page, enters the SmartBid iframe,
clicks 'View in SmartBid', and downloads all plans and scopes.

Usage:
  python download_cc_smartbid.py <portal_url> <output_dir>
  python download_cc_smartbid.py "https://app.constructconnect.com/project/850979/850979?sourceType=6" "data/projects/whole-foods/bid_docs"
"""

import asyncio
import json
import sys
import os
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "data" / "config"


async def download_smartbid_documents(portal_url, output_dir):
    from playwright.async_api import async_playwright

    config = json.load(open(CONFIG_DIR / "cc_auth.json"))
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    downloaded = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0.0.0",
            viewport={"width": 1600, "height": 1200},
            accept_downloads=True,
        )
        page = await ctx.new_page()

        try:
            # === Step 1: Login ===
            print("  [1/5] Logging into ConstructConnect...")
            await page.goto("https://app.constructconnect.com", timeout=30000)
            await asyncio.sleep(3)
            email_inp = await page.query_selector("#email-input")
            if email_inp:
                await email_inp.fill(config["username"])
                pwd_inp = await page.query_selector("#password-input")
                if pwd_inp:
                    await pwd_inp.fill(config["password"])
                login_btn = await page.query_selector('button:has-text("Log In")')
                if login_btn:
                    await login_btn.click()
                    await asyncio.sleep(8)

            # Dismiss cookie
            cb = await page.query_selector('button:has-text("Accept and Continue")')
            if cb:
                await cb.click()
                await asyncio.sleep(1)

            # === Step 2: Navigate to project page ===
            print(f"  [2/5] Opening project page...")
            await page.goto(portal_url, timeout=30000)
            await asyncio.sleep(10)

            # Dismiss cookie again
            cb = await page.query_selector('button:has-text("Accept and Continue")')
            if cb:
                await cb.click()
                await asyncio.sleep(1)

            # === Step 3: Find SmartBid iframe and click 'View in SmartBid' ===
            print("  [3/5] Looking for SmartBid Plan Room...")
            smartbid_frame = None
            for frame in page.frames:
                if "smartinsight" in frame.url:
                    smartbid_frame = frame
                    break

            # NEW DIRECT-RENDER FLOW: CC migrated some projects out of the iframe.
            # If we see "Download All" + a file listing on the main page, use that.
            if not smartbid_frame:
                page_text_check = ""
                try:
                    page_text_check = await page.inner_text("body")
                except Exception:
                    pass
                has_dl_all = "Download All" in page_text_check
                has_plans = "Plans" in page_text_check and ".pdf" in page_text_check.lower()
                if has_dl_all and has_plans:
                    print("  [3/5] CC direct-render SmartBid detected (no iframe). Using main page.")
                    # WAIT for the page to finish loading. CC shows "Document is loading,
                    # please wait..." while rendering, and Download All is disabled during that.
                    print("  Waiting for CC page to finish loading...")
                    for tick in range(30):  # up to 60s
                        await asyncio.sleep(2)
                        try:
                            body = await page.inner_text("body")
                            if "Document is loading" not in body and "please wait" not in body.lower():
                                print(f"  Page ready after ~{(tick+1)*2}s")
                                break
                        except Exception:
                            break
                    await page.screenshot(path=str(output / "_planroom.png"), full_page=True)

                    # Click "Download All" — CC will trigger a ZIP download or open a confirm dialog
                    dl_all_btn = await page.query_selector('button:has-text("Download All"), a:has-text("Download All")')
                    if dl_all_btn:
                        print("  [4/5] Clicking Download All...")
                        try:
                            async with page.expect_download(timeout=480_000) as dl_info:
                                await dl_all_btn.click()
                                # Optionally accept any confirm dialog
                                await asyncio.sleep(2)
                                confirm = await page.query_selector(
                                    'button:has-text("OK"), button:has-text("Confirm"), '
                                    'button:has-text("Yes"), button:has-text("Continue"), '
                                    'button:has-text("Download")'
                                )
                                if confirm:
                                    try: await confirm.click()
                                    except: pass
                                # Poll for ZIP prep status
                                for tick in range(48):
                                    await asyncio.sleep(10)
                                    try:
                                        body = await page.inner_text("body")
                                        if "preparing" in body.lower() or "loading" in body.lower():
                                            if tick % 3 == 0:
                                                print(f"  CC still preparing ZIP... (~{(tick+1)*10}s)")
                                        else:
                                            break
                                    except Exception:
                                        break
                            dl = await dl_info.value
                            fn = dl.suggested_filename
                            fp = output / fn
                            await dl.save_as(str(fp))
                            size_kb = fp.stat().st_size / 1024
                            print(f"  DOWNLOADED: {fn} ({size_kb:.0f} KB)")
                            downloaded.append({"name": fn, "path": str(fp), "size_kb": round(size_kb, 1)})
                        except Exception as e:
                            print(f"  Download All failed: {e}")

                    # If Download All didn't work, try clicking individual file links
                    if not downloaded:
                        print("  [4/5] Trying individual PDF links...")
                        for link in await page.query_selector_all('a:has-text(".pdf"), [href*=".pdf"]'):
                            try:
                                async with page.expect_download(timeout=60_000) as dl_info:
                                    await link.click()
                                dl = await dl_info.value
                                fn = dl.suggested_filename
                                fp = output / fn
                                await dl.save_as(str(fp))
                                downloaded.append({"name": fn, "path": str(fp),
                                                   "size_kb": round(fp.stat().st_size / 1024, 1)})
                                print(f"  DOWNLOADED: {fn}")
                            except Exception:
                                continue

                    # Save body text for debugging if nothing worked
                    try:
                        with open(output / "_smartbid_content.txt", "w", encoding="utf-8") as f:
                            f.write(page_text_check[:20000])
                    except Exception: pass
                    await page.screenshot(path=str(output / "_debug_dl_result.png"), full_page=True)

                    if downloaded:
                        # Unzip + return early
                        await browser.close()
                        import zipfile
                        for zf in output.glob("*.zip"):
                            try:
                                print(f"  Extracting: {zf.name}")
                                with zipfile.ZipFile(str(zf), "r") as z:
                                    z.extractall(str(output))
                                    for nm in z.namelist():
                                        ex = output / nm
                                        if ex.is_file():
                                            downloaded.append({
                                                "name": nm, "path": str(ex),
                                                "size_kb": round(ex.stat().st_size / 1024, 1),
                                            })
                            except Exception as e:
                                print(f"  ZIP extract failed: {e}")
                        return {"downloaded": downloaded, "total": len(downloaded)}
                    # else: fall through to old iSqFt-fallback path

            if not smartbid_frame:
                print("  Direct-render flow failed — trying View/Download Documents...")
                # Fallback: click "View/Download Documents" button on CC project page
                vdd_btn = await page.query_selector('a:has-text("View/Download Documents"), button:has-text("View/Download Documents"), a:has-text("View/Download")')
                if vdd_btn:
                    print("  [3/5] Clicking View/Download Documents...")
                    dl_page = None
                    try:
                        async with ctx.expect_page(timeout=15000) as new_page_info:
                            await vdd_btn.click()
                        dl_page = await new_page_info.value
                        await dl_page.wait_for_load_state("domcontentloaded", timeout=30000)
                        await asyncio.sleep(8)
                    except Exception:
                        # Might navigate same page instead of new tab
                        dl_page = page
                        await asyncio.sleep(5)

                    await dl_page.screenshot(path=str(output / "_planroom.png"), full_page=True)
                    print("  [4/5] Looking for downloadable files...")

                    # Try "Select All" checkbox first
                    select_all = await dl_page.query_selector('input[type="checkbox"][id*="select"], label:has-text("Select All"), input[aria-label*="select all"]')
                    if select_all:
                        try:
                            await select_all.click(force=True)
                            await asyncio.sleep(1)
                        except:
                            pass

                    # Look for download buttons
                    dl_btn = await dl_page.query_selector('a:has-text("Download"), button:has-text("Download"), a:has-text("DOWNLOAD")')
                    if dl_btn:
                        print("  [5/5] Downloading files...")
                        try:
                            async with dl_page.expect_download(timeout=120000) as dl_info:
                                await dl_btn.click()
                            dl = await dl_info.value
                            fname = dl.suggested_filename
                            fpath = output / fname
                            await dl.save_as(str(fpath))
                            size_kb = fpath.stat().st_size / 1024
                            print(f"  DOWNLOADED: {fname} ({size_kb:.0f} KB)")
                            downloaded.append({"name": fname, "path": str(fpath), "size_kb": round(size_kb, 1)})
                        except Exception as e:
                            print(f"  Download failed: {e}")

                    # Try individual file links
                    if not downloaded:
                        file_links = await dl_page.query_selector_all('a[href*=".pdf"], a[href*="download"], a[href*="GetFile"], a[href*="file"]')
                        print(f"  Found {len(file_links)} file links, downloading...")
                        for link in file_links[:30]:
                            try:
                                async with dl_page.expect_download(timeout=30000) as dl_info:
                                    await link.click()
                                dl = await dl_info.value
                                fname = dl.suggested_filename
                                fpath = output / fname
                                await dl.save_as(str(fpath))
                                size_kb = fpath.stat().st_size / 1024
                                print(f"  DOWNLOADED: {fname} ({size_kb:.0f} KB)")
                                downloaded.append({"name": fname, "path": str(fpath), "size_kb": round(size_kb, 1)})
                            except:
                                continue

                    # Save page content for debugging
                    try:
                        page_text = await dl_page.inner_text("body")
                        with open(output / "_smartbid_content.txt", "w", encoding="utf-8") as f:
                            f.write(page_text[:20000])
                    except:
                        pass

                    await dl_page.screenshot(path=str(output / "_debug_dl_result.png"), full_page=True)

                    if downloaded:
                        # Skip SmartBid section, go to unzip
                        await browser.close()
                        # Unzip any ZIP files
                        import zipfile
                        for zf in output.glob("*.zip"):
                            try:
                                print(f"  Extracting: {zf.name}")
                                with zipfile.ZipFile(str(zf), "r") as z:
                                    z.extractall(str(output))
                                    for name in z.namelist():
                                        extracted = output / name
                                        if extracted.is_file():
                                            downloaded.append({
                                                "name": name,
                                                "path": str(extracted),
                                                "size_kb": round(extracted.stat().st_size / 1024, 1),
                                            })
                            except Exception as e:
                                print(f"  ZIP extract failed: {e}")
                        return {"downloaded": downloaded, "total": len(downloaded)}

                # If we still have nothing
                if not downloaded:
                    await page.screenshot(path=str(output / "_debug_no_iframe.png"), full_page=True)
                    return {"error": "Could not download documents — no SmartBid iframe and View/Download fallback failed", "downloaded": []}

            # Get project info from the frame
            try:
                body_text = await smartbid_frame.inner_text("body")
                # Save project info
                with open(output / "_project_info.txt", "w", encoding="utf-8") as f:
                    f.write(body_text[:10000])
            except:
                pass

            # Click 'View in SmartBid'
            vib = await smartbid_frame.query_selector('a:has-text("View in SmartBid")')
            if not vib:
                print("  ERROR: 'View in SmartBid' button not found")
                await page.screenshot(path=str(output / "_debug_no_smartbid.png"), full_page=True)
                return {"error": "View in SmartBid not found", "downloaded": []}

            print("  [4/5] Opening SmartBid Plan Room...")
            # Click and wait for new page/tab
            smartbid_page = None
            try:
                async with ctx.expect_page(timeout=15000) as new_page_info:
                    await vib.click()
                smartbid_page = await new_page_info.value
                await smartbid_page.wait_for_load_state("domcontentloaded", timeout=30000)
                await asyncio.sleep(8)
            except Exception as e:
                print(f"  No new tab opened ({e}), checking current page...")
                smartbid_page = page
                await asyncio.sleep(5)

            await smartbid_page.screenshot(path=str(output / "_planroom.png"), full_page=True)

            # === Step 4: Expand folders and select all files ===
            print("  [5/5] Selecting and downloading files...")

            # Click on Plans folder to expand
            plans_toggle = await smartbid_page.query_selector('text=Plans')
            if plans_toggle:
                await plans_toggle.click()
                await asyncio.sleep(2)

            # Click on Scopes folder to expand
            scopes_toggle = await smartbid_page.query_selector('text=Scopes')
            if scopes_toggle:
                await scopes_toggle.click()
                await asyncio.sleep(2)

            # Select all checkboxes
            checkboxes = await smartbid_page.query_selector_all('input[type="checkbox"]')
            print(f"  Found {len(checkboxes)} checkboxes")
            for cb in checkboxes:
                try:
                    if not await cb.is_checked():
                        await cb.check(force=True)
                except:
                    try:
                        await cb.click(force=True)
                    except:
                        pass

            await asyncio.sleep(1)

            # Try individual file downloads first (more reliable)
            # Look for individual file links
            file_links = await smartbid_page.query_selector_all('a[href*=".pdf"], a[href*="download"], a[href*="GetFile"]')
            print(f"  Found {len(file_links)} file links")

            if file_links:
                for link in file_links:
                    try:
                        async with smartbid_page.expect_download(timeout=30000) as dl_info:
                            await link.click()
                        dl = await dl_info.value
                        fname = dl.suggested_filename
                        fpath = output / fname
                        await dl.save_as(str(fpath))
                        size_kb = fpath.stat().st_size / 1024
                        print(f"  DOWNLOADED: {fname} ({size_kb:.0f} KB)")
                        downloaded.append({"name": fname, "path": str(fpath), "size_kb": round(size_kb, 1)})
                    except:
                        continue

            # If no individual files found, try bulk download
            if not downloaded:
                dl_btn = await smartbid_page.query_selector('a:has-text("DOWNLOAD SELECTED FILES"), button:has-text("DOWNLOAD SELECTED FILES")')
                if dl_btn:
                    print("  Clicking DOWNLOAD SELECTED FILES...")
                    await dl_btn.click()
                    # SmartBid generates a ZIP and shows a popup.
                    # First shows "Preparing selected files..." then changes to "DOWNLOAD ZIP FILE"
                    # Wait for ZIP generation. SmartBid shows a sequence:
                    #   "Preparing selected files..." -> "N% ZIPPING..." -> "DOWNLOAD ZIP FILE"
                    # Previous version broke out of the loop as soon as the text
                    # changed from "preparing" to "zipping" — fix: keep waiting as
                    # long as ANY in-progress marker is present, and allow much
                    # longer timeouts (large HSNC-style plan sets need 3–5 min).
                    print("  Waiting for ZIP generation...")
                    in_progress_markers = ("preparing", "zipping", "zip...", "%", "please wait")
                    ready_markers = ("download zip file", "download zip", ".zip")
                    last_progress = ""
                    max_iters = 72  # 72 * 5s = 360s = 6 minutes
                    for wait in range(max_iters):
                        await asyncio.sleep(5)
                        page_text = await smartbid_page.inner_text("body")
                        lower = page_text.lower()
                        if any(m in lower for m in ready_markers):
                            print(f"  ZIP ready after ~{(wait+1)*5}s")
                            break
                        # Extract progress snippet for logging
                        snippet = ""
                        for line in page_text.splitlines():
                            l = line.strip().lower()
                            if any(m in l for m in in_progress_markers) and len(line.strip()) < 80:
                                snippet = line.strip()
                                break
                        if snippet and snippet != last_progress:
                            print(f"  {snippet} (t={(wait+1)*5}s)")
                            last_progress = snippet
                        elif not any(m in lower for m in in_progress_markers):
                            # No progress indicator at all — wait a bit more then bail
                            if wait > 6:
                                print(f"  No progress indicator after {(wait+1)*5}s — aborting wait")
                                break
                    await smartbid_page.screenshot(path=str(output / "_zip_popup.png"), full_page=True)

                    # Look for the "DOWNLOAD ZIP FILE" button in the popup
                    zip_btn = await smartbid_page.query_selector('a:has-text("DOWNLOAD ZIP FILE"), button:has-text("DOWNLOAD ZIP FILE"), a:has-text("Download Zip")')
                    if zip_btn:
                        print("  ZIP file generated, downloading...")
                        try:
                            async with smartbid_page.expect_download(timeout=120000) as dl_info:
                                await zip_btn.click()
                            dl = await dl_info.value
                            fname = dl.suggested_filename
                            fpath = output / fname
                            await dl.save_as(str(fpath))
                            size_kb = fpath.stat().st_size / 1024
                            print(f"  DOWNLOADED: {fname} ({size_kb:.0f} KB)")
                            downloaded.append({"name": fname, "path": str(fpath), "size_kb": round(size_kb, 1)})
                        except Exception as e:
                            print(f"  ZIP download failed: {e}")
                            # Try getting the href and downloading directly
                            href = await zip_btn.get_attribute("href")
                            if href:
                                print(f"  Trying direct URL: {href[:80]}")
                                try:
                                    async with smartbid_page.expect_download(timeout=60000) as dl_info:
                                        await smartbid_page.goto(href)
                                    dl = await dl_info.value
                                    fname = dl.suggested_filename
                                    fpath = output / fname
                                    await dl.save_as(str(fpath))
                                    size_kb = fpath.stat().st_size / 1024
                                    print(f"  DOWNLOADED: {fname} ({size_kb:.0f} KB)")
                                    downloaded.append({"name": fname, "path": str(fpath), "size_kb": round(size_kb, 1)})
                                except Exception as e2:
                                    print(f"  Direct download also failed: {e2}")
                    else:
                        print("  ZIP popup not found, checking for direct download link...")
                        # Maybe the popup has a different text
                        all_links = await smartbid_page.query_selector_all("a")
                        for link in all_links:
                            text = (await link.inner_text()).strip()
                            href = await link.get_attribute("href") or ""
                            if "zip" in text.lower() or "download" in text.lower() or ".zip" in href.lower():
                                print(f"  Found: [{text[:40]}] -> {href[:60]}")

                    await smartbid_page.screenshot(path=str(output / "_debug_dl_failed.png"), full_page=True)

            # Also try to get file listing from the page for the manifest
            try:
                page_text = await smartbid_page.inner_text("body")
                with open(output / "_smartbid_content.txt", "w", encoding="utf-8") as f:
                    f.write(page_text[:20000])
            except:
                pass

        except Exception as e:
            print(f"  ERROR: {e}")
            try:
                await page.screenshot(path=str(output / "_debug_error.png"), full_page=True)
            except:
                pass
            return {"error": str(e), "downloaded": downloaded}
        finally:
            await browser.close()

    # Unzip any ZIP files
    import zipfile
    for zf in output.glob("*.zip"):
        try:
            print(f"  Extracting: {zf.name}")
            with zipfile.ZipFile(str(zf), "r") as z:
                z.extractall(str(output))
                for name in z.namelist():
                    extracted = output / name
                    if extracted.is_file():
                        downloaded.append({
                            "name": name,
                            "path": str(extracted),
                            "size_kb": round(extracted.stat().st_size / 1024, 1),
                        })
        except Exception as e:
            print(f"  ZIP extract failed: {e}")

    return {"downloaded": downloaded, "total": len(downloaded)}


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python download_cc_smartbid.py <portal_url> <output_dir>")
        sys.exit(1)

    portal_url = sys.argv[1]
    output_dir = sys.argv[2]

    result = asyncio.run(download_smartbid_documents(portal_url, output_dir))
    print(json.dumps(result, indent=2, default=str))
