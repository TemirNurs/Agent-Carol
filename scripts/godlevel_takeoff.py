#!/usr/bin/env python3
r"""godlevel_takeoff.py — the ONE god-level takeoff entry: find the best source, route it.

Carol's takeoff swung because it measured a degraded PICTURE without knowing the
input's fidelity. This orchestrator embodies the fix: given a project folder (or a
single file), it finds the HIGHEST-fidelity source available and routes to the right
extractor, so the answer is as exact as the input allows — and it says so out loud.

  FIDELITY LADder (best first):
    .ifc / .ifczip  -> M-IFC  (IfcOpenShell: exact NetSideArea / covering NetArea)   GOLD
    .dxf            -> M-DXF  (ezdxf: exact wall length on A-WALL, door counts)       GOLD
    layered PDF     -> M-LAYER + M-DIM/M-VEC/M-EXT (isolate A-WALL OCG layers)         GOLD-eligible
    flattened PDF   -> M-DIM/M-VEC/M-EXT multi-measure                                 capped SILVER
    raster PDF      -> OCR scale + dims; FLAG + request a better file                  capped SILVER/REJECT

Usage:
  python scripts/godlevel_takeoff.py <project_dir_or_file> [--building-type athletic] [--known-gsf N] [--height 10]
"""
from __future__ import annotations
import argparse
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from _lib import pdf_tier_triage as TRI
import takeoff as TK

_TIER_RANK = {"T1_LAYERED": 0, "T2_FLATTENED": 1, "T3_RASTER": 2}


_DRAWING_HINT = ("drawing", "plan", "sheet", "arch", "_a", "a1.", "a-", "floor")
_NONDRAWING_HINT = ("manual", "spec", "specification", "addend", "geotech", "report",
                    "narrative", "bid form", "se-330", "se-310", "invitation", "scope")


def _name_score(path: str) -> int:
    """0 = looks like a DRAWING set (preferred); 2 = looks like a spec/manual (demote)."""
    n = os.path.basename(path).lower()
    if any(h in n for h in _NONDRAWING_HINT):
        return 2
    if any(h in n for h in _DRAWING_HINT):
        return 0
    return 1


def _find_best_pdf(pdfs: list[str]) -> tuple[str, dict] | tuple[None, None]:
    """Pick the DRAWING-set PDF with the best tier. Sort: name-type (drawings before
    spec books) → tier (layered>flattened>raster) → most pages."""
    best, best_key, best_t = None, (9, 9, 0), None
    for p in pdfs:
        try:
            t = TRI.triage(p)
        except Exception:
            continue
        key = (_name_score(p), _TIER_RANK.get(t.get("tier"), 5), -int(t.get("n_pages") or 0))
        if key < best_key:
            best, best_key, best_t = p, key, t
    return best, best_t


def find_best_source(path: str) -> dict:
    """Return {kind, path, tier, candidates} for the highest-fidelity source."""
    if os.path.isfile(path):
        ext = os.path.splitext(path)[1].lower()
        kind = {"ifc": "ifc", "ifczip": "ifc", "dxf": "dxf", "pdf": "pdf"}.get(ext.lstrip("."), "pdf")
        t = TRI.triage(path)
        return {"kind": kind, "path": path, "tier": t.get("tier"), "triage": t, "candidates": [path]}
    # directory: search recursively
    ifcs = glob.glob(os.path.join(path, "**", "*.ifc"), recursive=True) + \
        glob.glob(os.path.join(path, "**", "*.ifczip"), recursive=True)
    dxfs = glob.glob(os.path.join(path, "**", "*.dxf"), recursive=True)
    dwgs = glob.glob(os.path.join(path, "**", "*.dwg"), recursive=True)
    pdfs = [p for p in glob.glob(os.path.join(path, "**", "*.pdf"), recursive=True)
            if not os.path.basename(p).startswith("_")]
    if ifcs:
        return {"kind": "ifc", "path": ifcs[0], "tier": "T0_IFC", "candidates": ifcs}
    if dxfs:
        return {"kind": "dxf", "path": dxfs[0], "tier": "T0_CAD", "candidates": dxfs}
    if pdfs:
        best, t = _find_best_pdf(pdfs)
        if best:
            out = {"kind": "pdf", "path": best, "tier": t.get("tier"), "triage": t, "candidates": pdfs}
            if dwgs:
                out["dwg_available"] = dwgs[:3]   # DWG present but needs ODA→DXF conversion
            return out
    return {"kind": None, "path": None, "tier": None, "candidates": [], "error": "no source files found"}


def run(path: str, **kw) -> dict:
    src = find_best_source(path)
    if not src.get("path"):
        return {"source": src, "verdict": {"grade": "REJECT", "reasons": ["no drawings found"]}}
    kind, fp = src["kind"], src["path"]
    if kind == "ifc":
        rep = TK.run(fp, pages=[], ifc_path=fp, **kw)
    elif kind == "dxf":
        rep = TK.run(fp, pages=[], dxf_path=fp, **kw)
    else:
        rep = TK.run(fp, pages=[], **kw)
    rep["source_selected"] = {"kind": kind, "file": os.path.basename(fp), "tier": src.get("tier")}
    # upstream recommendation when the best available source is degraded
    tier = src.get("tier")
    if tier in ("T2_FLATTENED", "T3_RASTER"):
        rep["upstream_recommendation"] = (
            "Best available source is a DEGRADED export (" + str(tier) + "). For a GOLD-grade number, "
            "request from the GC: a vector PDF re-export (Bluebeam/CAD, not Print-to-PDF), the DWG/DXF "
            "background, or the IFC model. " + ("A DWG IS present in the set — convert via ODA File "
            "Converter to DXF and re-run with --dxf for exact geometry." if src.get("dwg_available") else ""))
    return rep


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path", help="project directory or a single file (.ifc/.dxf/.pdf)")
    ap.add_argument("--building-type", default=None, dest="building_type")
    ap.add_argument("--known-gsf", type=float, default=None, dest="known_gsf")
    ap.add_argument("--height", type=float, default=9.0, dest="height_ft")
    a = ap.parse_args()
    rep = run(a.path, building_type=a.building_type, known_gsf=a.known_gsf, height_ft=a.height_ft)
    src = rep.get("source_selected", {})
    print(f"\nSOURCE SELECTED: {src.get('kind','?')} — {src.get('file','?')}  [{src.get('tier','?')}]")
    v = rep.get("verdict", {})
    print("=" * 70)
    print(f"  INTERIOR: {v.get('grade')}   wall_sf = {('%s' % format(v['value'], ',.0f')) if v.get('value') else 'NONE'}")
    for r in v.get("reasons", [])[:3]:
        print("   - " + r)
    if rep.get("verdict_exterior"):
        ve = rep["verdict_exterior"]
        print(f"  EXTERIOR: {ve.get('grade')}   exterior_sf = {('%s' % format(ve['value'], ',.0f')) if ve.get('value') else 'NONE'}")
    if rep.get("upstream_recommendation"):
        print("  UPSTREAM: " + rep["upstream_recommendation"][:200])
    print("=" * 70)


if __name__ == "__main__":
    main()
