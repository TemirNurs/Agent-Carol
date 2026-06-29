#!/usr/bin/env python3
r"""layer_takeoff.py — M-LAYER: the T1 LAYERED-VECTOR-PDF reader (god-level takeoff).

The clean-PDF unlock NO commercial AI takeoff tool uses. Construction PDFs exported
from Revit/CAD often carry their Optional Content Groups (OCGs = PDF layers), one per
NCS layer (A-WALL-FULL-N, A-DOOR, A-GLAZ, A-AREA-...). When those survive, we don't
have to disentangle wall faces from dimension strings, furniture, grid and poché hatch
the way the whole-sheet vector engine (M-VEC) must — we ISOLATE the wall layers up
front and measure only those strokes. That makes the geometry clean enough to pair
with M-DIM's semantic read for a GOLD verdict.

WHY a distinct family from M-VEC: both are 'geometry', but M-LAYER is pre-filtered by
layer membership rather than by parallel-pair heuristics on the full stream. It is the
T1 (tier-1, cleanest input) path; M-VEC is the T2 fallback for flattened PDFs.

HARD HONESTY (the USC case): if the PDF's layers are stripped (a flattened plot, or a
civil/title-only OCG set with no architectural wall/door/room layer), there is NO clean
geometry to isolate — measure() returns ok=False with a clear reason and WITHHOLDS its
vote. It never guesses a wall number off a layerless sheet. ncs_layers.has_arch_layers
is the gate.

ACCOUNTING:
  wall_lf  = Σ length of wall-layer centerline runs (deduped parallel doubles so one
             physical wall counts once), measured the same parallel-pair way as M-VEC
             but PRE-FILTERED to WALL_ADD layers (clean — no hatch/dim contamination).
  wall_sf  = wall_lf × height_ft × face_factor   (face_factor 2.0 = both faces).
  doors    = count of DOOR_COUNT-layer blocks/path-groups (plan-counted, not factored).
  Net of openings: GLAZ_SUBTRACT-layer runs are deducted from gross wall LF before SF.

SCALE: accept scale_drawing (inches/ft on paper, e.g. 0.1875 for 3/16"=1') or lock it
via takeoff_calibrate (two independent anchors agreeing). No scale → vote withheld
(an unscaled geometry measure is worthless — the reconciler would drop it anyway).

self_reconciled = wall layers isolated AND scale locked (the two conditions that make
this a clean, trustworthy geometry vote that can stand as a magnitude corroborator).
"""
from __future__ import annotations

import math
import os

# --- IMPORT GUARD: never crash if PyMuPDF is missing; withhold the vote instead. ---
try:
    import fitz  # PyMuPDF
except Exception:  # pragma: no cover
    fitz = None

try:
    from . import takeoff_reconcile as R
except ImportError:
    import takeoff_reconcile as R

try:
    from . import ncs_layers as NCS
except ImportError:
    import ncs_layers as NCS

try:
    from . import takeoff_calibrate as CAL
except ImportError:
    try:
        import takeoff_calibrate as CAL
    except Exception:  # pragma: no cover
        CAL = None

# wall-thickness band + noise gates, mirrored from vector_takeoff so the two
# geometry engines speak the same units and a side-by-side comparison is honest.
WALL_MIN_FT = 0.29      # 3.5" stud partition
WALL_MAX_FT = 1.15      # 13.8" masonry + furring
MIN_SEG_FT = 1.5        # ignore strokes shorter than this (hatch/ticks)
MIN_OVERLAP_FT = 2.0    # parallel pair must overlap this much along the wall
MIN_RUN_FT = 4.0        # merged wall runs shorter than this = casework/frames
OFF_TOL_FT = 1.5        # parallel doubles within this normal offset = one wall


def _drawing_layer_name(doc, path: dict, ocg_names: dict) -> str | None:
    """Resolve the OCG layer name a get_drawings() path belongs to. PyMuPDF exposes
    the optional-content xref under the 'layer' key (newer) or nests it in the path's
    'oc' xref; map that xref to its OCG name."""
    xref = path.get("layer")
    if isinstance(xref, str):
        # some builds already give the name string
        return xref
    if xref is None:
        xref = path.get("oc")
    if xref is None:
        return None
    try:
        return ocg_names.get(int(xref))
    except Exception:
        return None


def _ocg_name_map(doc) -> dict:
    """xref -> OCG layer name."""
    out = {}
    try:
        ocgs = doc.get_ocgs() or {}
    except Exception:
        return out
    for xref, info in ocgs.items():
        try:
            out[int(xref)] = (info or {}).get("name") if isinstance(info, dict) else str(info)
        except Exception:
            pass
    return out


def _segments_from_paths(paths) -> list:
    """Straight segments (pt coords) from a list of get_drawings() path dicts."""
    segs = []
    for path in paths:
        for item in path.get("items", []):
            kind = item[0]
            if kind == "l":
                p1, p2 = item[1], item[2]
                segs.append((p1.x, p1.y, p2.x, p2.y))
            elif kind == "re":
                r = item[1]
                segs += [(r.x0, r.y0, r.x1, r.y0), (r.x1, r.y0, r.x1, r.y1),
                         (r.x1, r.y1, r.x0, r.y1), (r.x0, r.y1, r.x0, r.y0)]
    return segs


def _centerline_lf(segs, pt_per_ft: float) -> float:
    """Parallel-pair wall detection -> total deduped centerline LF (feet).

    Same physics as vector_takeoff._extract_centerlines but WITHOUT the diagonal-hatch
    rejection: the input is already layer-isolated to wall faces, so there is no hatch
    to fight. Collapses parallel doubles (veneer/CMU drawn as 2-3 lines) so one physical
    wall counts once."""
    if not segs or pt_per_ft <= 0:
        return 0.0
    import numpy as np
    a = np.asarray(segs, dtype=np.float64)
    dx, dy = a[:, 2] - a[:, 0], a[:, 3] - a[:, 1]
    length = np.hypot(dx, dy)
    keep = length >= MIN_SEG_FT * pt_per_ft
    a, dx, dy, length = a[keep], dx[keep], dy[keep], length[keep]
    if not len(a):
        return 0.0
    ang = np.degrees(np.arctan2(dy, dx)) % 180.0
    t_lo, t_hi = WALL_MIN_FT * pt_per_ft, WALL_MAX_FT * pt_per_ft
    min_ov = MIN_OVERLAP_FT * pt_per_ft
    runs = []  # (ang_deg, off, lo, hi) merged centerline pieces
    for bucket in np.unique(np.round(ang)):
        sel = np.abs(ang - bucket) <= 0.75
        if sel.sum() < 2:
            continue
        b = a[sel]
        th = math.radians(bucket)
        u, v = math.cos(th), math.sin(th)
        s1 = b[:, 0] * u + b[:, 1] * v
        s2 = b[:, 2] * u + b[:, 3] * v
        o = (b[:, 0] + b[:, 2]) / 2 * (-v) + (b[:, 1] + b[:, 3]) / 2 * u
        smin, smax = np.minimum(s1, s2), np.maximum(s1, s2)
        order = np.argsort(o)
        o_s, smin_s, smax_s = o[order], smin[order], smax[order]
        n = len(o_s)
        spans = []
        for i in range(n):
            j = i + 1
            while j < n and o_s[j] - o_s[i] <= t_hi:
                if o_s[j] - o_s[i] >= t_lo:
                    lo_ = max(smin_s[i], smin_s[j])
                    hi_ = min(smax_s[i], smax_s[j])
                    if hi_ - lo_ >= min_ov:
                        spans.append(((o_s[i] + o_s[j]) / 2, lo_, hi_))
                j += 1
        if not spans:
            continue
        spans.sort()
        merged = []
        for off, lo_, hi_ in spans:
            placed = False
            for m in merged:
                if abs(m[0] - off) <= t_lo * 0.9 and lo_ <= m[2] + min_ov * 0.5 \
                        and hi_ >= m[1] - min_ov * 0.5:
                    m[1], m[2] = min(m[1], lo_), max(m[2], hi_)
                    m[0] = (m[0] + off) / 2
                    placed = True
                    break
            if not placed:
                merged.append([off, lo_, hi_])
        min_run = MIN_RUN_FT * pt_per_ft
        for off, lo_, hi_ in merged:
            if hi_ - lo_ < min_run:
                continue
            runs.append((bucket, off, lo_, hi_))
    if not runs:
        return 0.0
    # dedup parallel doubles: same angle (<=2deg), within OFF_TOL normal offset, overlap
    off_tol = OFF_TOL_FT * pt_per_ft
    used = [False] * len(runs)
    total_pt = 0.0
    for i in range(len(runs)):
        if used[i]:
            continue
        ai, oi, lo_i, hi_i = runs[i]
        best_lo, best_hi = lo_i, hi_i
        for j in range(i + 1, len(runs)):
            if used[j]:
                continue
            aj, oj, lo_j, hi_j = runs[j]
            dang = min(abs(ai - aj), 180 - abs(ai - aj))
            if dang <= 2.0 and abs(oi - oj) <= off_tol and min(hi_i, hi_j) - max(lo_i, lo_j) > 0:
                used[j] = True
                if (hi_j - lo_j) > (best_hi - best_lo):
                    best_lo, best_hi = lo_j, hi_j
        used[i] = True
        total_pt += (best_hi - best_lo)
    return total_pt / pt_per_ft


def _count_door_blocks(doc, paths_by_layer: dict) -> int:
    """Count door instances on DOOR_COUNT layers. A door symbol = one path group
    (leaf rectangle + swing arc) per opening; count path groups, not raw strokes,
    so a single door isn't over-counted. Plan-COUNT, never factored."""
    n = 0
    for name, paths in paths_by_layer.items():
        if NCS.classify_layer(name).get("role") == "DOOR_COUNT":
            n += len(paths)
    return n


def measure(pdf_path: str, pages: list[int] | None = None, scale_drawing: float | None = None,
            height_ft: float = 10.0, scope: str = "new", face_factor: float = 2.0) -> R.Method:
    """M-LAYER measure. See module docstring. Returns R.Method('layer','geometry', ...)."""
    if fitz is None:
        return R.Method("layer", "geometry", ok=False,
                        note="M-LAYER: PyMuPDF (fitz) not installed — vote withheld")

    if not os.path.exists(pdf_path):
        return R.Method("layer", "geometry", ok=False,
                        note=f"M-LAYER: file not found ({pdf_path}) — vote withheld")

    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        return R.Method("layer", "geometry", ok=False,
                        note=f"M-LAYER: cannot open PDF ({e}) — vote withheld")

    try:
        ocg_names = _ocg_name_map(doc)
        layer_names = [v for v in ocg_names.values() if v]

        # GATE 1: are there architectural layers at all? (THE USC withhold path.)
        if not NCS.has_arch_layers(layer_names):
            doc.close()
            note = ("M-LAYER: PDF layers stripped / civil-title only — not a layered arch "
                    "PDF; use M-VEC/M-DIM")
            m = R.Method("layer", "geometry", ok=False, note=note)
            m.breakdown = {"arch_layers_present": False, "ocg_count": len(ocg_names),
                           "wall_layer_names": [], "wall_lf": 0.0, "door_count": 0,
                           "scale_used": None}
            return m

        # classify layers; collect names by role (respect scope for -E/repaint)
        wall_layer_names, glaz_layer_names, door_layer_names = [], [], []
        for nm in layer_names:
            role = NCS.classify_layer(nm, scope=scope).get("role")
            if role == "WALL_ADD":
                wall_layer_names.append(nm)
            elif role == "GLAZ_SUBTRACT":
                glaz_layer_names.append(nm)
            elif role == "DOOR_COUNT":
                door_layer_names.append(nm)

        if not wall_layer_names:
            doc.close()
            note = ("M-LAYER: arch layers present but NO wall layer (WALL/PRTN/COLS) isolated "
                    "— cannot measure painted wall; vote withheld")
            m = R.Method("layer", "geometry", ok=False, note=note)
            m.breakdown = {"arch_layers_present": True, "wall_layer_names": [],
                           "wall_lf": 0.0, "door_count": 0, "scale_used": None}
            return m

        # GATE 2: scale lock
        page_idxs = pages if pages is not None else list(range(doc.page_count))
        scale_used = scale_drawing
        scale_locked = scale_drawing is not None
        scale_src = "supplied scale_drawing" if scale_drawing is not None else None
        if scale_used is None and CAL is not None:
            sr = CAL.calibrate_set(pdf_path, page_idxs)
            if sr.locked and sr.inches_per_foot:
                scale_used = sr.inches_per_foot
                scale_locked = True
                scale_src = f"calibrate ({sr.confidence})"
        if scale_used is None or scale_used <= 0:
            doc.close()
            note = ("M-LAYER: wall layers isolated but NO scale lock (supply scale_drawing or "
                    "two independent calibration anchors) — geometry vote withheld")
            m = R.Method("layer", "geometry", ok=False, scale_locked=False, note=note)
            m.breakdown = {"arch_layers_present": True, "wall_layer_names": wall_layer_names,
                           "wall_lf": 0.0, "door_count": 0, "scale_used": None}
            return m

        pt_per_ft = scale_used * 72.0
        wall_set = {n.upper() for n in wall_layer_names}
        glaz_set = {n.upper() for n in glaz_layer_names}

        wall_segs, glaz_segs = [], []
        paths_by_layer: dict = {}
        for pi in page_idxs:
            if pi >= doc.page_count:
                continue
            page = doc[pi]
            for path in page.get_drawings():
                nm = _drawing_layer_name(doc, path, ocg_names)
                if not nm:
                    continue
                paths_by_layer.setdefault(nm, []).append(path)
                up = nm.upper()
                if up in wall_set:
                    wall_segs += _segments_from_paths([path])
                elif up in glaz_set:
                    glaz_segs += _segments_from_paths([path])

        wall_lf = _centerline_lf(wall_segs, pt_per_ft)
        glaz_lf = _centerline_lf(glaz_segs, pt_per_ft) if glaz_segs else 0.0
        net_lf = max(0.0, wall_lf - glaz_lf)
        door_count = _count_door_blocks(doc, paths_by_layer)
        doc.close()

        if net_lf <= 0:
            note = ("M-LAYER: wall layers present but no measurable wall length isolated "
                    "(strokes below min-run / no parallel pairs) — vote withheld")
            m = R.Method("layer", "geometry", ok=False, scale_locked=scale_locked, note=note)
            m.breakdown = {"arch_layers_present": True, "wall_layer_names": wall_layer_names,
                           "wall_lf": round(wall_lf, 1), "door_count": door_count,
                           "scale_used": scale_used}
            return m

        wall_sf = net_lf * height_ft * face_factor
        self_rec = bool(wall_layer_names and scale_locked)
        note = (f"M-LAYER (T1 layered-PDF): {len(wall_layer_names)} wall layer(s) isolated "
                f"({', '.join(wall_layer_names[:3])}{'...' if len(wall_layer_names) > 3 else ''}); "
                f"wall {net_lf:,.0f} LF × {height_ft:g}' × {face_factor:g} = {wall_sf:,.0f} SF; "
                f"{door_count} doors; scale {scale_used} ({scale_src})"
                f"{'; glaz net -%.0f LF' % glaz_lf if glaz_lf else ''}")
        m = R.Method("layer", "geometry", ok=True, qty=round(wall_sf, 0),
                     scale_locked=scale_locked, self_reconciled=self_rec,
                     floor_sf=None, note=note)
        m.breakdown = {"arch_layers_present": True, "wall_layer_names": wall_layer_names,
                       "glaz_layer_names": glaz_layer_names, "door_layer_names": door_layer_names,
                       "wall_lf": round(net_lf, 1), "wall_lf_gross": round(wall_lf, 1),
                       "glaz_lf": round(glaz_lf, 1), "door_count": door_count,
                       "scale_used": scale_used, "height_ft": height_ft,
                       "face_factor": face_factor}
        return m
    except Exception as e:
        try:
            doc.close()
        except Exception:
            pass
        return R.Method("layer", "geometry", ok=False,
                        note=f"M-LAYER: measurement error ({type(e).__name__}: {e}) — vote withheld")


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    print("=" * 72)
    print("M-LAYER self-test")
    print("=" * 72)

    # (b) import + ncs classification + OCG enumeration sanity (no crash) ------------
    print("\n[b] classification + ocg-enumeration sanity:")
    assert NCS.classify_layer("A-WALL-FULL-N")["role"] == "WALL_ADD"
    assert NCS.classify_layer("A-DOOR")["role"] == "DOOR_COUNT"
    assert NCS.classify_layer("A-GLAZ")["role"] == "GLAZ_SUBTRACT"
    assert NCS.has_arch_layers(["A-WALL-FULL-N", "A-ANNO-DIMS"]) is True
    assert NCS.has_arch_layers(["C-TOPO", "G-ANNO-TTLB"]) is False
    print("    classify_layer + has_arch_layers OK")
    if fitz is not None:
        # synthesize a tiny PDF (no OCGs) and confirm enumeration + withhold don't crash
        d = fitz.open()
        pg = d.new_page(width=300, height=300)
        pg.draw_line(fitz.Point(10, 10), fitz.Point(200, 10))
        tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_mlayer_selftest.pdf")
        d.save(tmp)
        d.close()
        m0 = measure(tmp, scale_drawing=0.1875, height_ft=10.0)
        print(f"    synthetic no-OCG PDF -> ok={m0.ok}  note={m0.note}")
        assert m0.ok is False, "no-OCG synthetic PDF must withhold"
        assert "stripped" in m0.note or "civil" in m0.note
        try:
            os.remove(tmp)
        except Exception:
            pass
    else:
        print("    fitz unavailable -> measure() returns withhold (import-guard) :",
              measure("nope.pdf").note)

    # (a) the REAL USC no-arch-layers withhold path -----------------------------------
    print("\n[a] USC real PDF (layers stripped) withhold path:")
    usc = (r"C:\Agent Carol\data\projects\usc-health-wellness-and-athletics-center"
           r"\bid_docs\BC\extracted\USC - Sumter Health, Wellness _ Athletic Center"
           r"\Plans\Project_Drawings.pdf")
    if os.path.exists(usc):
        m = measure(usc, scale_drawing=0.1875, height_ft=10.0)
        print(f"    ok={m.ok}")
        print(f"    note={m.note}")
        bd = getattr(m, "breakdown", {})
        print(f"    breakdown={bd}")
        assert m.ok is False, "USC must withhold (no arch layers / layers stripped)"
        print("    PASS: USC correctly WITHHELD (no fabricated wall number)")
    else:
        print(f"    (USC PDF not found at expected path; skipping real-file leg)")

    print("\nself-test complete: M-LAYER imports, enumerates OCGs, classifies, and "
          "withholds cleanly on layerless PDFs.")
