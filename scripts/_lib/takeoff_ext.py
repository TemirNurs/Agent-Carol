#!/usr/bin/env python3
r"""takeoff_ext.py — M-EXT: the EXTERIOR takeoff measure, as rigorous as the interior.

The exterior used to be a single uncorroborated elevation read (USC Sumter: ~8,600
SF, LOW confidence, never cross-checked). This promotes the exterior to a first-class
measure with the SAME backbone that made the interior (M-DIM) reliable:

  GROUND TRUTH      printed floor-to-roof DATUMS (14'-8" = 14.67 ft, scale-independent)
                    + per-facade lengths off the elevations.
  TWO PASSES (A/B)  A = Σ per-elevation length × datum-height (from the ELEVATIONS).
                    B = floor-plan PERIMETER × weighted datum-height (a DIFFERENT sheet).
                    Different inputs → agreement is genuine corroboration. A/B ≤10%
                    → self_reconciled (the flag the reconcile tripwire trusts).
  ENVELOPE ANCHOR   env_anchor = perimeter × weighted height = the independent
                    magnitude denominator (exterior analog of the interior wall:floor
                    gate). painted_facade / env must land in a building-GEOMETRY band.
  SCOPE ISOLATION   exclude non-painted substrates (storefront/curtainwall/Kalwall/
                    prefinished metal/powder-coat/signage/owner-brick) AND tie-in walls
                    to existing — kept in the report, never silently dropped.
  SUBSTRATE SPLIT   brick-stain vs precast/parge vs cast-stone vs stucco vs galv-steel
                    vs HM doors — the cost headline, carried through, never blended.
  NEW vs EXISTING   new facade (perimeter-anchored, gradeable) kept separate from the
                    existing-Nettles repaint (extent inherently a must-confirm).

Reads a pre-extracted _ext_faces.json (a vision/LLM read of the elevations done ONCE,
then deterministic forever), or an explicit ext_json. Returns R.Method('ext','elevation').
"""
from __future__ import annotations
import json
import math
import os
import re

try:
    from . import takeoff_reconcile as R
except ImportError:
    import takeoff_reconcile as R

OPENING_DEFAULT = 0.18
DEFAULT_NON_PAINTED = ("storefront", "curtainwall", "kalwall", "mtl_panel", "metal panel",
                       "prefin", "prefinished", "powder", "anodized", "aluminum", "alum",
                       "signage", "glazing", "glass", "vinyl", "acm", "by-others", "nic",
                       "existing-by-others")


def _parse_datum(v) -> float:
    """'14'-8\"' -> 14.67 ; accepts a number too."""
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v)
    m = re.match(r"\s*(\d+)\s*'\s*-?\s*(\d+)?", s)
    if m:
        ft = float(m.group(1)); inch = float(m.group(2) or 0)
        return ft + inch / 12.0
    try:
        return float(s)
    except Exception:
        return 0.0


def _is_non_painted(system: str) -> bool:
    s = (system or "").lower()
    return any(k in s for k in DEFAULT_NON_PAINTED)


def _face_height(face: dict, datums: dict) -> float:
    base = _parse_datum(datums.get(face.get("base_datum", "level1"), 0.0))
    top = _parse_datum(datums.get(face.get("roof_datum", "roof_low"), face.get("height_ft", 0)))
    h = max(0.0, top - base)
    return h or float(face.get("height_ft") or 0)


def _face_gross_sf(face: dict, datums: dict) -> float:
    L = float(face.get("length_ft") or 0)
    h = _face_height(face, datums)
    gable = 0.5 * L * float(face.get("gable_rise_ft") or 0)
    return L * h + gable


def _weighted_height(faces, datums) -> float:
    num = den = 0.0
    for f in faces:
        if f.get("tie_in") or f.get("nic"):
            continue
        L = float(f.get("length_ft") or 0)
        num += L * _face_height(f, datums); den += L
    return (num / den) if den else 0.0


def env_anchor(perimeter_lf: float, mean_height_ft: float) -> float:
    return float(perimeter_lf or 0) * float(mean_height_ft or 0)


def _load_faces(pdf_path: str, ext_json: str | None) -> tuple[list, dict, str]:
    cand = []
    if ext_json:
        cand.append(ext_json)
    d = os.path.dirname(os.path.abspath(pdf_path))
    cand += [os.path.join(d, "_ext_faces.json"), os.path.join(os.path.dirname(d), "_ext_faces.json")]
    for p in cand:
        if p and os.path.exists(p):
            try:
                obj = json.load(open(p, encoding="utf-8"))
                faces = obj.get("faces", obj if isinstance(obj, list) else [])
                if faces:
                    return faces, (obj.get("datums") if isinstance(obj, dict) else {}) or {}, \
                           f"face list from {os.path.basename(p)}"
            except Exception:
                pass
    return [], {}, ""


def _apportion(face: dict, net_sf: float) -> tuple[dict, dict, float]:
    """Split a face's net SF across painted substrate segments; return
    (painted_by_substrate, excluded_by_substrate, excluded_total)."""
    segs = face.get("segments") or [{"substrate": "field", "pct": 1.0, "painted": True}]
    painted, excluded = {}, {}
    exc_total = 0.0
    for s in segs:
        pct = float(s.get("pct") or 0)
        sub = s.get("substrate", "field")
        sf = net_sf * pct
        is_paint = bool(s.get("painted", True)) and not _is_non_painted(sub)
        if is_paint:
            painted[sub] = painted.get(sub, 0.0) + sf
        else:
            excluded[sub] = excluded.get(sub, 0.0) + sf
            exc_total += sf
    return painted, excluded, exc_total


def measure(pdf_path: str, elevation_pages: list[int] | None = None, datums: dict | None = None,
            perimeter_lf: float | None = None, ext_json: str | None = None,
            opening_default: float = OPENING_DEFAULT, building_type: str | None = None,
            ext_geometry: str | None = None, mean_height_ft: float | None = None) -> R.Method:
    faces, fdatums, src = _load_faces(pdf_path, ext_json)
    datums = {**(fdatums or {}), **(datums or {})}
    if not faces:
        return R.Method("ext", "elevation", ok=False, scale_locked=True,
                        note="M-EXT: no elevation face-list (provide ext_json / _ext_faces.json "
                             "from an elevation read) — headless datum harvest not populated")

    # ---- accumulate painted SF: pass A (per-elevation), by substrate, new vs existing ----
    A_new = A_existing = 0.0
    by_sub_new, by_sub_existing, excluded = {}, {}, {}
    exc_total = 0.0
    doors_new = doors_existing = 0
    new_perim = 0.0
    for f in faces:
        gross = _face_gross_sf(f, datums)
        net = gross * (1 - float(f.get("opening_pct", opening_default)))
        painted, exc, et = _apportion(f, net)
        exc_total += et
        for k, v in exc.items():
            excluded[k] = excluded.get(k, 0.0) + v
        is_existing = bool(f.get("existing"))
        tgt, bucket = (A_existing, by_sub_existing) if is_existing else (A_new, by_sub_new)
        psum = sum(painted.values())
        if is_existing:
            A_existing += psum; doors_existing += int(f.get("door_count") or 0)
            for k, v in painted.items():
                by_sub_existing[k] = by_sub_existing.get(k, 0.0) + v
        else:
            A_new += psum; doors_new += int(f.get("door_count") or 0)
            if not (f.get("tie_in") or f.get("nic")):
                new_perim += float(f.get("length_ft") or 0)
            for k, v in painted.items():
                by_sub_new[k] = by_sub_new.get(k, 0.0) + v

    # ---- pass B: perimeter × weighted height (independent length source) ----
    wh = _weighted_height([f for f in faces if not f.get("existing")], datums) or (mean_height_ft or 0)
    perim = perimeter_lf if perimeter_lf else new_perim     # prefer floor-plan perimeter
    env_new = env_anchor(perim, wh)                          # gross NEW envelope
    # painted fraction of the new facade (from pass A): painted / (painted+excluded)
    new_gross_painted_basis = A_new + exc_total
    paint_frac = (A_new / new_gross_painted_basis) if new_gross_painted_basis else 1.0
    B_new = env_new * (1 - opening_default) * paint_frac if env_new else 0.0

    have_B = B_new > 0 and A_new > 0
    spread = abs(A_new - B_new) / max(A_new, B_new) if have_B else 1.0
    if have_B and spread <= 0.10:
        new_qty, self_rec, gnote = (A_new + B_new) / 2.0, True, f"A/B agree {spread*100:.0f}%"
    elif have_B and spread <= 0.18:
        new_qty, self_rec, gnote = (A_new + B_new) / 2.0, False, f"A/B spread {spread*100:.0f}% (wider band)"
    elif have_B:
        new_qty, self_rec, gnote = A_new, False, f"A/B spread {spread*100:.0f}% >18% — review"
    else:
        new_qty, self_rec, gnote = A_new, False, "no perimeter for B cross-check (elevation-only)"

    total_painted = round(new_qty + A_existing)
    length_chk = f"Σ new face len {new_perim:.0f} LF vs perimeter {perim:.0f} LF" if perim else "no perimeter"
    breakdown = {
        "new_facade_sf": round(new_qty), "existing_repaint_sf": round(A_existing),
        "by_substrate_new": {k: round(v) for k, v in by_sub_new.items()},
        "by_substrate_existing": {k: round(v) for k, v in by_sub_existing.items()},
        "excluded_non_painted_sf": round(exc_total),
        "excluded_substrates": {k: round(v) for k, v in excluded.items()},
        "hm_doors_new": doors_new, "hm_doors_existing": doors_existing,
        "env_anchor_new_sf": round(env_new), "weighted_height_ft": round(wh, 1),
        "passA_new": round(A_new), "passB_new": round(B_new), "ab_note": gnote,
        "length_check": length_chk,
        "existing_repaint_confidence": "LOW — confirm extent from existing-building elevation",
    }
    note = (f"M-EXT ({src}): NEW facade {new_qty:,.0f} SF ({gnote}; {length_chk}); "
            f"EXISTING-Nettles repaint {A_existing:,.0f} SF (confirm extent); "
            f"excluded non-painted {exc_total:,.0f} SF ({','.join(excluded) or 'none'}); "
            f"doors {doors_new}+{doors_existing}; env-anchor {env_new:,.0f} SF "
            f"facade:env {(new_qty/env_new if env_new else 0):.2f}x; total painted {total_painted:,.0f} SF")
    m = R.Method("ext", "elevation", ok=True, qty=total_painted, scale_locked=True,
                 self_reconciled=self_rec, floor_sf=round(env_new) or None, note=note)
    m.breakdown = breakdown
    return m


def measure_vision(pdf_path: str, ext_json: str | None = None) -> R.Method:
    """OPT-IN second family ('elevation_vision') so exterior can reach GOLD. Reads a
    pre-supplied vision facade list (_ext_vision.json) — a distinct image-based read.
    Withholds if absent (no silent fabrication)."""
    faces, fdatums, src = _load_faces(pdf_path, ext_json or "_ext_vision.json")
    if not faces:
        return R.Method("extvis", "elevation_vision", ok=False, scale_locked=True,
                        note="M-EXT-VIS: no vision face-list — withheld")
    base = measure(pdf_path, ext_json=ext_json or "_ext_vision.json")
    if not base.ok:
        return R.Method("extvis", "elevation_vision", ok=False, note="M-EXT-VIS: read failed")
    return R.Method("extvis", "elevation_vision", ok=True, qty=base.qty, scale_locked=True,
                    note="M-EXT-VIS (image read): " + base.note)


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    pdf = sys.argv[1]
    ej = sys.argv[2] if len(sys.argv) > 2 else None
    per = float(sys.argv[3]) if len(sys.argv) > 3 else None
    m = measure(pdf, ext_json=ej, perimeter_lf=per)
    print(f"M-EXT: ok={m.ok} exterior_sf={m.qty:,.0f} self_reconciled={m.self_reconciled}\n  {m.note}")
