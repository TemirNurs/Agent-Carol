#!/usr/bin/env python3
"""
CCF Project Document Fetcher
Finds a project in active_bids.json, downloads all bid documents from the
source portal (ConstructConnect or BuildingConnected), parses PDFs for
painting scope, and saves everything organized.

Carol calls this script when a user asks to "check the scope" or
"what do they need from us" for a specific bid.

Usage:
  python fetch_project_docs.py "Whole Food Market"           # fuzzy match
  python fetch_project_docs.py --list-tomorrow               # list bids due tomorrow
  python fetch_project_docs.py --dry-run "Whole Food Market"  # show what would happen
  python fetch_project_docs.py --force "Whole Food Market"    # re-download even if exists
"""

import asyncio
import argparse
import difflib
import json
import os
import re
import sys
from datetime import datetime, date, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# === Path setup ===
BASE_DIR = Path(__file__).resolve().parent.parent
BIDS_FILE = BASE_DIR / "data" / "memory" / "active_bids.json"
PROJECTS_DIR = BASE_DIR / "data" / "projects"
CONFIG_DIR = BASE_DIR / "data" / "config"
SKILLS_SCRIPTS = BASE_DIR / "skills" / "ccf-estimator" / "scripts"

# Add skills scripts to path for imports
sys.path.insert(0, str(SKILLS_SCRIPTS))
sys.path.insert(0, str(BASE_DIR / "scripts"))

# Load ROOT .env so GMAIL_APP_PASSWORD / portal creds are present on a bare CLI
# run (the daemon pre-loads env, but a direct call / fresh subprocess needs this).
# Must precede any _lib.gmail import (it binds GMAIL_PASS at import time).
try:
    from dotenv import load_dotenv as _ld
    _ld(BASE_DIR / ".env")
except Exception:
    pass

# === File classification patterns (from doc_downloader.py) ===
FILE_TYPE_PATTERNS = {
    "plans": [r"plan", r"drawing", r"sheet", r"A\d", r"S\d", r"M\d", r"E\d", r"P\d",
              r"floor\s*plan", r"elevation", r"section", r"detail"],
    "specs": [r"spec", r"specification", r"division", r"section\s*\d{2}",
              r"09\s*91", r"09\s*96", r"09\s*72", r"masterformat"],
    "scope_letter": [r"scope", r"scope\s*letter", r"scope\s*of\s*work", r"SOW",
                     r"bid\s*form", r"bid\s*package"],
    "addendum": [r"addend", r"addenda", r"revision", r"revised", r"ASI",
                 r"bulletin", r"supplement"],
    "finish_schedule": [r"finish\s*schedule", r"color\s*schedule", r"paint\s*schedule",
                        r"color\s*board"],
    "schedule": [r"schedule", r"timeline", r"milestone", r"phasing"],
}


def classify_document(filename):
    """Classify a document by type based on filename."""
    name_lower = filename.lower()
    for doc_type, patterns in FILE_TYPE_PATTERNS.items():
        for pat in patterns:
            if re.search(pat, name_lower, re.IGNORECASE):
                return doc_type
    ext = Path(filename).suffix.lower()
    if ext == ".pdf":
        return "document"
    elif ext in (".xlsx", ".xls", ".csv"):
        return "spreadsheet"
    return "other"


# === Load bids ===
def load_active_bids():
    if not BIDS_FILE.exists():
        print(f"  ERROR: {BIDS_FILE} not found")
        return []
    return json.load(open(BIDS_FILE, encoding="utf-8"))


# === Fuzzy project matching ===
def fuzzy_find_project(query, bids):
    """Find the best matching project in bids list. Returns (best_match, top_candidates).

    Strict mode: short tokens (<=4 chars) must match as whole words, not substrings.
    Prevents "ARA" matching "Carvana Adesa" or "AZ" matching "Plaza".
    """
    import re as _re
    if not query or not bids:
        return None, []

    query_lower = query.lower().strip()
    query_words = query_lower.split()
    short_tokens = [w for w in query_words if 2 <= len(w) <= 4 and w.isalpha()]
    scored = []

    for bid in bids:
        name = bid.get("project_name", "")
        name_lower = name.lower()
        # Word-boundary tokenization for whole-word matching
        name_words = set(_re.findall(r"[a-z0-9]+", name_lower))

        # SequenceMatcher ratio
        ratio = difflib.SequenceMatcher(None, query_lower, name_lower).ratio()

        # Substring boost — but only for the FULL query, not pieces
        if query_lower in name_lower:
            ratio += 0.35
        elif len(query_lower) >= 8 and name_lower in query_lower:
            ratio += 0.20

        # Whole-word match boost (not substring) for short tokens
        if short_tokens:
            whole_matches = sum(1 for w in short_tokens if w in name_words)
            ratio += 0.30 * (whole_matches / len(short_tokens))
            # Penalty if a short token is NOT a whole word in the name (likely false positive)
            if whole_matches == 0 and any(w in name_lower for w in short_tokens):
                ratio -= 0.40

        # Partial-word match for longer query words
        long_words = [w for w in query_words if len(w) > 4]
        if long_words:
            partial = sum(1 for w in long_words if w in name_lower)
            ratio += 0.20 * (partial / len(long_words))

        scored.append((ratio, bid))

    scored.sort(key=lambda x: -x[0])
    top = scored[:5]

    best_score, best_bid = top[0] if top else (0, None)
    if best_score < 0.45:
        return None, top

    return best_bid, top


# === List bids due tomorrow ===
def list_due_tomorrow(bids):
    tomorrow = date.today() + timedelta(days=1)
    if sys.platform == "win32":
        tomorrow_str = tomorrow.strftime("%#m/%#d/%Y")
    else:
        tomorrow_str = tomorrow.strftime("%-m/%-d/%Y")

    return [b for b in bids if b.get("due_date") == tomorrow_str], tomorrow_str


# === Slugify project name ===
def slugify(name):
    """Convert project name to a filesystem-friendly slug."""
    s = name.lower().strip()
    s = re.sub(r'[^a-z0-9\s-]', '', s)
    s = re.sub(r'[\s]+', '-', s)
    s = re.sub(r'-+', '-', s)
    return s[:80].strip('-')


# === Create project directory ===
def ensure_project_dir(bid):
    """Create project directory structure. Returns (slug, project_dir, bid_docs_dir)."""
    slug = slugify(bid["project_name"])
    project_dir = PROJECTS_DIR / slug
    bid_docs_dir = project_dir / "bid_docs"
    bid_docs_dir.mkdir(parents=True, exist_ok=True)

    # Save project metadata
    meta_file = project_dir / "project.json"
    if not meta_file.exists():
        meta = {
            "id": slug,
            "name": bid["project_name"],
            "gc": bid.get("gc", ""),
            "city": bid.get("city", ""),
            "state": bid.get("state", ""),
            "due_date": bid.get("due_date", ""),
            "source": bid.get("source", ""),
            "portal_url": bid.get("portal_url", ""),
            "distance_miles": bid.get("distance_miles"),
            "status": "ingest",
            "created_at": datetime.now().isoformat(),
        }
        with open(meta_file, "w") as f:
            json.dump(meta, f, indent=2)

    return slug, project_dir, bid_docs_dir


# === Download documents from ConstructConnect (SmartBid) ===
async def download_cc_documents(portal_url, output_dir):
    """Download documents from CC SmartBid Plan Room using Playwright."""
    try:
        # Use the dedicated SmartBid downloader
        smartbid_script = BASE_DIR / "scripts" / "download_cc_smartbid.py"
        if smartbid_script.exists():
            from download_cc_smartbid import download_smartbid_documents
            print(f"  Downloading from ConstructConnect SmartBid...")
            print(f"  Portal: {portal_url}")
            result = await download_smartbid_documents(portal_url, str(output_dir))
            return result
        else:
            # Fallback to old method
            from constructconnect_client import browser_download_documents
            print(f"  Downloading from ConstructConnect...")
            print(f"  Portal: {portal_url}")
            result = await browser_download_documents(portal_url, str(output_dir))
            return result
    except ImportError as e:
        return {"error": f"CC download module not available: {e}"}
    except Exception as e:
        return {"error": f"CC download failed: {str(e)}"}


# === Download documents from BuildingConnected (browser) ===
async def download_bc_documents(bid_info, output_dir):
    """Download documents from BC using Playwright browser automation.

    bid_info: str (project_name for legacy) or dict with opportunity_id/portal_url
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"error": "Playwright not installed. Run: pip install playwright"}

    bc_auth = CONFIG_DIR / "bc_auth.json"
    bc_state_f = CONFIG_DIR / "bc_storage_state.json"
    if not bc_auth.exists() and not bc_state_f.exists():
        return {"error": "No BC auth — run scripts/bc_login_capture.py (saves bc_storage_state.json)"}
    config = json.load(open(bc_auth)) if bc_auth.exists() else {}
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    downloaded = []

    # Extract opportunity info
    if isinstance(bid_info, dict):
        opportunity_id = bid_info.get("opportunity_id", "")
        portal_url = bid_info.get("portal_url", "")
        project_name = bid_info.get("project_name", "unknown")
    else:
        opportunity_id = ""
        portal_url = ""
        project_name = bid_info

    have_session = bc_state_f.exists()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        _ctx_kw = dict(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0.0.0",
            viewport={"width": 1600, "height": 900},
            accept_downloads=True,   # required so the page-download path works for normal-size
                                     # BC sets (Taco Bell etc.). Giant ZIPs that crash the tab
                                     # fall back to the sniffed presigned-url stream below.
        )
        if have_session:
            _ctx_kw["storage_state"] = str(bc_state_f)   # captured login from bc_login_capture.py
        ctx = await browser.new_context(**_ctx_kw)
        page = await ctx.new_page()
        # NETWORK-SNIFF FALLBACK: BC's "Download All" fetches a signed S3 zip
        # (content-type application/zip, url …/archives/<uuid>-files-archive.zip).
        # Capture it so that even if the page download event misbehaves we can pull
        # the ZIP directly via the API request context (carries the session).
        _bc_zip = {"url": None}
        _bc_api = {"resp": None}    # the /files/download-all XHR (JSON holds the presigned url)
        def _sniff_req(req):
            try:
                if "files-archive.zip" in req.url:
                    _bc_zip["url"] = req.url   # full AWS-presigned URL (no cookies needed)
            except Exception:
                pass
        def _sniff_resp(resp):
            try:
                u = resp.url
                if "files-archive.zip" in u or "application/zip" in resp.headers.get("content-type", ""):
                    _bc_zip["url"] = u
                if "files/download-all" in u and resp.status == 200:
                    _bc_api["resp"] = resp     # read its JSON after the clicks
            except Exception:
                pass
        ctx.on("request", _sniff_req)
        ctx.on("response", _sniff_resp)

        try:
            if have_session:
                print("  Using captured BC session (bc_storage_state.json) — already logged in")
            else:
                # Legacy email/password login (BC Autodesk SSO — unreliable; prefer the captured session)
                print(f"  Logging into BuildingConnected...")
                await page.goto("https://app.buildingconnected.com/login", timeout=30000)
                await asyncio.sleep(3)
                await page.fill("#emailField", config["email"])
                await page.click('button:has-text("NEXT")')
                await asyncio.sleep(4)
                pwd = await page.query_selector("#passwordField, input[type=password]")
                if pwd:
                    await pwd.fill(config["password"])
                    btns = await page.query_selector_all("button")
                    for btn in btns:
                        txt = (await btn.inner_text()).strip().upper()
                        if "SIGN" in txt or "LOG" in txt or "NEXT" in txt:
                            await btn.click()
                            break
                    await asyncio.sleep(8)

            # Navigate directly to opportunity files page if we have the ID
            # EXPIRED-SESSION GUARD (6/23): a stale captured session loads fine but
            # BC redirects to its login page on navigation. Detect that and report
            # it clearly instead of silently returning "0 files" (which once read as
            # a scraper bug and wrongly blamed the user's recent capture).
            _BC_EXPIRED = {"error": "BC session EXPIRED — the captured session redirected to "
                           "the BC login page. Re-run: python scripts/bc_login_capture.py "
                           "(log in, it re-saves bc_storage_state.json), then retry.",
                           "expired": True, "total": 0, "downloaded": []}
            if portal_url:
                print(f"  Navigating directly to: {portal_url}")
                await page.goto(portal_url, timeout=30000)
                await asyncio.sleep(5)
                if "login" in (page.url or "").lower():
                    print("  [!] BC redirected to LOGIN — captured session is expired.")
                    await browser.close(); return _BC_EXPIRED
            elif opportunity_id:
                files_url = f"https://app.buildingconnected.com/opportunities/{opportunity_id}/files"
                print(f"  Navigating directly to: {files_url}")
                await page.goto(files_url, timeout=30000)
                await asyncio.sleep(5)
                if "login" in (page.url or "").lower():
                    print("  [!] BC redirected to LOGIN — captured session is expired.")
                    await browser.close(); return _BC_EXPIRED
            else:
                # Legacy fallback: search bid board by name
                print(f"  No opportunity ID — searching bid board for: {project_name}")
                rows = await page.query_selector_all("[role=row]")
                target_row = None
                for row in rows:
                    text = (await row.inner_text()).strip().lower()
                    if project_name.lower()[:30] in text:
                        target_row = row
                        break

                if not target_row:
                    search = await page.query_selector('input[placeholder*="Search"], input[type="search"]')
                    if search:
                        await search.fill(project_name)
                        await asyncio.sleep(3)
                        rows = await page.query_selector_all("[role=row]")
                        for row in rows:
                            text = (await row.inner_text()).strip().lower()
                            if project_name.lower()[:20] in text:
                                target_row = row
                                break

                if not target_row:
                    await page.screenshot(path=str(output_path / "_bc_search_debug.png"))
                    return {"error": f"Project '{project_name}' not found on BC bid board. Try re-scraping to get opportunity ID.", "downloaded": []}

                link = await target_row.query_selector("a")
                if link:
                    await link.click()
                    await asyncio.sleep(5)

                # Click Files tab
                for tab_text in ["Files", "Documents", "Attachments", "Docs"]:
                    tab = await page.query_selector(f'[role="tab"]:has-text("{tab_text}"), a:has-text("{tab_text}"), button:has-text("{tab_text}")')
                    if tab:
                        await tab.click()
                        await asyncio.sleep(3)
                        break

            # Wait for the BC files table to fully render before clicking anything.
            # BC's React app loads progressively; clicking "Download All" too early
            # leaves the button registered but the click handler not bound yet.
            print(f"  Waiting for BC files page to fully render...")
            for tick in range(15):  # up to 30s
                await asyncio.sleep(2)
                try:
                    body = await page.inner_text("body")
                    # Look for evidence the file table has rendered
                    if ("Download All" in body and
                        ("Date Modified" in body or "Indicator" in body or "Size" in body)):
                        print(f"  Files table rendered after ~{(tick+1)*2}s")
                        break
                except Exception:
                    pass

            # Now on the files page — try "Download All" button first
            print(f"  Looking for downloadable files...")
            # Try multiple selector strategies. BC's button is typically a <button> with
            # exact text "Download All" — prefer that, then fall back to broader matches.
            dl_all = (await page.query_selector('button:text-is("Download All")')
                      or await page.query_selector('button:has-text("Download All")')
                      or await page.query_selector('[aria-label="Download All"]')
                      or await page.query_selector('a:has-text("Download All")'))
            if dl_all:
                try:
                    try:
                        await dl_all.scroll_into_view_if_needed(timeout=5000)
                    except Exception:
                        pass
                    print(f"  Clicking Download All button...")
                    # Overlay-proof click: normal -> force -> dispatch_event.
                    try:
                        await dl_all.click(timeout=8000)
                    except Exception:
                        try:
                            await dl_all.click(force=True, timeout=8000)
                        except Exception:
                            await dl_all.dispatch_event('click')
                    await asyncio.sleep(3)
                    # Confirm "OK, go for it!" dialog.
                    confirm_btn = await page.query_selector(
                        'button:has-text("go for it"), button:has-text("OK"), '
                        'button:has-text("Confirm"), button:has-text("Yes"), button:has-text("Continue")')
                    # PRIMARY: page download + save_as — proven for normal-size BC sets
                    # (Taco Bell pulled 9 docs this way). Moderate timeout so a giant ZIP
                    # can't hang; if it crashes/times out we fall back to the sniffed url.
                    if confirm_btn:
                        print(f"  Confirming 'OK, go for it!'...")
                        try:
                            async with page.expect_download(timeout=240000) as _di:
                                try:
                                    await confirm_btn.click(timeout=8000)
                                except Exception:
                                    await confirm_btn.dispatch_event('click')
                            _dl = await _di.value
                            _fp = output_path / _dl.suggested_filename
                            await _dl.save_as(str(_fp))
                            downloaded.append({"name": _dl.suggested_filename, "path": str(_fp),
                                               "size_kb": round(_fp.stat().st_size / 1024, 1)})
                            print(f"  Downloaded: {_dl.suggested_filename}")
                        except Exception as _de:
                            print(f"  page-download path failed ({str(_de)[:50]}); trying sniffed url...")
                    # FALLBACK for giant ZIPs that crash the tab: poll for BC's presigned url
                    # (captured by the request/response/api sniffers), extract from API JSON if needed.
                    if not downloaded:
                        print(f"  Waiting for BC's signed S3 url (fallback, up to 6 min)...")
                        for _ in range(72):
                            if _bc_zip["url"] or _bc_api["resp"]:
                                break
                            await asyncio.sleep(5)
                        if not _bc_zip["url"] and _bc_api["resp"]:
                            try:
                                blob = json.dumps(await _bc_api["resp"].json())
                                m = re.search(r'https://[^"\\\s]+files-archive\.zip[^"\\\s]*', blob)
                                if m:
                                    _bc_zip["url"] = m.group(0)
                                    print(f"  Got presigned url from download-all API JSON")
                            except Exception as _je:
                                print(f"  Could not parse download-all JSON: {_je}")
                except Exception as e:
                    print(f"  Download All click step failed: {e}")

            # Stream the AWS-presigned S3 zip straight to disk with a plain HTTP client
            # (presigned = no cookies needed), then extract. Any size, never crashes the tab.
            if not downloaded and _bc_zip["url"]:
                zip_url = _bc_zip["url"]
                try:
                    import requests, zipfile
                    fn = (f"{project_name[:60]}-files-archive.zip").replace("/", "-").replace("\\", "-")
                    fp = output_path / fn
                    print(f"  Streaming signed S3 zip to disk...")
                    with requests.get(zip_url, stream=True, timeout=1200) as r:
                        r.raise_for_status()
                        with open(fp, "wb") as fh:
                            for chunk in r.iter_content(1 << 20):
                                if chunk:
                                    fh.write(chunk)
                    mb = round(fp.stat().st_size / 1e6, 1)
                    downloaded.append({"name": fn, "path": str(fp),
                                       "size_kb": round(fp.stat().st_size / 1024, 1)})
                    print(f"  Streamed {fn} ({mb} MB)")
                    try:
                        with zipfile.ZipFile(str(fp)) as z:
                            z.extractall(str(output_path))
                        print(f"  Extracted {fn}")
                    except Exception as _ze:
                        print(f"  (extract deferred to downstream: {_ze})")
                except Exception as e:
                    print(f"  Signed-URL stream failed: {e}")

            # Try individual folder downloads if Download All didn't work
            if not downloaded:
                # BC shows folders — try clicking into each folder and downloading
                folder_links = await page.query_selector_all('[data-testid*="folder"], [class*="folder"] a, [role="row"] a')
                for folder in folder_links[:10]:
                    try:
                        folder_text = (await folder.inner_text()).strip()
                        if not folder_text or folder_text in ("Name", "Indicator", "Size", "Date Modified"):
                            continue
                        await folder.click()
                        await asyncio.sleep(3)

                        # Inside folder — look for download buttons
                        inner_dl = await page.query_selector('button:has-text("Download All"), button:has-text("Download")')
                        if inner_dl:
                            try:
                                async with page.expect_download(timeout=60000) as download_info:
                                    await inner_dl.click()
                                download = await download_info.value
                                file_path = output_path / download.suggested_filename
                                await download.save_as(str(file_path))
                                downloaded.append({
                                    "name": download.suggested_filename,
                                    "path": str(file_path),
                                    "size_kb": round(file_path.stat().st_size / 1024, 1),
                                })
                                print(f"  Downloaded: {download.suggested_filename}")
                            except Exception:
                                pass

                        # Go back to files list
                        await page.go_back()
                        await asyncio.sleep(2)
                    except Exception:
                        continue

            # Fallback: try any download-like links on the page
            if not downloaded:
                download_links = await page.query_selector_all(
                    'a[href*="download"], a[href*=".pdf"], a[download], '
                    'button:has-text("Download"), [class*="download"]'
                )
                for link in download_links[:20]:
                    try:
                        async with page.expect_download(timeout=30000) as download_info:
                            await link.click()
                        download = await download_info.value
                        file_path = output_path / download.suggested_filename
                        await download.save_as(str(file_path))
                        downloaded.append({
                            "name": download.suggested_filename,
                            "path": str(file_path),
                            "size_kb": round(file_path.stat().st_size / 1024, 1),
                        })
                        print(f"  Downloaded: {download.suggested_filename}")
                    except Exception:
                        continue

            # Screenshot for debugging
            await page.screenshot(path=str(output_path / "_bc_page_debug.png"))

            # Capture project info text
            try:
                body_text = await page.inner_text("body")
                info_path = output_path / "_project_info.txt"
                with open(info_path, "w", encoding="utf-8") as f:
                    f.write(body_text[:10000])
            except Exception:
                pass

        except Exception as e:
            return {"error": f"BC browser automation failed: {str(e)}", "downloaded": downloaded}
        finally:
            await browser.close()

    return {"downloaded": downloaded, "total": len(downloaded), "method": "browser_buildingconnected"}


# === Source router ===
async def download_documents(bid, output_dir, dry_run=False, force=False):
    """Route to the correct downloader based on bid source."""
    source = bid.get("source", "")
    portal_url = bid.get("portal_url", "")

    # Check if documents are already downloaded. NOTE: --force MUST re-pull so we
    # catch ADDENDA posted after the first download (Guilford 6/25: 9 addenda were
    # invisible because this guard skipped forever once the base set landed).
    output_path = Path(output_dir)
    if output_path.exists() and not force:
        existing_files = [
            f for f in output_path.iterdir()
            if f.is_file() and not f.name.startswith("_")  # skip debug files
        ]
        if existing_files:
            # Auto-organize if not done yet (extract zips, copy PDFs to drawings/)
            project_dir = output_path.parent
            drawings_dir = project_dir / "drawings"
            if not drawings_dir.exists() or not list(drawings_dir.glob("*.pdf")):
                post_download_organize(output_path, project_dir)

            manifest = []
            for f in existing_files:
                manifest.append({
                    "name": f.name,
                    "path": str(f),
                    "size_kb": round(f.stat().st_size / 1024, 1),
                })
            total_mb = round(sum(f["size_kb"] for f in manifest) / 1024, 1)

            # Include drawings info
            drawing_pdfs = list(drawings_dir.glob("*.pdf")) if drawings_dir.exists() else []

            print(f"  Documents already downloaded ({len(existing_files)} files, {total_mb} MB). Skipping re-download.")
            if drawing_pdfs:
                print(f"  Drawings ready: {len(drawing_pdfs)} PDFs in {drawings_dir}")
            return {
                "downloaded": manifest,
                "total": len(manifest),
                "method": "already_exists",
                "drawings_dir": str(drawings_dir),
                "drawings_count": len(drawing_pdfs),
                "note": f"Files were previously downloaded to {output_dir}",
            }

    if dry_run:
        return {
            "dry_run": True,
            "source": source,
            "portal_url": portal_url,
            "would_download_to": str(output_dir),
        }

    if source == "constructconnect":
        if not portal_url:
            return {"error": "No portal_url for this CC bid. Re-run scrape_cc_inbox.py to get URLs."}
        return await download_cc_documents(portal_url, output_dir)

    elif source == "buildingconnected":
        return await download_bc_documents(bid, output_dir)

    elif source == "email":
        return await download_email_documents(bid, output_dir)

    elif source == "parkway_portal":
        return await download_parkway_documents(bid, output_dir)

    elif source == "procore":
        if portal_url and "download_zip" in portal_url:
            return await download_procore_documents(portal_url, output_dir)
        return {"error": "Procore board entry without a planroom zip URL — "
                "fetch via the invite email instead", "downloaded": []}

    else:
        return {"error": f"Unknown source: {source}"}


# === Download documents from an email INVITE (iSqFt access-key / attachments) ===
async def download_email_documents(bid, output_dir):
    """Fetch docs for an email-invite project. Invites rarely attach drawings —
    they link out. Dominant case: iSqFt passwordless access link, which the
    ConstructConnect login carries (proven 6/20). Falls back to direct
    attachments; records Procore/BC/Dropbox links when we can't auto-fetch."""
    try:
        from _lib import email_invite, gmail
    except ImportError as e:
        return {"error": f"email_invite module unavailable: {e}", "downloaded": []}

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    pname = bid.get("project_name", "")
    gc = bid.get("gc", "")

    try:
        with gmail.connect() as M:
            sig = email_invite.discover(M, pname, gc)
    except Exception as e:
        return {"error": f"could not search inbox for invite: {e}", "downloaded": []}

    # Persist what we found (links, etc.) for traceability — never lose a doc host
    try:
        (output_path.parent / "_email_invite.json").write_text(
            json.dumps(email_invite.serializable(sig), indent=2), encoding="utf-8")
    except Exception:
        pass

    if not sig.get("found_email") and not sig.get("isqft_url") and not sig.get("attachments"):
        return {"error": f"no invite email with fetchable docs found for '{pname}' "
                f"(searched {sig.get('candidates_seen', 0)} candidates)", "downloaded": []}

    # 1) Direct attachments (rare, but free) — save the bytes we already pulled
    downloaded = []
    for fn, payload in sig.get("attachments", []):
        try:
            fp = output_path / re.sub(r"[^\w.\- ]", "_", fn)
            fp.write_bytes(payload)
            downloaded.append({"name": fn, "path": str(fp),
                               "size_kb": round(len(payload) / 1024, 1)})
            print(f"  SAVED attachment: {fn} ({len(payload)//1024} KB)")
        except Exception as e:
            print(f"  attachment save failed ({fn}): {e}")

    # 2) iSqFt access link → reuse the CC SmartBid downloader (CC login carries the token)
    if sig.get("isqft_url"):
        print(f"  iSqFt access link found (ProjectID={sig.get('isqft_project_id')}). "
              f"Fetching via ConstructConnect session...")
        try:
            from download_cc_smartbid import download_smartbid_documents
            res = await download_smartbid_documents(sig["isqft_url"], str(output_dir))
            res_dl = res.get("downloaded") or []
            if res_dl:
                return {"downloaded": res_dl + downloaded, "total": len(res_dl) + len(downloaded),
                        "method": "isqft_access_key", "isqft_project_id": sig.get("isqft_project_id")}
            if downloaded:
                return {"downloaded": downloaded, "total": len(downloaded), "method": "attachments"}
            return {"error": f"iSqFt access link found but no docs downloaded "
                    f"(key may be expired): {res.get('error', '')}",
                    "isqft_url": sig["isqft_url"], "downloaded": []}
        except Exception as e:
            return {"error": f"iSqFt fetch failed: {e}", "isqft_url": sig["isqft_url"],
                    "downloaded": downloaded}

    # 3) Procore planroom → download the ZIP via the stored Procore session
    if sig.get("procore_url"):
        print("  Procore planroom link found. Fetching via stored Procore session...")
        res = await download_procore_documents(sig["procore_url"], output_dir)
        res_dl = res.get("downloaded") or []
        if res_dl:
            return {"downloaded": res_dl + downloaded, "total": len(res_dl) + len(downloaded),
                    "method": "procore_session"}
        if downloaded:
            return {"downloaded": downloaded, "total": len(downloaded), "method": "attachments"}
        return {"error": f"Procore link found but no docs downloaded: {res.get('error', '')}",
                "procore_url": sig["procore_url"], "downloaded": []}

    if downloaded:
        return {"downloaded": downloaded, "total": len(downloaded), "method": "attachments"}

    # 4) Hosts without an auto path — record the link for routing/manual
    if sig.get("bc_url"):
        return {"error": "docs on BuildingConnected — recorded link (needs BC opportunity flow)",
                "bc_url": sig["bc_url"], "downloaded": []}
    if sig.get("dropbox") or sig.get("gdrive"):
        return {"error": "docs on external file host (Dropbox/Google) — recorded link(s)",
                "links": (sig.get("dropbox") or []) + (sig.get("gdrive") or []), "downloaded": []}

    return {"error": f"invite email found but no recognizable doc host for '{pname}'",
            "downloaded": []}


# === Download documents from a Procore planroom (stored session) ===
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0.0.0"


async def download_procore_documents(zip_url, output_dir):
    """Download a Procore planroom ZIP using the session captured by
    scrape_procore_portal.py (procore_state/state.json). Proven 6/20: pulled the
    full 66-file Food Lion Dinwiddie set. Navigating the download_zip URL triggers
    a file download (goto raises 'Download is starting' — expected, swallow it)."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"error": "Playwright not installed", "downloaded": []}
    state = CONFIG_DIR / "procore_state" / "state.json"
    if not state.exists():
        return {"error": "no Procore session — run scripts/scrape_procore_portal.py --setup",
                "downloaded": []}
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    downloaded = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(storage_state=str(state), accept_downloads=True,
                                        user_agent=_UA)
        page = await ctx.new_page()
        try:
            async with page.expect_download(timeout=300_000) as di:
                try:
                    await page.goto(zip_url, timeout=60000)
                except Exception:
                    pass  # goto raises "Download is starting" when the URL is a file
            dl = await di.value
            fn = dl.suggested_filename
            fp = output_path / fn
            await dl.save_as(str(fp))
            downloaded.append({"name": fn, "path": str(fp),
                               "size_kb": round(fp.stat().st_size / 1024, 1)})
            print(f"  DOWNLOADED: {fn} ({fp.stat().st_size // 1024} KB)")
        except Exception as e:
            await browser.close()
            return {"error": f"Procore download failed (session expired? re-run --setup): "
                    f"{str(e)[:160]}", "downloaded": []}
        finally:
            try:
                await browser.close()
            except Exception:
                pass
    import zipfile
    for zf in output_path.glob("*.zip"):
        try:
            with zipfile.ZipFile(str(zf)) as z:
                z.extractall(str(output_path))
                for n in z.namelist():
                    ex = output_path / n
                    if ex.is_file():
                        downloaded.append({"name": n, "path": str(ex),
                                           "size_kb": round(ex.stat().st_size / 1024, 1)})
        except Exception as e:
            print(f"  ZIP extract failed: {e}")
    return {"downloaded": downloaded, "total": len(downloaded), "method": "procore_session"}


# === Download documents from Parkway's private portal (login + Documents tab) ===
async def download_parkway_documents(bid, output_dir):
    """Download a project's docs from parkwayconstructionplans.com. Logs in
    (parkway_auth.json — SSL cert expired, so ignore_https_errors), finds the
    project row on the Bidding Projects board, opens its dashboard (the row's
    arrow button → #projects/<id>/dashboard), goes to the Documents tab
    (#projects/<id>/attachments) and clicks Download All. Proven flow 6/20."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"error": "Playwright not installed", "downloaded": []}
    auth = CONFIG_DIR / "parkway_auth.json"
    if not auth.exists():
        return {"error": "no parkway_auth.json", "downloaded": []}
    cfg = json.loads(auth.read_text(encoding="utf-8"))
    pname = bid.get("project_name", "")
    # match on the project name minus the trailing "- City, ST"
    key = re.sub(r"\s*[-–—]\s*[A-Za-z .']+,\s*[A-Z]{2}\s*$", "", pname).strip().lower() or pname.lower()
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    downloaded = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(ignore_https_errors=True, accept_downloads=True,
                                        user_agent=_UA)
        page = await ctx.new_page()
        try:
            await page.goto(cfg["url"], timeout=40000)
            await page.fill('input[placeholder="Enter Username"]', cfg["username"])
            pwf = page.locator('input[placeholder="Enter Password"]')
            await pwf.fill(cfg["password"])
            await pwf.press("Enter")
            try:
                await page.wait_for_selector("table tbody tr", timeout=25000)
            except Exception:
                pass
            await page.goto("https://parkwayconstructionplans.com/#p/projects/bidding",
                            timeout=25000, wait_until="networkidle")
            await page.wait_for_timeout(2500)

            proj_id = None
            for _pg in range(1, 6):                       # walk pagination
                rows = await page.query_selector_all("table tbody tr")
                target = None
                for r in rows:
                    txt = (await r.inner_text()).lower()
                    if pname.lower()[:18] in txt or (len(key) > 6 and key[:16] in txt):
                        target = r
                        break
                if target:
                    await target.hover()
                    await page.wait_for_timeout(500)
                    clicks = await target.query_selector_all('a,button,[role="button"]')
                    if clicks:
                        try:
                            await clicks[-1].click(timeout=6000)   # the row's open-arrow
                        except Exception:
                            await target.dblclick()
                    await page.wait_for_timeout(4500)
                    m = re.search(r"#projects/(\d+)", page.url)
                    proj_id = m.group(1) if m else None
                    break
                nxt = await page.query_selector('a:has-text("Next"):not(.disabled), '
                                                'button:has-text("Next"):not([disabled])')
                if nxt:
                    await nxt.click()
                    await page.wait_for_timeout(1800)
                else:
                    break
            if not proj_id:
                await browser.close()
                return {"error": f"Parkway project '{pname}' not found on bidding board",
                        "downloaded": []}

            await page.goto(f"https://www.parkwayconstructionplans.com/#projects/{proj_id}/attachments",
                            timeout=25000, wait_until="networkidle")
            await page.wait_for_timeout(4000)
            dl_all = await page.query_selector('button:has-text("Download All"), a:has-text("Download All")')
            if dl_all:
                try:
                    async with page.expect_download(timeout=300_000) as di:
                        await dl_all.click()
                        await page.wait_for_timeout(2000)
                    dl = await di.value
                    fn = dl.suggested_filename
                    fp = output_path / fn
                    await dl.save_as(str(fp))
                    downloaded.append({"name": fn, "path": str(fp),
                                       "size_kb": round(fp.stat().st_size / 1024, 1)})
                    print(f"  DOWNLOADED: {fn} ({fp.stat().st_size // 1024} KB)")
                except Exception as e:
                    print(f"  Download All failed: {str(e)[:120]}")
            if not downloaded:                            # fallback: individual files
                for link in await page.query_selector_all('a[href*=".pdf" i], a[href*="download" i]'):
                    try:
                        async with page.expect_download(timeout=60000) as di:
                            await link.click()
                        dl = await di.value
                        fp = output_path / dl.suggested_filename
                        await dl.save_as(str(fp))
                        downloaded.append({"name": dl.suggested_filename, "path": str(fp),
                                           "size_kb": round(fp.stat().st_size / 1024, 1)})
                    except Exception:
                        continue
        except Exception as e:
            try:
                await browser.close()
            except Exception:
                pass
            return {"error": f"Parkway fetch failed: {str(e)[:160]}", "downloaded": downloaded}
        finally:
            try:
                await browser.close()
            except Exception:
                pass
    import zipfile
    for zf in output_path.glob("*.zip"):
        try:
            with zipfile.ZipFile(str(zf)) as z:
                z.extractall(str(output_path))
                for n in z.namelist():
                    ex = output_path / n
                    if ex.is_file():
                        downloaded.append({"name": n, "path": str(ex),
                                           "size_kb": round(ex.stat().st_size / 1024, 1)})
        except Exception as e:
            print(f"  ZIP extract failed: {e}")
    return {"downloaded": downloaded, "total": len(downloaded), "method": "parkway_session"}


# === Post-download: extract zips and organize drawings ===
def post_download_organize(bid_docs_dir, project_dir):
    """Extract any ZIP files and copy plan/drawing PDFs to drawings/ folder.

    This ensures Carol always finds plans in a consistent location:
      data/projects/<slug>/drawings/*.pdf

    Skips survey photos and other large non-plan files to save disk space.
    """
    import zipfile
    import shutil

    bid_docs_path = Path(bid_docs_dir)
    drawings_dir = Path(project_dir) / "drawings"

    # Step 1: Extract any ZIP files in bid_docs/
    zip_files = list(bid_docs_path.glob("*.zip"))
    if zip_files:
        for zf in zip_files:
            try:
                with zipfile.ZipFile(str(zf), 'r') as z:
                    for member in z.namelist():
                        # Skip directories
                        if member.endswith('/'):
                            continue
                        filename = os.path.basename(member)
                        if not filename:
                            continue

                        # Skip large photo folders (survey photos, site photos)
                        member_lower = member.lower()
                        skip_patterns = ['survey photo', 'site photo', 'construction photo',
                                         'progress photo', 'existing photo']
                        if any(pat in member_lower for pat in skip_patterns):
                            # Still extract if it's a PDF (might be useful)
                            if not filename.lower().endswith('.pdf'):
                                continue

                        # Extract to bid_docs/ with flat structure
                        target = bid_docs_path / filename
                        if not target.exists():
                            with z.open(member) as src, open(target, 'wb') as dst:
                                dst.write(src.read())

                print(f"  Extracted: {zf.name}")
            except Exception as e:
                print(f"  WARNING: Failed to extract {zf.name}: {e}")

    # Step 2: Copy plan/drawing PDFs to drawings/ folder
    pdf_files = list(bid_docs_path.glob("**/*.pdf"))
    if pdf_files:
        drawings_dir.mkdir(parents=True, exist_ok=True)
        copied = 0
        for pdf in pdf_files:
            target = drawings_dir / pdf.name
            if not target.exists():
                shutil.copy2(pdf, target)
                copied += 1
        if copied:
            print(f"  Organized: {copied} PDFs copied to drawings/")

    return {
        "zips_extracted": len(zip_files),
        "drawings_organized": len(list(drawings_dir.glob("*.pdf"))) if drawings_dir.exists() else 0,
        "drawings_dir": str(drawings_dir),
    }


# === Classify downloaded files ===
def classify_downloads(bid_docs_dir, slug, source):
    """Classify all files in bid_docs/ and save manifest."""
    manifest = []
    for f in bid_docs_dir.iterdir():
        if f.name.startswith("_"):  # skip debug files
            continue
        if f.is_file():
            doc_type = classify_document(f.name)
            manifest.append({
                "name": f.name,
                "path": str(f),
                "size_kb": round(f.stat().st_size / 1024, 1),
                "doc_type": doc_type,
            })

    # Save manifest
    manifest_data = {
        "project": slug,
        "source": source,
        "downloaded_at": datetime.now().isoformat(),
        "documents": manifest,
        "summary": {
            "total": len(manifest),
            "by_type": {},
        }
    }
    for doc in manifest:
        dt = doc.get("doc_type", "other")
        manifest_data["summary"]["by_type"][dt] = manifest_data["summary"]["by_type"].get(dt, 0) + 1

    manifest_path = bid_docs_dir.parent / "doc_manifest.json"
    with open(manifest_path, "w") as f:
        json.dump(manifest_data, f, indent=2)

    return manifest_data


# === Parse all PDFs for painting scope ===
def parse_all_pdfs(bid_docs_dir, slug):
    """Parse all PDFs in bid_docs/ and extract painting scope."""
    try:
        from parse_pdf import extract_text, extract_scope_sections
    except ImportError:
        print("  WARNING: parse_pdf not available. Skipping scope extraction.")
        return None

    scope_results = []
    combined_lines = []

    pdf_files = list(bid_docs_dir.glob("**/*.pdf"))
    if not pdf_files:
        # Check for ZIP files that might contain PDFs
        zip_files = list(bid_docs_dir.glob("*.zip"))
        if zip_files:
            print(f"  Found {len(zip_files)} ZIP files. Extracting...")
            import zipfile
            for zf in zip_files:
                try:
                    with zipfile.ZipFile(str(zf), 'r') as z:
                        z.extractall(str(bid_docs_dir))
                except Exception as e:
                    print(f"  WARNING: Failed to extract {zf.name}: {e}")
            pdf_files = list(bid_docs_dir.glob("**/*.pdf"))

    print(f"  Parsing {len(pdf_files)} PDF files for painting scope...")

    for pdf_path in pdf_files:
        try:
            result = extract_text(str(pdf_path), pages="all")
            if "error" in result:
                print(f"    SKIP: {pdf_path.name} - {result['error']}")
                continue

            full_text = "\n".join(p.get("text", "") for p in result.get("pages", []))
            if not full_text.strip():
                print(f"    SKIP: {pdf_path.name} - no text extracted (may be scanned/image PDF)")
                continue

            scope = extract_scope_sections(full_text)
            doc_type = classify_document(pdf_path.name)

            painting_lines = scope.get("painting_relevant_lines", [])
            if painting_lines:
                print(f"    FOUND: {pdf_path.name} - {len(painting_lines)} painting scope lines")
            else:
                print(f"    SCAN:  {pdf_path.name} - no painting keywords found")

            scope_results.append({
                "file": pdf_path.name,
                "doc_type": doc_type,
                "pages": result.get("total_pages", 0),
                "painting_relevant_lines": painting_lines,
                "sections": scope.get("sections", {}),
            })
            combined_lines.extend(painting_lines)

        except Exception as e:
            print(f"    ERROR: {pdf_path.name} - {e}")

    # Save scope extract
    scope_data = {
        "project": slug,
        "extracted_at": datetime.now().isoformat(),
        "files_parsed": len(scope_results),
        "total_painting_lines": len(combined_lines),
        "scope_items": scope_results,
        "combined_painting_lines": list(set(combined_lines)),  # deduplicate
    }

    scope_path = bid_docs_dir.parent / "scope_extract.json"
    with open(scope_path, "w", encoding="utf-8") as f:
        json.dump(scope_data, f, indent=2, ensure_ascii=False)

    return scope_data


# === Print summary ===
def print_summary(bid, slug, manifest, scope):
    W = 70
    print(f"\n{'=' * W}")
    print(f"  PROJECT DOCUMENTS FETCHED - Carolina Commercial Finishes")
    print(f"{'=' * W}")
    print(f"  Project:  {bid['project_name']}")
    print(f"  GC:       {bid.get('gc', 'Unknown')}")
    print(f"  Source:   {bid.get('source', '')}")
    print(f"  Due:      {bid.get('due_date', '')}")
    print(f"  Location: {bid.get('city', '')}, {bid.get('state', '')[:2] if bid.get('state') else ''}")
    d = bid.get('distance_miles')
    if d:
        print(f"  Distance: {d:.0f} mi from Monroe")
    print(f"  Folder:   data/projects/{slug}/")
    print(f"  {'-' * (W - 4)}")

    if manifest:
        docs = manifest.get("documents", [])
        summary = manifest.get("summary", {})
        print(f"  DOCUMENTS DOWNLOADED: {summary.get('total', 0)}")
        for doc in docs:
            size = doc.get("size_kb", 0)
            dtype = doc.get("doc_type", "other")
            print(f"    [{dtype:<14s}] {doc['name'][:45]} ({size:.0f} KB)")
        by_type = summary.get("by_type", {})
        if by_type:
            type_str = ", ".join(f"{v} {k}" for k, v in by_type.items())
            print(f"  Breakdown: {type_str}")
    else:
        print(f"  DOCUMENTS: None downloaded")

    print(f"  {'-' * (W - 4)}")

    if scope:
        total_lines = scope.get("total_painting_lines", 0)
        files_parsed = scope.get("files_parsed", 0)
        print(f"  PAINTING SCOPE: {total_lines} relevant lines across {files_parsed} files")
        combined = scope.get("combined_painting_lines", [])
        for line in combined[:10]:  # show first 10
            print(f"    - {line[:70]}")
        if len(combined) > 10:
            print(f"    ... and {len(combined) - 10} more lines")
        if total_lines == 0:
            print(f"  NOTE: No painting keywords found. Documents may be scanned images")
            print(f"        or scope may be in drawings rather than text specs.")
    else:
        print(f"  SCOPE PARSING: Skipped (no PDFs or parser unavailable)")

    print(f"{'=' * W}")


# === Main ===
def main():
    parser = argparse.ArgumentParser(description="CCF Project Document Fetcher")
    parser.add_argument("project_name", nargs="?", help="Project name (fuzzy match)")
    parser.add_argument("--project-name", dest="project_name_flag", help="Project name (explicit)")
    parser.add_argument("--list-tomorrow", action="store_true", help="List bids due tomorrow")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen without downloading")
    parser.add_argument("--force", action="store_true", help="Re-download even if docs exist")
    args = parser.parse_args()

    project_query = args.project_name or args.project_name_flag

    print("=" * 70)
    print("  CCF PROJECT DOC FETCHER")
    print("=" * 70)

    # Load bids
    bids = load_active_bids()
    if not bids:
        print("  No active bids found.")
        return

    # List tomorrow mode
    if args.list_tomorrow:
        tomorrow_bids, date_str = list_due_tomorrow(bids)
        print(f"\n  BIDS DUE {date_str}: {len(tomorrow_bids)} projects")
        print(f"  {'-' * 66}")
        print(f"  {'#':>2s}  {'Dist':>6s}  {'Project':<35s}  {'Source':<5s}  {'GC':<20s}")
        print(f"  {'-' * 66}")
        for i, b in enumerate(sorted(tomorrow_bids, key=lambda x: x.get("distance_miles") or 999), 1):
            d = b.get("distance_miles")
            dist = f"{d:.0f} mi" if d else "? mi"
            src = "CC" if b.get("source") == "constructconnect" else "BC"
            print(f"  {i:>2d}  {dist:>6s}  {b['project_name'][:35]:<35s}  {src:<5s}  {b.get('gc', '')[:20]}")
        return

    # Need a project query
    if not project_query:
        print("  ERROR: Provide a project name to search for, or use --list-tomorrow")
        parser.print_help()
        return

    # Fuzzy match
    print(f"\n  Searching for: \"{project_query}\"")
    best, candidates = fuzzy_find_project(project_query, bids)

    if not best:
        print(f"  ERROR: No matching project found.")
        if candidates:
            print(f"  Did you mean:")
            for score, bid in candidates[:3]:
                print(f"    - {bid['project_name']} ({bid.get('gc', '')}) [{score:.0%} match]")
        result = {"status": "error", "message": "No matching project found"}
        print(f"\n__RESULT__:{json.dumps(result)}")
        return

    # Show match
    match_score = candidates[0][0] if candidates else 0
    print(f"  MATCHED: {best['project_name']} [{match_score:.0%} match]")
    print(f"  GC: {best.get('gc', 'Unknown')} | Source: {best.get('source', '')} | Due: {best.get('due_date', '')}")

    # Create project directory
    slug, project_dir, bid_docs_dir = ensure_project_dir(best)
    print(f"  Project dir: {project_dir}")

    # Check if docs already exist
    existing_files = list(bid_docs_dir.glob("*"))
    existing_files = [f for f in existing_files if not f.name.startswith("_")]
    if existing_files and not args.force:
        print(f"\n  EXISTING DOCS: {len(existing_files)} files already in bid_docs/")
        for f in existing_files[:5]:
            print(f"    - {f.name}")
        if len(existing_files) > 5:
            print(f"    ... and {len(existing_files) - 5} more")
        print(f"  Use --force to re-download.")

        # Extract any unprocessed zips and organize drawings
        post_download_organize(bid_docs_dir, project_dir)

        # Still parse existing PDFs
        manifest = classify_downloads(bid_docs_dir, slug, best.get("source", ""))
        scope = parse_all_pdfs(bid_docs_dir, slug)
        print_summary(best, slug, manifest, scope)

        result = {
            "status": "existing",
            "project": slug,
            "project_dir": str(project_dir),
            "documents_existing": len(existing_files),
            "scope_lines_found": scope.get("total_painting_lines", 0) if scope else 0,
        }
        print(f"\n__RESULT__:{json.dumps(result)}")
        return

    # Download documents
    print(f"\n  DOWNLOADING DOCUMENTS...")
    dl_result = asyncio.run(download_documents(best, bid_docs_dir, dry_run=args.dry_run, force=args.force))

    if args.dry_run:
        print(f"\n  DRY RUN - would download from:")
        print(f"    Source: {dl_result.get('source', '')}")
        print(f"    URL: {dl_result.get('portal_url', 'N/A')}")
        print(f"    To: {dl_result.get('would_download_to', '')}")
        result = {"status": "dry_run", "project": slug, **dl_result}
        print(f"\n__RESULT__:{json.dumps(result)}")
        return

    if "error" in dl_result:
        print(f"\n  DOWNLOAD ERROR: {dl_result['error']}")
        # Still check if any partial files were downloaded
        downloaded_count = len(dl_result.get("downloaded", []))
        if downloaded_count == 0:
            result = {"status": "error", "project": slug, "error": dl_result["error"]}
            print(f"\n__RESULT__:{json.dumps(result)}")
            return
        else:
            print(f"  Partial download: {downloaded_count} files saved before error")

    downloaded = dl_result.get("downloaded", [])
    print(f"  Downloaded {len(downloaded)} files")

    # Extract zips and organize drawings
    org = post_download_organize(bid_docs_dir, project_dir)

    # Classify
    manifest = classify_downloads(bid_docs_dir, slug, best.get("source", ""))

    # Parse PDFs
    scope = parse_all_pdfs(bid_docs_dir, slug)

    # Summary
    print_summary(best, slug, manifest, scope)

    # Machine-readable result
    result = {
        "status": "success",
        "project": slug,
        "project_dir": str(project_dir),
        "documents_downloaded": len(downloaded),
        "scope_lines_found": scope.get("total_painting_lines", 0) if scope else 0,
        "scope_extract_path": str(project_dir / "scope_extract.json"),
        "doc_manifest_path": str(project_dir / "doc_manifest.json"),
        "summary": f"Downloaded {len(downloaded)} documents, found {scope.get('total_painting_lines', 0) if scope else 0} painting scope lines across {scope.get('files_parsed', 0) if scope else 0} files"
    }
    print(f"\n__RESULT__:{json.dumps(result)}")


if __name__ == "__main__":
    main()
