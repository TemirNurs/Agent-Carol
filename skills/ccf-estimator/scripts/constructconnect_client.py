#!/usr/bin/env python3
"""
ConstructConnect API Client + Playwright Browser Fallback
Connects to ConstructConnect to search projects and download documents.

Setup:
  1. Save credentials to data/config/cc_auth.json:
     {"username": "...", "password": "...", "api_key": "..."}
  2. For browser fallback: pip install playwright && playwright install chromium

Usage:
  python constructconnect_client.py status
  python constructconnect_client.py search --trade painting --state NC
  python constructconnect_client.py project --id <project_id>
  python constructconnect_client.py documents --id <project_id> --output <dir>
  python constructconnect_client.py browser-search --trade painting --state NC  (Playwright fallback)
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import httpx
except ImportError:
    httpx = None

CONFIG_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data" / "config"
AUTH_FILE = CONFIG_DIR / "cc_auth.json"
SESSION_FILE = CONFIG_DIR / "cc_session.json"

API_BASE = "https://api.constructconnect.com"
PORTAL_URL = "https://projects.constructconnect.com"


def _load_config():
    if not AUTH_FILE.exists():
        return None
    with open(AUTH_FILE) as f:
        return json.load(f)


def _load_session():
    if not SESSION_FILE.exists():
        return None
    with open(SESSION_FILE) as f:
        data = json.load(f)
    if data.get("expires_at", 0) < time.time():
        return None
    return data


def _save_session(session_data):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    session_data["expires_at"] = time.time() + 3600
    with open(SESSION_FILE, "w") as f:
        json.dump(session_data, f, indent=2)


# --- API Methods ---

def api_authenticate():
    """Authenticate with ConstructConnect API."""
    if httpx is None:
        return {"error": "httpx not installed. Run: pip install httpx"}

    config = _load_config()
    if not config:
        return {"error": f"No config found. Create {AUTH_FILE}",
                "template": {"username": "YOUR_EMAIL", "password": "YOUR_PASSWORD", "api_key": "YOUR_API_KEY"}}

    api_key = config.get("api_key")
    if api_key:
        _save_session({"api_key": api_key, "auth_type": "api_key"})
        return {"status": "authenticated", "method": "api_key"}

    # Try username/password auth
    try:
        response = httpx.post(f"{API_BASE}/auth/login", json={
            "username": config["username"],
            "password": config["password"],
        })
        if response.status_code == 200:
            data = response.json()
            _save_session({"token": data.get("token"), "auth_type": "token"})
            return {"status": "authenticated", "method": "token"}
        return {"error": f"Auth failed: {response.status_code}"}
    except Exception as e:
        return {"error": f"Auth error: {str(e)}", "fallback": "Use browser-search instead"}


def _get_api_headers():
    session = _load_session()
    if not session:
        result = api_authenticate()
        if "error" in result:
            return None, result
        session = _load_session()

    if session.get("auth_type") == "api_key":
        return {"X-API-Key": session["api_key"], "Content-Type": "application/json"}, None
    return {"Authorization": f"Bearer {session.get('token', '')}", "Content-Type": "application/json"}, None


def api_search_projects(trade="painting", state="NC", status="bidding", limit=50):
    """Search projects via API."""
    if httpx is None:
        return {"error": "httpx not installed"}

    headers, err = _get_api_headers()
    if err:
        return err

    params = {
        "trade": trade,
        "state": state,
        "status": status,
        "limit": limit,
    }

    try:
        response = httpx.get(f"{API_BASE}/projects", headers=headers, params=params)
        if response.status_code == 200:
            data = response.json()
            projects = []
            for p in data.get("results", data.get("projects", [])):
                projects.append({
                    "id": p.get("id"),
                    "project_name": p.get("name") or p.get("title", ""),
                    "gc": p.get("generalContractor") or p.get("company", ""),
                    "location": f"{p.get('city', '')}, {p.get('state', '')}",
                    "bid_due": p.get("bidDate") or p.get("dueDate", ""),
                    "facility_type": p.get("buildingUse") or p.get("type", ""),
                    "trades": p.get("trades", [trade]),
                    "est_value": p.get("estimatedValue", ""),
                    "documents_available": bool(p.get("documents") or p.get("planCount", 0) > 0),
                    "portal_url": f"{PORTAL_URL}/project/{p.get('id', '')}",
                    "source": "constructconnect",
                })
            return {"projects": projects, "total": len(projects)}
        return {"error": f"API error: {response.status_code}", "detail": response.text}
    except Exception as e:
        return {"error": str(e), "fallback": "Use browser-search instead"}


def api_download_documents(project_id, output_dir):
    """Download project documents via API."""
    if httpx is None:
        return {"error": "httpx not installed"}

    headers, err = _get_api_headers()
    if err:
        return err

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    try:
        response = httpx.get(f"{API_BASE}/projects/{project_id}/documents", headers=headers)
        if response.status_code != 200:
            return {"error": f"API error: {response.status_code}"}

        docs = response.json().get("documents", [])
        downloaded = []

        for doc in docs:
            doc_name = doc.get("name") or doc.get("fileName", f"doc_{doc.get('id', '')}")
            download_url = doc.get("downloadUrl") or doc.get("url", "")

            if download_url:
                try:
                    file_resp = httpx.get(download_url, headers=headers, follow_redirects=True)
                    if file_resp.status_code == 200:
                        file_path = output_path / doc_name
                        with open(file_path, "wb") as f:
                            f.write(file_resp.content)
                        downloaded.append({"name": doc_name, "path": str(file_path),
                                         "size_kb": round(len(file_resp.content) / 1024, 1)})
                except Exception as e:
                    downloaded.append({"name": doc_name, "error": str(e)})

        return {"downloaded": downloaded, "total": len(downloaded)}
    except Exception as e:
        return {"error": str(e)}


# --- Playwright Browser Fallback ---

async def browser_search(trade="painting", state="NC", bid_date_filter=None):
    """Pull projects from ConstructConnect Bid Center inbox via Playwright.
    This is the primary method — the Bid Center contains all SmartBid invitations.

    Args:
        trade: Trade filter (currently unused — CC inbox shows all invited trades)
        state: State filter (currently unused — CC inbox shows all locations)
        bid_date_filter: Optional date string like "Apr 1, 2026" to filter results.
                         Pass "today" to auto-filter to today's date.
    """
    try:
        from playwright.async_api import async_playwright
        import asyncio as _asyncio
    except ImportError:
        return {"error": "Playwright not installed. Run: pip install playwright && playwright install chromium"}

    config = _load_config()
    if not config or "username" not in config:
        return {"error": "Need username/password in cc_auth.json for browser login"}

    # Resolve "today" filter
    if bid_date_filter == "today":
        bid_date_filter = datetime.now().strftime("%b %-d, %Y") if sys.platform != "win32" else datetime.now().strftime("%b %#d, %Y")

    projects = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36",
            viewport={"width": 1600, "height": 900},
        )
        page = await context.new_page()

        try:
            # Login
            await page.goto("https://app.constructconnect.com", timeout=30000)
            await _asyncio.sleep(3)
            email_inp = await page.query_selector("#email-input")
            if email_inp:
                await email_inp.fill(config["username"])
                pwd_inp = await page.query_selector("#password-input")
                if pwd_inp:
                    await pwd_inp.fill(config["password"])
                login_btn = await page.query_selector('button:has-text("Log In")')
                if login_btn:
                    await login_btn.click()
                    await _asyncio.sleep(8)

            # Navigate to Bid Center inbox
            await page.goto("https://app.constructconnect.com/bidcenter/tabs/inbox", timeout=30000)
            await _asyncio.sleep(8)

            # Paginate through all pages
            page_num = 0
            while True:
                page_num += 1
                rows = await page.query_selector_all("table tbody tr")

                for row in rows:
                    cells = await row.query_selector_all("td")
                    if len(cells) < 6:
                        continue  # Skip action rows (e.g. "Moved to Archive")

                    texts = []
                    for cell in cells:
                        t = (await cell.inner_text()).strip()
                        texts.append(t)

                    # Skip rows without a project name
                    project_name = texts[1] if len(texts) > 1 else ""
                    if not project_name or project_name == "Moved to":
                        continue

                    # Get project link
                    link = await row.query_selector("a")
                    href = await link.get_attribute("href") if link else ""

                    # Table columns: [0]checkbox [1]Project Name [2]menu [3]Status
                    #   [4]Assigned [5]Location [6]Bid Date [7]Internal ID [8]Source [9]Company
                    status = texts[3] if len(texts) > 3 else ""
                    location = texts[5] if len(texts) > 5 else ""
                    bid_date = texts[6] if len(texts) > 6 else ""
                    company = texts[9] if len(texts) > 9 else ""

                    # Clean up project name (remove " - All Trades", " - Main Trades")
                    clean_name = project_name
                    for suffix in [" - All Trades", " - Main Trades", " - All", " - Main"]:
                        if clean_name.endswith(suffix):
                            clean_name = clean_name[:-len(suffix)]

                    projects.append({
                        "project_name": clean_name.strip(),
                        "full_name": project_name.strip(),
                        "gc": company.strip(),
                        "location": location.strip(),
                        "bid_due": bid_date.strip(),
                        "status": status.strip(),
                        "portal_url": f"https://app.constructconnect.com{href}" if href and not href.startswith("http") else href,
                        "source": "constructconnect",
                        "trade_scope": "All Trades" if "All Trades" in project_name else "Main Trades",
                    })

                # Check for next page
                next_btn = await page.query_selector('button[aria-label="Go to next page"]:not([disabled]), [class*="pagination"] button:last-child:not([disabled])')
                if not next_btn:
                    # Try generic next page arrow
                    pagination_text = ""
                    pag_el = await page.query_selector('[class*="pagination"], [class*="pager"]')
                    if pag_el:
                        pagination_text = (await pag_el.inner_text()).strip()

                    # Look for a ">" or "Next" button that's not disabled
                    next_buttons = await page.query_selector_all('button')
                    found_next = False
                    for btn in next_buttons:
                        txt = (await btn.inner_text()).strip()
                        disabled = await btn.get_attribute("disabled")
                        if txt in (">", "›", "Next") and disabled is None:
                            await btn.click()
                            await _asyncio.sleep(3)
                            found_next = True
                            break

                    if not found_next:
                        break  # No more pages
                else:
                    await next_btn.click()
                    await _asyncio.sleep(3)

                # Safety: max 10 pages
                if page_num >= 10:
                    break

        except Exception as e:
            return {"error": f"Browser automation failed: {str(e)}", "projects_found": len(projects)}
        finally:
            await browser.close()

    # Apply date filter if specified
    if bid_date_filter:
        projects = [p for p in projects if p["bid_due"] == bid_date_filter]

    return {"projects": projects, "total": len(projects), "method": "browser_bidcenter"}


async def browser_download_documents(project_url, output_dir):
    """Download documents from ConstructConnect project page using Playwright."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"error": "Playwright not installed"}

    config = _load_config()
    if not config:
        return {"error": "No config found"}

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    downloaded = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        try:
            import asyncio as _asyncio

            # Login — same approach as browser_search (proven working)
            await page.goto("https://app.constructconnect.com", timeout=30000)
            await _asyncio.sleep(3)
            email_inp = await page.query_selector("#email-input")
            if email_inp:
                await email_inp.fill(config["username"])
                pwd_inp = await page.query_selector("#password-input")
                if pwd_inp:
                    await pwd_inp.fill(config["password"])
                login_btn = await page.query_selector('button:has-text("Log In")')
                if login_btn:
                    await login_btn.click()
                    await _asyncio.sleep(8)

            # Dismiss cookie banner if present
            cookie_btn = await page.query_selector('button:has-text("Accept and Continue")')
            if cookie_btn:
                await cookie_btn.click()
                await _asyncio.sleep(1)

            # Navigate to project page
            await page.goto(project_url, timeout=30000)
            await _asyncio.sleep(3)

            # Dismiss cookie banner again if it reappears
            cookie_btn = await page.query_selector('button:has-text("Accept and Continue")')
            if cookie_btn:
                await cookie_btn.click()
                await _asyncio.sleep(1)

            # Wait for project content to load (not just "Please wait")
            for _ in range(12):  # up to 60 seconds
                loading = await page.query_selector('text="Please wait while we load"')
                if not loading:
                    break
                await _asyncio.sleep(5)
            await _asyncio.sleep(3)

            # Also scrape project info text from the page
            project_info = {}
            try:
                body_text = await page.inner_text("body")
                project_info["page_text"] = body_text[:5000]
            except:
                pass

            # Try "View/Download Documents" button (CC Project Intelligence pages)
            dl_btn = await page.query_selector('button:has-text("View/Download Documents")')
            if dl_btn:
                # Check if it opens a new tab/popup
                async with context.expect_page(timeout=10000) as new_page_info:
                    await dl_btn.click()
                try:
                    new_page = await new_page_info.value
                    await new_page.wait_for_load_state("networkidle", timeout=15000)
                    await _asyncio.sleep(3)

                    # Screenshot the document viewer
                    debug_dir = Path(output_dir)
                    debug_dir.mkdir(parents=True, exist_ok=True)
                    await new_page.screenshot(path=str(debug_dir / "_docs_page_screenshot.png"))

                    # Look for download links in the new page
                    doc_links = await new_page.query_selector_all(
                        'a[href*="download"], a[href*=".pdf"], a[download], '
                        'button:has-text("Download"), [class*="download"]'
                    )
                    for link in doc_links:
                        try:
                            async with new_page.expect_download(timeout=30000) as download_info:
                                await link.click()
                            download = await download_info.value
                            file_path = output_path / download.suggested_filename
                            await download.save_as(str(file_path))
                            downloaded.append({
                                "name": download.suggested_filename,
                                "path": str(file_path),
                            })
                        except:
                            pass
                except:
                    # Didn't open a new page — may have triggered an in-page action
                    await _asyncio.sleep(5)

            # Screenshot main page for debugging
            debug_dir = Path(output_dir)
            debug_dir.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(debug_dir / "_page_screenshot.png"))

            # Also try finding download links on the main page
            if not downloaded:
                links = await page.query_selector_all(
                    'a[href*="download"], a[href*=".pdf"], '
                    'a[download], button:has-text("Download All")'
                )
                for link in links:
                    try:
                        async with page.expect_download(timeout=15000) as download_info:
                            await link.click()
                        download = await download_info.value
                        file_path = output_path / download.suggested_filename
                        await download.save_as(str(file_path))
                        downloaded.append({
                            "name": download.suggested_filename,
                            "path": str(file_path),
                        })
                    except:
                        pass

            # Save project info even if no docs downloaded
            if project_info:
                import json as _json
                info_path = debug_dir / "_project_info.json"
                with open(info_path, "w") as f:
                    _json.dump(project_info, f, indent=2)

        except Exception as e:
            return {"error": f"Download failed: {str(e)}", "downloaded": downloaded}
        finally:
            await browser.close()

    return {"downloaded": downloaded, "total": len(downloaded)}


def check_status():
    """Check ConstructConnect configuration status."""
    config = _load_config()
    if not config:
        return {
            "configured": False,
            "message": f"Create {AUTH_FILE} with your credentials",
            "template": {"username": "YOUR_EMAIL", "password": "YOUR_PASSWORD", "api_key": "YOUR_API_KEY_IF_AVAILABLE"}
        }

    session = _load_session()
    return {
        "configured": True,
        "has_api_key": bool(config.get("api_key")),
        "has_credentials": bool(config.get("username")),
        "authenticated": session is not None,
    }


def main():
    parser = argparse.ArgumentParser(description="ConstructConnect Client")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status")
    sub.add_parser("auth")

    search_p = sub.add_parser("search")
    search_p.add_argument("--trade", default="painting")
    search_p.add_argument("--state", default="NC")
    search_p.add_argument("--status", default="bidding")

    proj_p = sub.add_parser("documents")
    proj_p.add_argument("--id", required=True)
    proj_p.add_argument("--output", required=True)

    bsearch_p = sub.add_parser("browser-search")
    bsearch_p.add_argument("--trade", default="painting")
    bsearch_p.add_argument("--state", default="NC")
    bsearch_p.add_argument("--bid-date-filter", default=None, help="Filter by bid date: 'today' or 'Apr 2, 2026'")

    bdl_p = sub.add_parser("browser-download")
    bdl_p.add_argument("--url", required=True)
    bdl_p.add_argument("--output", required=True)

    args = parser.parse_args()

    if args.command == "status":
        result = check_status()
    elif args.command == "auth":
        result = api_authenticate()
    elif args.command == "search":
        result = api_search_projects(args.trade, args.state, args.status)
    elif args.command == "documents":
        result = api_download_documents(args.id, args.output)
    elif args.command == "browser-search":
        import asyncio
        result = asyncio.run(browser_search(args.trade, args.state, bid_date_filter=args.bid_date_filter))
    elif args.command == "browser-download":
        import asyncio
        result = asyncio.run(browser_download_documents(args.url, args.output))
    else:
        parser.print_help()
        sys.exit(1)

    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
