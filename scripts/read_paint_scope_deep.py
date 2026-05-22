#!/usr/bin/env python3
r"""
read_paint_scope_deep.py — Second pass: pull the ACTUAL painting Section text
(09 91 13 + 09 91 23 + 09 93 00 + 09 96 00) from the Division 09 / scope PDF
of every today's-due project. Print the Summary / Scope Includes prose so we
can compare scope size by reading, not counting.

Output excerpts to data/memory/paint_scope_deep.json + console.
"""
from __future__ import annotations
import json, re, sys
from pathlib import Path

try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

import pdfplumber

ROOT = Path(r"C:/Agent Carol/data/projects")

PROJECTS = [
    ("rtop_cook_cdc",                "RTOP Cook CDC (Fort Liberty)",          "Specifications/Specs_Repair Cook Child Development.pdf"),
    ("sampson_ci_gatehouse",          "Sampson CI Gatehouse",                  "Specifications/24-27819-01A-DAC-BID SET-PM.pdf"),
    ("sampson_cc_allied_health",      "Sampson CC Allied Health (Monteith)",   "Specifications/Division 09.pdf"),
    ("sampson_cc_nursing_daniels",    "Sampson CC Nursing (Daniels)",          "Specifications/Division 09.pdf"),
    ("food_lion_2671b",               "Food Lion 2671B Petersburg",            "Specifications/Division 09.pdf"),
    ("food_lion_2118b",               "Food Lion 2118B Dinwiddie",             "Specifications/Division 09.pdf"),
    ("foreman_bundy_wtp",             "Foreman Bundy WTP",                     "Specifications/Division 09.pdf"),
    ("vika_corporation",              "Vika Corporation",                      "Scope Sheets/Paint.pdf"),
]


SECTION_HEADERS = [
    re.compile(r"^\s*SECTION\s*0?9\s*9?\s*1\s*1?3\b.*EXTERIOR\s*PAINT", re.I | re.M),
    re.compile(r"^\s*SECTION\s*0?9\s*9?\s*1\s*2?3\b.*INTERIOR\s*PAINT", re.I | re.M),
    re.compile(r"^\s*SECTION\s*0?9\s*9?\s*3\s*0?0\b.*STAIN", re.I | re.M),
    re.compile(r"^\s*SECTION\s*0?9\s*9?\s*6\s*0?0\b.*HIGH[-\s]?PERF", re.I | re.M),
    re.compile(r"\bEXTERIOR\s*PAINTING\b", re.I),
    re.compile(r"\bINTERIOR\s*PAINTING\b", re.I),
    re.compile(r"\bHIGH[-\s]?PERFORMANCE\s*COATING", re.I),
]
SUMMARY_RE = re.compile(r"(?:SUMMARY|SECTION\s+INCLUDES|SCOPE\s+OF\s+WORK|Description)[:\.\s]", re.I)


def find_section_pages(pdf, section_keywords):
    """Return list of pages where any keyword matches."""
    hits = []
    for i, page in enumerate(pdf.pages, start=1):
        try: txt = page.extract_text() or ""
        except Exception: continue
        for kw in section_keywords:
            if kw.search(txt):
                hits.append(i)
                break
    return hits


def extract_section(pdf, start_page, max_pages=8):
    """Pull text from start_page through next ~max_pages pages."""
    out = []
    for i in range(start_page, min(start_page + max_pages, len(pdf.pages) + 1)):
        try:
            t = pdf.pages[i - 1].extract_text() or ""
            out.append(f"\n— page {i} —\n{t}")
        except Exception:
            continue
    return "\n".join(out)


def quote_summary_region(text: str, max_chars=2500) -> str:
    """Pull the SUMMARY / SECTION INCLUDES / SCOPE prose."""
    m = SUMMARY_RE.search(text)
    if not m:
        return text[:max_chars]
    start = m.start()
    return text[start:start + max_chars]


def main():
    out = {}
    for folder, label, pdfrel in PROJECTS:
        path = ROOT / folder / pdfrel
        rec = {"project": label, "pdf": str(path), "exists": path.exists()}
        if not path.exists():
            out[label] = rec
            continue
        try:
            with pdfplumber.open(str(path)) as pdf:
                rec["total_pages"] = len(pdf.pages)
                # find first page of each painting section
                ext = find_section_pages(pdf, [SECTION_HEADERS[0], SECTION_HEADERS[4]])
                inte = find_section_pages(pdf, [SECTION_HEADERS[1], SECTION_HEADERS[5]])
                stn = find_section_pages(pdf, [SECTION_HEADERS[2]])
                hpc = find_section_pages(pdf, [SECTION_HEADERS[3], SECTION_HEADERS[6]])
                rec["ext_pages"] = ext
                rec["int_pages"] = inte
                rec["stain_pages"] = stn
                rec["hpc_pages"] = hpc

                for name, pages, label_short in [
                    ("ext_summary", ext, "EXTERIOR PAINTING 09 91 13"),
                    ("int_summary", inte, "INTERIOR PAINTING 09 91 23"),
                    ("stain_summary", stn, "STAINING 09 93 00"),
                    ("hpc_summary", hpc, "HIGH-PERFORMANCE COATING 09 96 00"),
                ]:
                    if not pages:
                        rec[name] = None
                        continue
                    txt = extract_section(pdf, pages[0], max_pages=6)
                    rec[name] = {
                        "label": label_short,
                        "first_page": pages[0],
                        "summary": quote_summary_region(txt, 2200),
                    }
        except Exception as e:
            rec["error"] = str(e)
        out[label] = rec

    out_path = Path(r"C:/Agent Carol/data/memory/paint_scope_deep.json")
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"[wrote] {out_path}")

    # Pretty console summary
    for label, rec in out.items():
        print("=" * 88)
        print(f"  {label}")
        print("=" * 88)
        if not rec.get("exists"):
            print("  (no spec PDF)")
            continue
        print(f"  total pages: {rec.get('total_pages')}")
        for k in ("ext_summary", "int_summary", "stain_summary", "hpc_summary"):
            v = rec.get(k)
            if not v: continue
            print(f"\n  --- {v['label']} (first page {v['first_page']}) ---")
            # Trim to first ~1100 chars so console stays readable
            print("  " + v["summary"][:1100].replace("\n", "\n  "))
        print()


if __name__ == "__main__":
    main()
