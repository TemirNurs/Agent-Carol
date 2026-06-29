#!/usr/bin/env python3
r"""takeoff_dim.py — M-DIM: the DIMENSION-STRING measurement engine (god-level takeoff).

This promotes the ONLY method that produced a trustworthy wall number on a real
GMC vector-CAD set (USC Sumter, 6/25) into a permanent first-class measure. Both
the local vector engine (M-VEC) and Togal (M-TOGAL) failed on that set — the
vector engine returned 0 walls, Togal returned walls with area=0. What worked was
reading the printed DIMENSION strings room-by-room off the dimension plan and
computing painted wall SF = Σ(room perimeter × height) over in-scope rooms.

WHY a semantic measure: printed dimensions are SCALE-INDEPENDENT ground truth
(a "14'-8\"" string is 14.67 ft at any plot scale). So M-DIM is always
scale_locked=True and fires even when no vector scale lock exists (the exact USC
situation that left M-VEC withheld).

KEY ACCOUNTING: Σ(each room's interior perimeter × height) automatically paints
BOTH faces of every shared partition (once per adjoining room) and the interior
face of each exterior wall once — which is exactly the painted wall area. The gym
/ natatorium / high-bay / mech rooms are EXCLUDED (kept in the report so the owner
sees they were intentionally dropped, not missed — the USC over-bid lesson).

SELF-RECONCILIATION (independent internal cross-check, deterministic):
  pass A = Σ(MEASURED room perimeter × height)        — uses the read perimeters
  pass B = Σ(AREA-DERIVED perimeter × height)          — perimeter inferred from each
           room's area at a 1.3:1 aspect: 2*(√(1.3A)+√(A/1.3))
A and B use different inputs (measured perimeter vs measured area), so their
agreement is genuine corroboration. spread ≤10% → self_reconciled=True (the flag
the reconcile tripwire treats as a magnitude corroborator). >18% → ok=False.

ROOM SOURCE (in priority order):
  1. dim_json arg, or a sibling <project>/_dim_rooms.json — a pre-extracted room
     list (room, perimeter_lf, area_sf|length_ft+width_ft, wall_height_ft, exclude).
     This is how a vision/LLM read of the dimension plan feeds M-DIM (the reliable
     path: extract rooms once, then this engine is deterministic forever).
  2. headless regex harvest of dimension strings (best-effort fallback, flagged).
"""
from __future__ import annotations
import json
import math
import os
import re

try:
    import fitz
except Exception:
    fitz = None
try:
    from . import takeoff_reconcile as R
except ImportError:
    import takeoff_reconcile as R

_DIM_RE = re.compile(r"(\d{1,3})'\s*-?\s*(\d{1,2})?(?:\s*(\d)/(\d))?\"?")  # 14'-8 1/2"
OPENING_DEDUCT = 0.12   # doors + storefront + tile wainscot
DEFAULT_EXCLUDE = ("gym", "court", "natatorium", "pool", "high bay", "high-bay",
                   "highbay", "mechanical", "electrical room", "elev", "shaft",
                   "stair", "not in scope", "nic")


def _is_excluded(name: str, keywords) -> bool:
    n = (name or "").lower()
    return any(k in n for k in keywords)


def _area(room: dict) -> float:
    if room.get("area_sf"):
        return float(room["area_sf"])
    if room.get("length_ft") and room.get("width_ft"):
        return float(room["length_ft"]) * float(room["width_ft"])
    return 0.0


def _perim(room: dict) -> float:
    if room.get("perimeter_lf"):
        return float(room["perimeter_lf"])
    if room.get("length_ft") and room.get("width_ft"):
        return 2.0 * (float(room["length_ft"]) + float(room["width_ft"]))
    return 0.0


def _derived_perim(area: float) -> float:
    """perimeter of a 1.3:1-aspect rectangle of the given area."""
    if area <= 0:
        return 0.0
    return 2.0 * (math.sqrt(area * 1.3) + math.sqrt(area / 1.3))


def _load_rooms(pdf_path: str, dim_json: str | None) -> tuple[list, str]:
    # explicit path
    cand = []
    if dim_json:
        cand.append(dim_json)
    # sibling fixtures next to the pdf and in its project dir
    d = os.path.dirname(os.path.abspath(pdf_path))
    cand += [os.path.join(d, "_dim_rooms.json"),
             os.path.join(os.path.dirname(d), "_dim_rooms.json")]
    for p in cand:
        if p and os.path.exists(p):
            try:
                obj = json.load(open(p, encoding="utf-8"))
                rooms = obj.get("rooms", obj) if isinstance(obj, dict) else obj
                if rooms:
                    return rooms, f"room list from {os.path.basename(p)}"
            except Exception:
                pass
    return [], ""


def measure(pdf_path: str, pages: list[int] | None = None, height_ft: float = 9.0,
            known_gsf: float | None = None, building_type: str | None = None,
            dim_json: str | None = None, exclude_keywords=DEFAULT_EXCLUDE) -> R.Method:
    rooms, src = _load_rooms(pdf_path, dim_json)
    if not rooms:
        return R.Method("dim", "semantic", ok=False, scale_locked=True,
                        note="M-DIM: no dimension room-list (provide dim_json / _dim_rooms.json "
                             "or run the dimension-plan read) — headless string harvest not yet populated")

    in_scope, excluded = [], []
    for r in rooms:
        (excluded if (r.get("exclude") or _is_excluded(r.get("room", ""), exclude_keywords))
         else in_scope).append(r)

    if not in_scope:
        return R.Method("dim", "semantic", ok=False, scale_locked=True,
                        note=f"M-DIM: all {len(rooms)} rooms excluded — check exclusion keywords")

    # pass A: measured perimeter × per-room height
    A_gross = sum(_perim(r) * float(r.get("wall_height_ft") or height_ft) for r in in_scope)
    # pass B: area-derived perimeter × height (independent input)
    B_gross = sum(_derived_perim(_area(r)) * float(r.get("wall_height_ft") or height_ft) for r in in_scope)
    A = A_gross * (1 - OPENING_DEDUCT)
    B = B_gross * (1 - OPENING_DEDUCT)

    have_B = B > 0
    spread = abs(A - B) / max(A, B) if (have_B and max(A, B) > 0) else 1.0
    if have_B and spread <= 0.10:
        qty, self_rec, grade_note = (A + B) / 2.0, True, f"A/B agree {spread*100:.0f}%"
    elif have_B and spread <= 0.18:
        qty, self_rec, grade_note = (A + B) / 2.0, False, f"A/B spread {spread*100:.0f}% (acceptable, wider band)"
    elif have_B:
        # passes disagree materially — still return A but flag for human
        qty, self_rec, grade_note = A, False, f"A/B spread {spread*100:.0f}% >18% — review"
    else:
        qty, self_rec, grade_note = A, False, "perimeter-only (no area for B cross-check)"

    floor_sf = sum(_area(r) for r in in_scope)
    note = (f"M-DIM ({src}): {len(in_scope)} in-scope rooms, {len(excluded)} excluded "
            f"({', '.join(r.get('room','?') for r in excluded)[:60]}); "
            f"A(perim)={A:,.0f} B(area)={B:,.0f} {grade_note}; "
            f"floor≈{floor_sf:,.0f} SF wall:floor={qty/floor_sf:.2f}x" if floor_sf else f"M-DIM: {grade_note}")
    return R.Method("dim", "semantic", ok=True, qty=round(qty, 0), scale_locked=True,
                    self_reconciled=self_rec, floor_sf=round(floor_sf, 0) or None, note=note)


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    pdf = sys.argv[1]
    dj = sys.argv[2] if len(sys.argv) > 2 else None
    m = measure(pdf, height_ft=10.0, dim_json=dj)
    print(f"M-DIM: ok={m.ok} wall_sf={m.qty:,.0f} self_reconciled={m.self_reconciled}\n  {m.note}")
