#!/usr/bin/env python3
r"""
download_bc_files.py - Download ALL bid documents from a BuildingConnected
opportunity (Files tab) as the portal's zip. Uses the captured BC session.

Usage:
  python scripts/download_bc_files.py <opportunity_files_url> <output_dir>

Flow (proven on Morningstar 6/11): goto files page (domcontentloaded — BC's
SPA never reaches networkidle), click "Download All", BC shows a
"Preparing Files" modal while it zips server-side (can take minutes on
100MB+ sets), then the browser download fires. First click sometimes does
nothing — re-click once if no download starts within 90s. The zip is saved
to <output_dir> and extracted into it; manifest.json records contents.
"""
from __future__ import annotations
import asyncio
import json
import sys
import zipfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "data" / "config" / "bc_storage_state.json"

DL_SELS = [
    'button:has-text("Download all")',
    'button:has-text("Download All")',
    '[data-test-id*="download-all"]',
    'button[aria-label*="Download" i]',
]


async def main(url: str, out_dir: str):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    from playwright.async_api import async_playwright
    if not STATE.exists():
        print(f"ERR: no BC session at {STATE} — run bc_login_capture.py first")
        return 1
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(storage_state=str(STATE),
                                        accept_downloads=True)
        page = await ctx.new_page()
        print("goto files page…")
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(10_000)
        if "login" in page.url or "signin" in page.url:
            print("ERR: BC session expired — run bc_login_capture.py")
            return 1

        btn = None
        for sel in DL_SELS:
            btn = await page.query_selector(sel)
            if btn:
                print("download control:", sel)
                break
        if not btn:
            texts = await page.eval_on_selector_all(
                "button",
                "els => els.map(e => (e.innerText||'').trim()).filter(Boolean)")
            print("ERR: no Download All button. Buttons on page:", texts[:30])
            await page.screenshot(path=str(out / "_page.png"), full_page=True)
            return 1

        print("clicking Download All… (server-side zip can take minutes)")
        dl = None
        try:
            async with page.expect_download(timeout=90_000) as dl_info:
                await btn.click()
            dl = await dl_info.value
        except Exception:
            print("no download after first click — re-clicking (known BC quirk)")
            try:
                btn2 = None
                for sel in DL_SELS:
                    btn2 = await page.query_selector(sel)
                    if btn2:
                        break
                async with page.expect_download(timeout=480_000) as dl_info:
                    if btn2:
                        await btn2.click()
                dl = await dl_info.value
            except Exception as e:
                print("ERR: download never fired:", str(e)[:120])
                await page.screenshot(path=str(out / "_page.png"), full_page=True)
                return 1

        dest = out / dl.suggested_filename
        print("downloading:", dest.name)
        await dl.save_as(dest)
        size = dest.stat().st_size
        print(f"saved {dest.name}  {size/1e6:.1f} MB")
        await browser.close()

    extracted = []
    if dest.suffix.lower() == ".zip":
        with zipfile.ZipFile(dest) as z:
            z.extractall(out)
            extracted = z.namelist()
        print(f"extracted {len(extracted)} files")
    (out / "manifest.json").write_text(
        json.dumps({"url": url, "zip": dest.name, "zip_bytes": size,
                    "extracted": extracted}, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: download_bc_files.py <url> <out_dir>")
        sys.exit(1)
    sys.exit(asyncio.run(main(sys.argv[1], sys.argv[2])) or 0)
