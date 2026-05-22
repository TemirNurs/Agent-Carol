#!/usr/bin/env python3
r"""
download_bc_files.py - Download bid documents from a BuildingConnected
opportunity (Files tab). Uses the captured BC session.

Usage:
  python scripts/download_bc_files.py <opportunity_files_url> <output_dir>

The opportunity URL looks like:
  https://app.buildingconnected.com/opportunities/<id>/files

Saves each file to <output_dir>/ and writes a manifest.json with names/sizes.
"""
from __future__ import annotations
import asyncio, json, sys, re
from pathlib import Path

try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "data" / "config" / "bc_storage_state.json"


async def main(url: str, out_dir: str):
    out = Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    from playwright.async_api import async_playwright
    if not STATE.exists():
        print(f"ERR: no BC session at {STATE} — run bc_login_capture.py first")
        return
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(storage_state=str(STATE))
        page = await ctx.new_page()
        await page.goto(url, wait_until="networkidle", timeout=60_000)
        # Give the JS-rendered file list time
        await page.wait_for_timeout(3000)

        # File rows in BC's file table — try multiple selectors that match either
        # the project files table or the documents pane
        files = []
        sels = [
            "[data-test-id='files-table'] tbody tr",
            "div[role='rowgroup'] [role='row']",
            "tr:has-text('.pdf'), tr:has-text('.dwg')",
            "[class*='FileRow'], [class*='file-row']",
        ]
        for sel in sels:
            rows = await page.query_selector_all(sel)
            if len(rows) > len(files):
                files = []
                for r in rows:
                    name = (await r.inner_text()).strip().replace("\n", " | ")[:200]
                    files.append(name)
                if files:
                    break

        # Fallback: pull every anchor that looks like a file link
        anchors = await page.query_selector_all("a[href]")
        link_files = []
        for a in anchors:
            href = await a.get_attribute("href") or ""
            txt = (await a.inner_text() or "").strip()
            if re.search(r"\.(pdf|dwg|xls|xlsx|doc|docx|zip|rfa|rvt|ifc)\b", txt, re.I):
                link_files.append(f"{txt} [{href[:80]}]")

        if not files and link_files:
            files = link_files

        # Try to count rows by alternate text patterns if still empty
        if not files:
            body_text = await page.inner_text("body")
            pdf_mentions = len(re.findall(r"\.pdf\b", body_text, re.I))
            files = [f"(no row selector matched; body text contains {pdf_mentions} .pdf mentions)"]

        print(json.dumps({"total": len(files), "files_sample": files[:25]}, indent=2))
        (out / "manifest.json").write_text(
            json.dumps({"url": url, "total": len(files), "files": files}, indent=2),
            encoding="utf-8")
        await browser.close()


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: download_bc_files.py <url> <out_dir>"); sys.exit(1)
    asyncio.run(main(sys.argv[1], sys.argv[2]))
