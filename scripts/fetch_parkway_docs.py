#!/usr/bin/env python3
r"""
fetch_parkway_docs.py — Pull bid documents for ONE project from Parkway's private
portal (parkwayconstructionplans.com).

Why this exists: fetch_project_docs.py only handles ConstructConnect + Building-
Connected; it errors "Unknown source" on parkway_portal. Parkway is a key
recurring GC, so we need first-class Parkway doc-pull.

Flow: log in with data/config/parkway_auth.json -> open the matching bidding
project -> download its plan/spec files into data/projects/<slug>/bid_docs/.

Runs HEADFUL by default so that if the auto-download can't find the button, you
can click "Download" yourself in the open window — Playwright's download listener
captures user-initiated downloads too. The project-page DOM is also dumped to
_parkway_project_debug.html so the auto-path can be refined next time.

Usage:
  python scripts/fetch_parkway_docs.py "Harrogate"
  python scripts/fetch_parkway_docs.py "Harrogate" --name "Comfort Inn & Suites Harrogate, TN"
  python scripts/fetch_parkway_docs.py "Harrogate" --headless --timeout 300
"""
from __future__ import annotations
import argparse, asyncio, json, re, sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / "data" / "config" / "parkway_auth.json"
PROJECTS_DIR = ROOT / "data" / "projects"


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9\s-]", "", (name or "").lower().strip())
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s[:80].strip("-")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query", help="distinctive substring of the project name (e.g. 'Harrogate')")
    ap.add_argument("--name", default=None,
                    help="canonical project name for the folder slug (defaults to query)")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--timeout", type=int, default=300,
                    help="max seconds to wait for downloads (auto or your manual click)")
    args = ap.parse_args()

    from playwright.async_api import async_playwright

    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    slug = slugify(args.name or args.query)
    out_dir = PROJECTS_DIR / slug / "bid_docs"
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"[parkway] target folder: {out_dir}")

    downloaded: list[str] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=args.headless,
            args=["--disable-blink-features=AutomationControlled", "--start-maximized"],
        )
        # Parkway's SSL cert is expired (known) — ignore for this portal only.
        ctx = await browser.new_context(ignore_https_errors=True, accept_downloads=True,
                                        viewport=None)
        page = await ctx.new_page()

        async def _save(dl):
            try:
                fp = out_dir / dl.suggested_filename
                await dl.save_as(str(fp))
                downloaded.append(fp.name)
                print(f"  ⬇ saved: {fp.name}")
            except Exception as e:
                print(f"  download save failed: {e}")

        ctx.on("download", lambda dl: asyncio.create_task(_save(dl)))

        # ---- login ----
        print("[parkway] logging in...")
        await page.goto(cfg["url"], timeout=30000)
        try:
            await page.fill('input[placeholder="Enter Username"]', cfg["username"])
            pwf = page.locator('input[placeholder="Enter Password"]')
            await pwf.fill(cfg["password"])
            await pwf.press("Enter")
        except Exception as e:
            print(f"[parkway] login form issue: {e}")
        try:
            await page.wait_for_selector("table tbody tr, [role=row]", timeout=20000)
        except Exception as e:
            print(f"[parkway] login may have failed: {e}")

        # ---- go to bidding + find the project ----
        await page.goto("https://parkwayconstructionplans.com/#p/projects/bidding",
                        timeout=20000, wait_until="networkidle")
        await page.wait_for_timeout(2500)

        q = args.query.lower()
        clicked = False
        for _page in range(20):  # pagination safety cap
            rows = await page.query_selector_all("table tbody tr")
            for row in rows:
                try:
                    txt = ((await row.inner_text()) or "").lower()
                except Exception:
                    continue
                if q in txt:
                    # Parkway SPA: open the project via the row's "go to project" arrow
                    # button (class btn-go-to-project, revealed on row hover) — clicking
                    # the row TEXT does nothing. Hover first to reveal it, then click;
                    # fall back to a JS click if it's still hover-hidden.
                    try:
                        await row.hover()
                        await page.wait_for_timeout(300)
                    except Exception:
                        pass
                    go = (await row.query_selector("button.btn-go-to-project")
                          or await row.query_selector(".btn-go-to-project")
                          or await row.query_selector("td:last-child button"))
                    if go:
                        try:
                            await go.click(timeout=5000)
                            clicked = True
                        except Exception:
                            try:
                                await go.evaluate("el => el.click()")
                                clicked = True
                            except Exception:
                                pass
                    if not clicked:
                        try:
                            await row.click()
                            clicked = True
                        except Exception:
                            pass
                    break
            if clicked:
                break
            nxt = await page.query_selector(
                'a:has-text("Next"), button:has-text("Next"), [aria-label="Next"]')
            if nxt:
                try:
                    await nxt.click()
                    await page.wait_for_timeout(1500)
                except Exception:
                    break
            else:
                break

        if clicked:
            await page.wait_for_timeout(4000)
            print(f"[parkway] opened project page: {page.url}")
            # Jump to the documents/plans area if it's a separate tab/link.
            for tab in ("Documents", "Plans", "Plan Room", "Files", "Attachments",
                        "Bid Documents", "Project Documents", "Drawings"):
                el = await page.query_selector(
                    f'a:has-text("{tab}"), button:has-text("{tab}"), '
                    f'[role=tab]:has-text("{tab}")')
                if el:
                    try:
                        await el.click()
                        await page.wait_for_timeout(3000)
                        print(f"[parkway] opened '{tab}' tab")
                        break
                    except Exception:
                        pass
        else:
            print(f"[parkway] could not auto-find a row matching '{args.query}'. "
                  f"The window is open — click into the project + Download manually.")

        # dump project-page DOM for future refinement
        try:
            (out_dir.parent / "_parkway_project_debug.html").write_text(
                (await page.content())[:200000], encoding="utf-8")
        except Exception:
            pass

        # ---- attempt auto-download ----
        async def _try(selector):
            el = await page.query_selector(selector)
            if not el:
                return False
            try:
                await el.scroll_into_view_if_needed(timeout=4000)
            except Exception:
                pass
            try:
                await el.click()
                await page.wait_for_timeout(2500)
                return True
            except Exception:
                return False

        # Pantera plan room: the ONLY bulk download is the exact 'btn-download-all'
        # button. Click it ONCE — do NOT spam other clicks, which breaks the ZIP prep.
        for sel in ('button.btn-download-all', 'button:has-text("Download All")',
                    'a:has-text("Download All")', '[aria-label*="Download All"]'):
            if await _try(sel):
                print(f"[parkway] clicked: {sel}")
                break

        # 'Download All' may pop a modal and/or prepare a ZIP server-side before the
        # download fires. Give it room, then confirm a modal ONCE if one appears.
        # The context-level download listener captures the ZIP whenever it arrives.
        await page.wait_for_timeout(4000)
        try:
            modal_btn = await page.query_selector(
                '.modal.show button:has-text("Download"), [role=dialog] button:has-text("Download"), '
                '.modal.show button:has-text("Confirm"), .modal.show button:has-text("OK"), '
                '.modal.show button:has-text("Continue")')
            if modal_btn:
                await modal_btn.click()
                print("[parkway] confirmed download modal")
        except Exception:
            pass

        # ---- wait for downloads (auto OR your manual click) ----
        print(f"[parkway] waiting up to {args.timeout}s for downloads — if a file list "
              f"is showing, click Download/Download All in the window now...")
        waited, idle, last = 0, 0, 0
        while waited < args.timeout:
            await page.wait_for_timeout(3000)
            waited += 3
            if len(downloaded) > last:
                last = len(downloaded)
                idle = 0
            else:
                idle += 3
            # got file(s) and 20s with no new download → assume complete
            if downloaded and idle >= 20:
                break

        await page.wait_for_timeout(1500)
        await browser.close()

    print(f"\n[parkway] downloaded {len(downloaded)} file(s) -> {out_dir}")
    for n in downloaded:
        print(f"   - {n}")
    print(f"__RESULT__:{json.dumps({'downloaded': downloaded, 'dir': str(out_dir), 'slug': slug})}")
    return 0 if downloaded else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
