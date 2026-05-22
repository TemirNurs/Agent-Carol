#!/usr/bin/env python3
r"""
read_paint_scope_today.py — Open every project due today, locate the painting
spec(s) (09 91 13 / 09 91 23 / 09 93 00 / 09 72 16 / 09 96 / 09 24), pull the
Summary + Scope text, and quantify scope footprint (surface types, special
coatings, sq ft hints, room counts).

Output: a ranked table — REAL paint scope, not file count.
"""
from __future__ import annotations
import sys, re, json
from pathlib import Path
from collections import Counter

try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

import pdfplumber

ROOT = Path(r"C:/Agent Carol/data/projects")

PROJECTS = [
    # (folder, label, primary_spec_pdf_candidates_relative_paths)
    ("sampson_cc_allied_health", "Sampson CC Allied Health (Monteith)", [
        "Specifications/Division 09.pdf",
    ]),
    ("sampson_cc_nursing_daniels", "Sampson CC Nursing (Daniels)", [
        "Specifications/Division 09.pdf",
    ]),
    ("foreman_bundy_wtp", "Foreman Bundy WTP", [
        "Specifications/Division 09.pdf",
    ]),
    ("food_lion_2671b", "Food Lion 2671B Petersburg", [
        "Specifications/Division 09.pdf",
    ]),
    ("food_lion_2118b", "Food Lion 2118B Dinwiddie", [
        "Specifications/Division 09.pdf",
    ]),
    ("vika_corporation", "Vika Corporation", [
        "Scope Sheets/Paint.pdf",
    ]),
    ("rtop_cook_cdc", "RTOP Cook CDC", [
        "Specifications/Specs_Repair Cook Child Development.pdf",
    ]),
    ("sampson_ci_gatehouse", "Sampson CI Gatehouse", [
        "Specifications/24-27819-01A-DAC-BID SET-PM.pdf",
    ]),
    ("national_heritage_restroom", "National Heritage Restroom", [
        "Other Documents/Greensboro Restroom RFP.pdf",
    ]),
    ("box_lunch_raleigh", "Box Lunch 4825 Raleigh", []),
]

SECTION_RE = re.compile(
    r"\b09\s*(?:24|72\s*16|91\s*13|91\s*23|93\s*00|96\s*00|96\s*23|96\s*43|96\s*53|96\s*56)\b",
    re.I,
)
PAINT_HINT_RE = re.compile(
    r"\b(painting|paint|primer|coating|wallcover|wall\s*cover|enamel|epoxy|"
    r"intumescent|stain|sealer|FRP|fiberglass\s*reinforced\s*panel)\b",
    re.I,
)
ROOM_HINT_RE = re.compile(
    r"\b(GWB|gypsum|drywall|CMU|concrete\s*masonry|ferrous\s*metal|hollow\s*metal|"
    r"door[s]?\s*and\s*frame|woodwork|trim|ceiling|deck|exposed\s*structure|"
    r"stair|handrail|guardrail|bollard|pipe\s*marking)\b",
    re.I,
)


def scan_pdf(path: Path) -> dict:
    """Pull paint-related text from a PDF. Returns excerpts + counts."""
    result = {
        "path": str(path),
        "exists": path.exists(),
        "pages": 0,
        "size_kb": 0,
        "paint_pages": [],   # page numbers where paint content lives
        "sections_found": [],
        "surface_counts": Counter(),
        "excerpts": [],
        "scope_chars": 0,
    }
    if not path.exists():
        return result
    result["size_kb"] = int(path.stat().st_size / 1024)
    try:
        with pdfplumber.open(str(path)) as pdf:
            result["pages"] = len(pdf.pages)
            paint_text_total = ""
            for i, pg in enumerate(pdf.pages, start=1):
                try:
                    txt = pg.extract_text() or ""
                except Exception:
                    continue
                # Find sections
                for m in SECTION_RE.finditer(txt):
                    sec = re.sub(r"\s+", " ", m.group(0)).strip()
                    if sec not in result["sections_found"]:
                        result["sections_found"].append(sec)
                # Is this a paint page?
                paint_hits = len(PAINT_HINT_RE.findall(txt))
                if paint_hits >= 2 or SECTION_RE.search(txt):
                    result["paint_pages"].append(i)
                    paint_text_total += "\n" + txt
                    # Surface vocabulary
                    for sw in ROOM_HINT_RE.findall(txt):
                        result["surface_counts"][sw.lower()] += 1
            result["scope_chars"] = len(paint_text_total)
            # Pull a couple of representative excerpts (first 800 chars of paint zone)
            chunks = []
            for chunk_start in (0, 1500, 3000):
                seg = paint_text_total[chunk_start:chunk_start + 600].strip()
                if seg:
                    chunks.append(seg)
            result["excerpts"] = chunks[:3]
    except Exception as e:
        result["error"] = str(e)
    return result


def rate_scope(r: dict) -> tuple[int, str]:
    """Heuristic score 0-100 of painting scope footprint.
       Combines paint-page count, sections found, surface vocab spread, and
       absolute paint text volume."""
    if not r["exists"]:
        return 0, "no docs downloaded"
    score = 0
    score += min(len(r["paint_pages"]) * 5, 40)
    score += min(len(r["sections_found"]) * 8, 32)
    score += min(sum(r["surface_counts"].values()), 20)
    score += min(r["scope_chars"] // 1200, 28)
    notes = []
    if "09 91 13" in " ".join(r["sections_found"]):
        notes.append("EXT paint")
    if "09 91 23" in " ".join(r["sections_found"]):
        notes.append("INT paint")
    if "09 93 00" in " ".join(r["sections_found"]):
        notes.append("stain/finish")
    if any("72" in s for s in r["sections_found"]):
        notes.append("WALLCOVERING")
    if any("96" in s for s in r["sections_found"]):
        notes.append("HIGH-PERF coating")
    if r["surface_counts"].get("cmu", 0) + r["surface_counts"].get("concrete masonry", 0) > 0:
        notes.append("CMU")
    if r["surface_counts"].get("ferrous metal", 0) > 0:
        notes.append("ferrous metal")
    if "FRP" in " ".join(r["excerpts"]) or "FRP" in r["surface_counts"]:
        notes.append("FRP")
    return min(score, 100), ", ".join(notes) or "—"


def main():
    rows = []
    for folder, label, candidates in PROJECTS:
        proj = ROOT / folder
        best = None
        for cand in candidates:
            p = proj / cand
            r = scan_pdf(p)
            if not best or r["scope_chars"] > best["scope_chars"]:
                best = r
        if best is None:
            best = {"path": "(no candidates)", "exists": False, "pages": 0, "size_kb": 0,
                    "paint_pages": [], "sections_found": [], "surface_counts": Counter(),
                    "excerpts": [], "scope_chars": 0}
        score, notes = rate_scope(best)
        rows.append((score, label, best, notes))

    rows.sort(key=lambda r: -r[0])
    print(f"\n{'='*88}\nPAINT SCOPE — REAL spec-reading (not file counts)\n{'='*88}")
    print(f"{'#':<3}{'PROJECT':<42}{'SCORE':>6}  {'PG':>4} {'CHARS':>6}  SECTIONS / NOTES")
    print("-" * 88)
    for i, (sc, name, r, notes) in enumerate(rows, 1):
        sects = ", ".join(r["sections_found"][:4]) or "—"
        print(f"{i:<3}{name[:40]:<42}{sc:>6}  {len(r['paint_pages']):>4} {r['scope_chars']:>6}  {sects[:38]:<38}  {notes}")
    print("-" * 88)

    # Drop excerpts to disk for full review
    out = Path(r"C:/Agent Carol/data/memory/paint_scope_today.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    dump = []
    for sc, name, r, notes in rows:
        dump.append({
            "rank_score": sc, "project": name, "notes": notes,
            "pdf": r["path"], "size_kb": r["size_kb"], "pages": r["pages"],
            "paint_pages": r["paint_pages"],
            "sections_found": r["sections_found"],
            "surface_counts": dict(r["surface_counts"]),
            "scope_chars": r["scope_chars"],
            "excerpts": r["excerpts"],
        })
    out.write_text(json.dumps(dump, indent=2), encoding="utf-8")
    print(f"\n[wrote] {out}")


if __name__ == "__main__":
    main()
