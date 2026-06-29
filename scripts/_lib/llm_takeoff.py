#!/usr/bin/env python3
r"""llm_takeoff.py — M-LLM: the SEMANTIC measurement engine (god-level takeoff).

Independent from the geometry engine (M-VEC) because it reads MEANING, not
strokes: printed room-area tags ("694 HSF"), dimension strings, and the finish
schedule. Two engines that fail differently is what makes a GOLD pair valid.

Two levels, both real:
  (1) area_semantic(): sum the printed area tags -> an independent ceiling/floor
      SF measure, headless and deterministic. Always available on a vector sheet.
  (2) measure(): feed the extracted per-room areas + dimension strings + finish
      schedule + locked scale + height to the claude-code LLM chain (_lib/llm.py)
      and have it compute paint wall_sf / ceiling_sf / base_lf / doors. Grounded
      in extracted plan text, not vision.

The HIGHEST-fidelity M-LLM (per-unit-type read of dimensioned plans) is done by a
vision subagent; takeoff.py accepts that result via --llm-json. This module is the
headless engine that always runs so the reconciler has a second independent vote.
"""
from __future__ import annotations
import json
import re

try:
    import fitz
except Exception:
    fitz = None
try:
    from . import takeoff_reconcile as R
except ImportError:
    import takeoff_reconcile as R

_AREA_RE = re.compile(r'\b(\d{1,3}(?:,\d{3})*|\d{2,5})\s*(HSF|NSF|SF)\b', re.I)
_DIM_RE = re.compile(r"\b\d{1,2}'\s*-\s*\d{1,2}(?:\s*\d/\d{1,2})?\"")  # 12'-4 1/2"


def extract_plan_data(pdf_path: str, pages: list[int]) -> dict:
    """Pull the semantic anchors a painter reads: area tags, dim strings, finish text."""
    out = {"area_tags": [], "total_tagged_sf": 0, "dim_strings": 0, "finish_text": ""}
    if fitz is None:
        return out
    doc = fitz.open(pdf_path)
    seen = set()
    for pg in pages:
        if pg >= doc.page_count:
            continue
        t = doc[pg].get_text("text") or ""
        for m in _AREA_RE.finditer(t):
            val = int(m.group(1).replace(",", ""))
            if 40 <= val <= 20000:
                out["area_tags"].append(val)
        out["dim_strings"] += len(_DIM_RE.findall(t))
        if re.search(r"FINISH (MATRIX|SCHEDULE)|PAINT|PT-\d|SHERWIN", t, re.I):
            out["finish_text"] += t[:2500] + "\n"
    out["total_tagged_sf"] = sum(out["area_tags"])
    return out


def area_semantic(pdf_path: str, pages: list[int]) -> float:
    """Independent semantic AREA measure = sum of printed room-area tags."""
    return extract_plan_data(pdf_path, pages)["total_tagged_sf"]


def measure(pdf_path: str, pages: list[int], height_ft: float = 9.0,
            scale_locked: bool = True, units: int | None = None) -> R.Method:
    """Compute a paint takeoff via the LLM, grounded in extracted plan data.
    Returns a reconcile.Method(family='semantic').

    NOTE: this engine reads PRINTED values (area tags, dimension strings) — those
    are scale-INDEPENDENT (a '142 SF' tag is 142 SF whatever the plot scale). So a
    semantic measure is always scale_locked=True; only the geometry engine (M-VEC)
    needs the drawing scale. (Fix 6/21 — Unity Pharmacy: a valid semantic read was
    being wrongly dropped because the sheet's vector scale didn't lock.)"""
    data = extract_plan_data(pdf_path, pages)
    # WITHHOLD (don't emit a number) when there aren't enough area tags to credibly
    # measure the building. A handful of stray tags (e.g. 2 rooms on a 30-room plan)
    # yields a tiny wall number that then CONFLICTS with a good measure (M-DIM) and
    # causes a false REJECT. A measure that can't measure must abstain, not poison
    # the reconcile. (Fix 6/25 — USC: 2 tags -> 1,150 SF killed an 18k SILVER.)
    MIN_TAGS = 5
    if len(data["area_tags"]) < MIN_TAGS:
        return R.Method("llm", "semantic", ok=False,
                        note=f"only {len(data['area_tags'])} printed area tags found (<{MIN_TAGS}) — "
                             f"insufficient for a credible semantic AREA read; vote WITHHELD "
                             f"(M-DIM dimension read covers vector-CAD sets)")
    prompt = (
        "You are a commercial painting estimator computing a takeoff from extracted plan data. "
        "Compute paintable quantities and return STRICT JSON only.\n"
        f"Ceiling height: {height_ft} ft. Printed room-area tags (SF) on the floor plans: "
        f"{data['area_tags'][:120]} (total {data['total_tagged_sf']:,} SF, {len(data['area_tags'])} rooms). "
        f"Dimension strings present: {data['dim_strings']}. "
        f"Finish notes: {data['finish_text'][:1500] or 'standard latex walls/ceilings, semi-gloss trim'}.\n"
        + (f"Building has {units} dwelling units.\n" if units else "")
        + "Rules: CEILING_SF = sum of room areas (painted gyp ceilings). WALL_SF = sum over rooms of "
        "(room perimeter x height); estimate each room's perimeter from its area assuming a typical "
        "1.3:1 room aspect (perimeter ~= 2*(sqrt(A*1.3)+sqrt(A/1.3))), minus ~21 SF per door opening "
        "(~1.4 doors/room) and ~15 SF per window (~1/room). BASE_LF = sum of room perimeters. "
        "DOORS = ~1.4 x room count. Return JSON: "
        '{"wall_sf":int,"ceiling_sf":int,"base_lf":int,"doors":int,"basis":"<1 line>"}'
    )
    try:
        from _lib import llm as LLM
    except Exception:
        import importlib, sys, os
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        from _lib import llm as LLM
    res = LLM.chat_json([{"role": "user", "content": prompt}], max_tokens=500)
    if res.get("error") or "wall_sf" not in res:
        # deterministic fallback grounded in the tagged area (still a real semantic measure)
        import math
        A = data["total_tagged_sf"]
        nrooms = len(data["area_tags"])
        peri = sum(2 * (math.sqrt(a * 1.3) + math.sqrt(a / 1.3)) for a in data["area_tags"])
        wall = round(peri * height_ft - nrooms * (1.4 * 21 + 15))
        return R.Method("llm", "semantic", ok=True, qty=max(wall, 0), scale_locked=True,
                        note=f"semantic fallback: {nrooms} tagged rooms, {A:,} SF area, perimeter x {height_ft}ft")
    return R.Method("llm", "semantic", ok=True, qty=float(res["wall_sf"]), scale_locked=True,
                    note=f"LLM semantic read ({res.get('_model','?')}): {res.get('basis','')[:80]}")


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    pdf = sys.argv[1]; pages = [int(x) for x in (sys.argv[2].split(",") if len(sys.argv) > 2 else ["0"])]
    m = measure(pdf, pages, height_ft=9.1)
    print(f"M-LLM: ok={m.ok} wall_sf={m.qty:,.0f} -> {m.note}")
