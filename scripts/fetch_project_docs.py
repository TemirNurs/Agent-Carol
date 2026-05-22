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
    if not bc_auth.exists():
        return {"error": f"No BC auth config at {bc_auth}"}

    config = json.load(open(bc_auth))
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

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0.0.0",
            viewport={"width": 1600, "height": 900},
        )
        page = await ctx.new_page()

        try:
            # Login to BC
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
            if portal_url:
                print(f"  Navigating directly to: {portal_url}")
                await page.goto(portal_url, timeout=30000)
                await asyncio.sleep(5)
            elif opportunity_id:
                files_url = f"https://app.buildingconnected.com/opportunities/{opportunity_id}/files"
                print(f"  Navigating directly to: {files_url}")
                await page.goto(files_url, timeout=30000)
                await asyncio.sleep(5)
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
                    # Scroll into view + dispatch click. Sometimes pure .click() is
                    # intercepted by overlays — dispatch_event guarantees the handler fires.
                    try:
                        await dl_all.scroll_into_view_if_needed(timeout=5000)
                    except Exception:
                        pass
                    print(f"  Clicking Download All button...")
                    await dl_all.click()
                    await asyncio.sleep(3)

                    # BC may show a confirmation dialog. Match more variants.
                    confirm_btn = await page.query_selector(
                        'button:has-text("OK, go for it"), button:has-text("go for it"), '
                        'button:has-text("Ok"), button:has-text("OK"), '
                        'button:has-text("Confirm"), button:has-text("Yes"), '
                        'button:has-text("Download"), button:has-text("Continue")'
                    )
                    # BC ZIP prep can take 3-6 min for large plan sets; bump timeouts.
                    # Also actively poll for "Preparing Files For Download" -> "Download" transition.
                    DOWNLOAD_TIMEOUT_MS = 480_000  # 8 min — covers slow ZIP prep
                    if confirm_btn:
                        print(f"  Confirming download dialog (will wait up to 8 min for BC ZIP prep)...")
                        async with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as download_info:
                            await confirm_btn.click()
                            # Optionally watch for "Preparing Files For Download" status to log progress
                            for tick in range(48):  # 48 * 10s = 8 min
                                await asyncio.sleep(10)
                                try:
                                    body = await page.inner_text("body")
                                    if "Preparing Files For Download" in body or "preparing" in body.lower():
                                        if tick % 3 == 0:
                                            print(f"  BC still preparing files... (~{(tick+1)*10}s)")
                                    else:
                                        # No "preparing" text — either done, or never started. Break out.
                                        break
                                except Exception:
                                    break
                    else:
                        print(f"  No confirm dialog — waiting for direct download (up to 8 min)...")
                        async with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as download_info:
                            for tick in range(48):
                                await asyncio.sleep(10)
                                try:
                                    body = await page.inner_text("body")
                                    if "Preparing" in body or "preparing" in body.lower():
                                        if tick % 3 == 0:
                                            print(f"  BC still preparing files... (~{(tick+1)*10}s)")
                                    else:
                                        break
                                except Exception:
                                    break

                    download = await download_info.value
                    file_path = output_path / download.suggested_filename
                    await download.save_as(str(file_path))
                    downloaded.append({
                        "name": download.suggested_filename,
                        "path": str(file_path),
                        "size_kb": round(file_path.stat().st_size / 1024, 1),
                    })
                    print(f"  Downloaded: {download.suggested_filename}")
                except Exception as e:
                    print(f"  Download All failed: {e}")

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
async def download_documents(bid, output_dir, dry_run=False):
    """Route to the correct downloader based on bid source."""
    source = bid.get("source", "")
    portal_url = bid.get("portal_url", "")

    # Check if documents are already downloaded
    output_path = Path(output_dir)
    if output_path.exists():
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

    else:
        return {"error": f"Unknown source: {source}"}


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
    dl_result = asyncio.run(download_documents(best, bid_docs_dir, dry_run=args.dry_run))

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
