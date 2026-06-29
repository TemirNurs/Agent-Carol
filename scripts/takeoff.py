#!/usr/bin/env python3
r"""takeoff.py — the ONE god-level takeoff entrypoint (2026-06-21).

Replaces the whack-a-mole graveyard (_wssu_*, _wedd_m1_refire, _togal_m1_refire,
_clemson_*, togal_run_takeoff, togal_takeoff_proper ...). One spine:

  LAYER 1  calibrate scale per floor-plan sheet (two independent anchors)   [keystone]
  LAYER 2  run every INDEPENDENT measure that applies:
             M-VEC   local vector engine (geometry)      — needs scale-locked vector sheet
             M-LLM   dimensioned-plan / per-unit read     — pass via --llm-wall-sf or a json
             M-TOGAL Togal AI, sanity-gated (ai_image)    — optional corroborating vote only
             M-PROG  program floor-SF magnitude tripwire  — not a takeoff, a guardrail
  LAYER 3  reconcile -> GOLD / SILVER / REJECT (loud, with reasons + provenance)

Truth = independent methods agreeing. Togal is one vote, never the verdict — so
when Togal is down/noisy (its normal state) the takeoff still produces a
defensible, honestly-graded number or HARD-STOPS. No single tool can sink it.

  python scripts/takeoff.py <pdf> [--pages 25,26,29] [--floor-sf 35588] \
         [--llm-wall-sf 297858] [--togal-json path] [--scale 0.125]
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from _lib import takeoff_calibrate as CAL
from _lib import takeoff_reconcile as R
from _lib import togal_sanity as TS


def run(pdf, pages, floor_sf=None, llm_wall_sf=None, togal_json=None, scale_override=None,
        height_ft=9.0, units=None, known_gsf=None, dim_pages=None, building_type=None,
        elevation_pages=None, ext_json=None, perimeter_lf=None, datums=None,
        ext_geometry=None, ext_mean_height=None, ext_vision=False,
        ifc_path=None, dxf_path=None):
    report = {"pdf": Path(pdf).name, "layers": {}}
    methods = []

    # ---- MODEL-ONLY short-circuit: when the source is an IFC/DXF (not a PDF), skip the
    # entire PDF pipeline (intake/calibrate/M-VEC/M-DIM/M-EXT) and read the structured
    # source directly — that's the exact, GOLD path. ----
    is_pdf = str(pdf).lower().endswith(".pdf") and os.path.exists(str(pdf))
    if not is_pdf and (ifc_path or dxf_path):
        if ifc_path:
            try:
                from _lib import ifc_takeoff as IFC
                methods.append(IFC.measure(ifc_path, height_ft=height_ft))
            except Exception as e:
                methods.append(R.Method("ifc", "model", ok=False, note=f"M-IFC error: {str(e)[:80]}"))
        if dxf_path:
            try:
                from _lib import dxf_takeoff as DXF
                methods.append(DXF.measure(dxf_path, height_ft=height_ft))
            except Exception as e:
                methods.append(R.Method("dxf", "model", ok=False, note=f"M-DXF error: {str(e)[:80]}"))
        report["layers"]["0_provenance"] = {"tier": "T0_IFC" if ifc_path else "T0_CAD",
                                            "path": os.path.basename(ifc_path or dxf_path)}
        report["layers"]["2_measures"] = [{"method": m.method, "family": m.family, "ok": m.ok,
                                           "qty": round(m.qty, 0), "note": m.note} for m in methods]
        v = R.reconcile(methods, primary="wall_sf", floor_sf=known_gsf, building_type=building_type)
        report["verdict"] = v.as_dict()
        return report

    # ---- LAYER 0a: PROVENANCE TRIAGE — classify input fidelity BEFORE measuring.
    # A construction drawing is born as a Revit/IFC/DWG database (exact quantities);
    # what we receive is a lossy export. Knowing the tier stops the engine from
    # promoting a flattened/rasterized source to GOLD (the $40K↔$34K rollercoaster).
    prov = None
    try:
        from _lib import pdf_tier_triage as TRI
        prov = TRI.triage(pdf)
        report["layers"]["0_provenance"] = prov
    except Exception as e:
        report["layers"]["0_provenance"] = {"error": str(e)[:120]}

    # ---- LAYER 0: intake/triage — auto-select floor plans, flag raster ----
    from _lib import takeoff_intake as IN
    any_raster = False
    try:
        tri = IN.triage(pdf)
        any_raster = tri.get("any_raster", False)
        report["layers"]["0_intake"] = {"floorplan_pages": tri.get("floorplan_pages"),
                                         "any_raster": any_raster, "summary": tri.get("summary")}
        if not pages:
            pages = tri.get("floorplan_pages") or [0]
    except Exception as e:
        report["layers"]["0_intake"] = {"error": str(e)[:120]}
        if not pages:
            pages = [0]

    # ---- LAYER 1: calibrate scale (best lock across all floor-plan pages) ----
    locked_page = None
    if scale_override:
        locked_scale, any_locked, cal_reasons = scale_override, True, ["scale override (manual)"]
        locked_page = pages[0] if pages else 0
    else:
        cal = CAL.calibrate_set(pdf, pages)
        locked_scale = cal.inches_per_foot if cal.locked else None
        any_locked = cal.locked
        locked_page = cal.page_index if cal.locked else None
        cal_reasons = cal.reasons
    report["layers"]["1_calibrate"] = {"scale_locked": any_locked, "inches_per_foot": locked_scale,
                                       "locked_page": locked_page, "reasons": cal_reasons}

    # ---- LAYER 2: independent measures ----
    # M-VEC (geometry) — only if we have a locked scale
    if any_locked:
        try:
            from _lib import vector_takeoff as V
            vec_pages = [locked_page] if locked_page is not None else pages
            vr = V.measure_pdf(pdf, locked_scale, pages=vec_pages, height_ft=height_ft)
            tot = vr.get("totals", vr) if isinstance(vr, dict) else {}
            wsf = tot.get("wall_sf") or 0
            reliable = tot.get("reliable", False)
            fp = tot.get("footprint_sf") or 0
            # extra cross-check: if a known floor magnitude exists, the geometry
            # footprint must be in the right ballpark or the read is whole-sheet/noisy
            ref = known_gsf or floor_sf
            if reliable and ref and fp and not (0.4 <= fp / ref <= 1.8):
                reliable = False
            if wsf and reliable:
                methods.append(R.Method("vec", "geometry", ok=True, qty=wsf, scale_locked=True,
                                        note=f"vector engine @ {locked_scale}: {tot.get('wall_cl_lf',0):,.0f} LF "
                                             f"centerline ×{height_ft}ft×2, footprint {fp:,.0f} SF"))
            else:
                methods.append(R.Method("vec", "geometry", ok=False, scale_locked=True,
                                        note=f"vector engine measured {wsf:,.0f} SF but WITHHELD — polygonize "
                                             f"unreliable (footprint {fp:,.0f} SF / rooms {tot.get('rooms_n',0)} "
                                             f"@ {tot.get('room_sf',0):,.0f} SF; noisy or whole-sheet read)"))
        except Exception as e:
            methods.append(R.Method("vec", "geometry", ok=False, note=f"vector engine error: {str(e)[:80]}"))
    else:
        methods.append(R.Method("vec", "geometry", ok=False, scale_locked=False,
                                note="no SCALE_LOCKED sheet — vector engine withheld"))

    # M-LLM (semantic) — explicit per-unit measure if supplied, else auto headless read
    if llm_wall_sf:
        methods.append(R.Method("llm", "semantic", ok=True, qty=float(llm_wall_sf),
                                scale_locked=True, note="LLM per-unit/dimensioned-plan measured read (supplied)"))
    else:
        try:
            from _lib import llm_takeoff as LT
            methods.append(LT.measure(pdf, pages, height_ft=height_ft,
                                      scale_locked=any_locked, units=units))
        except Exception as e:
            methods.append(R.Method("llm", "semantic", ok=False, note=f"llm_takeoff error: {str(e)[:80]}"))

    # M-DIM (semantic, dimension-string read) — the measure that works on vector-CAD
    # sets where M-VEC/Togal fail. Scale-INDEPENDENT (printed dims), self-reconciles
    # via two internal passes, carries its own in-scope floor. Reads a _dim_rooms.json
    # room list (from the dimension-plan read) sibling to the PDF.
    try:
        from _lib import takeoff_dim as DIM
        methods.append(DIM.measure(pdf, dim_pages or pages, height_ft=height_ft,
                                   known_gsf=known_gsf, building_type=building_type))
    except Exception as e:
        methods.append(R.Method("dim", "semantic", ok=False, note=f"M-DIM error: {str(e)[:80]}"))

    # M-LAYER (geometry) — isolate A-WALL/A-DOOR OCG layers on a LAYERED vector PDF
    # (T1). Withholds cleanly when layers are stripped (T2/T3). Pairs with M-DIM → GOLD.
    try:
        from _lib import layer_takeoff as LAY
        methods.append(LAY.measure(pdf, pages=dim_pages or pages, height_ft=height_ft))
    except Exception as e:
        methods.append(R.Method("layer", "geometry", ok=False, note=f"M-LAYER error: {str(e)[:80]}"))

    # M-IFC / M-DXF (model) — STRUCTURED SOURCE, exact quantities. When a model/CAD file
    # is supplied these dominate (GOLD). The PDF measures above stay as cross-checks.
    if ifc_path:
        try:
            from _lib import ifc_takeoff as IFC
            methods.append(IFC.measure(ifc_path, height_ft=height_ft))
        except Exception as e:
            methods.append(R.Method("ifc", "model", ok=False, note=f"M-IFC error: {str(e)[:80]}"))
    if dxf_path:
        try:
            from _lib import dxf_takeoff as DXF
            methods.append(DXF.measure(dxf_path, height_ft=height_ft))
        except Exception as e:
            methods.append(R.Method("dxf", "model", ok=False, note=f"M-DXF error: {str(e)[:80]}"))

    # M-TOGAL (ai_image) — sanity-gated optional vote
    tj = togal_json or (ROOT / "data/projects" / Path(pdf).parent.parent.name / "togal_takeoff.json")
    methods.append(TS.togal_method(tj, plausible_floor_sf=floor_sf))

    # M-PROG (program magnitude tripwire)
    if floor_sf:
        methods.append(R.Method("prog", "program", ok=True, qty=float(floor_sf),
                                note="program floor-SF magnitude reference"))

    report["layers"]["2_measures"] = [
        {"method": m.method, "family": m.family, "ok": m.ok, "qty": round(m.qty, 0), "note": m.note}
        for m in methods]

    # ---- LAYER 3: reconcile (INTERIOR wall_sf) ----
    v = R.reconcile(methods, primary="wall_sf", floor_sf=floor_sf, building_type=building_type)
    # critique hardening: a raster/scanned set can NEVER be GOLD (no trustworthy
    # geometry measure exists; LLM+Togal on a scan share an image-read failure mode).
    if any_raster and v.grade == "GOLD":
        v.grade = "SILVER"
        v.reasons.insert(0, "RASTER set — GOLD structurally disallowed (no vector geometry to corroborate); capped to SILVER")
    # PROVENANCE CAP: a degraded input can never ship as GOLD. T2_FLATTENED / T3_RASTER
    # cap at SILVER; T0/T1 allow GOLD. This makes the confidence gate input-quality-aware.
    cap = (prov or {}).get("confidence_cap")
    if cap == "SILVER" and v.grade == "GOLD":
        v.grade = "SILVER"
        v.reasons.insert(0, f"capped GOLD→SILVER: input is {prov.get('tier')} "
                            f"({prov.get('producer','?')}) — degraded export, not a structured source. "
                            f"{prov.get('recommendation','')[:90]}")
    report["verdict"] = v.as_dict()

    # ---- EXTERIOR (opt-in): only runs when exterior inputs are supplied; interior
    # output above is byte-identical to before when no exterior flags are passed. ----
    if elevation_pages or ext_json or perimeter_lf:
        try:
            from _lib import takeoff_ext as EXT
            ext_methods = [EXT.measure(pdf, elevation_pages, datums=datums, perimeter_lf=perimeter_lf,
                                       ext_json=ext_json, building_type=ext_geometry,
                                       ext_geometry=ext_geometry, mean_height_ft=ext_mean_height)]
            if ext_vision:
                ext_methods.append(EXT.measure_vision(pdf, ext_json=ext_json))
            env_sf = ext_methods[0].floor_sf
            ve = R.reconcile(ext_methods, primary="exterior_sf", floor_sf=env_sf, building_type=ext_geometry)
            if cap == "SILVER" and ve.grade == "GOLD":
                ve.grade = "SILVER"
                ve.reasons.insert(0, f"capped GOLD→SILVER: {prov.get('tier')} degraded input")
            rd = getattr(ext_methods[0], "breakdown", None)
            report["verdict_exterior"] = {**ve.as_dict(), "breakdown": rd}
        except Exception as e:
            report["verdict_exterior"] = {"grade": "ERROR", "reasons": [f"M-EXT error: {str(e)[:120]}"]}
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--pages", default="", help="0-based floor-plan page indices, comma-sep")
    ap.add_argument("--floor-sf", type=float, default=None)
    ap.add_argument("--llm-wall-sf", type=float, default=None)
    ap.add_argument("--togal-json", default=None)
    ap.add_argument("--scale", type=float, default=None, help="override inches-per-foot")
    ap.add_argument("--height", type=float, default=9.0, help="ceiling height ft")
    ap.add_argument("--units", type=int, default=None, help="# dwelling units (multifamily)")
    ap.add_argument("--known-gsf", type=float, default=None, dest="known_gsf",
                    help="known building gross SF (calibrates Togal area + magnitude ref)")
    ap.add_argument("--dim-pages", default="", dest="dim_pages",
                    help="0-based dimension-plan page indices for M-DIM (comma-sep)")
    ap.add_argument("--building-type", default=None, dest="building_type",
                    help="athletic|open|warehouse|retail|office|school|medical|residential|hotel "
                         "(sets the wall:floor tripwire band)")
    # exterior (M-EXT) — all opt-in; absent => interior-only output unchanged
    ap.add_argument("--elevation-pages", default="", dest="elevation_pages",
                    help="0-based elevation sheet page indices for M-EXT (comma-sep)")
    ap.add_argument("--ext-json", default=None, dest="ext_json", help="path to _ext_faces.json")
    ap.add_argument("--perimeter-lf", type=float, default=None, dest="perimeter_lf",
                    help="building exterior perimeter LF (envelope anchor)")
    ap.add_argument("--ext-geometry", default=None, dest="ext_geometry",
                    help="simple_box|articulated|gable_heavy|metal_panel_heavy (facade:env band)")
    ap.add_argument("--ext-mean-height", type=float, default=None, dest="ext_mean_height")
    ap.add_argument("--ext-vision", action="store_true", dest="ext_vision")
    ap.add_argument("--ifc", default=None, dest="ifc_path", help="IFC/BIM model file (exact quantities, GOLD)")
    ap.add_argument("--dxf", default=None, dest="dxf_path", help="DXF CAD file (exact geometry on layers, GOLD)")
    a = ap.parse_args()
    pages = [int(x) for x in a.pages.split(",") if x.strip()]   # empty -> intake auto-selects
    dim_pages = [int(x) for x in a.dim_pages.split(",") if x.strip()] or None
    elev_pages = [int(x) for x in a.elevation_pages.split(",") if x.strip()] or None
    rep = run(a.pdf, pages, a.floor_sf, a.llm_wall_sf, a.togal_json, a.scale,
              height_ft=a.height, units=a.units, known_gsf=a.known_gsf,
              dim_pages=dim_pages, building_type=a.building_type,
              elevation_pages=elev_pages, ext_json=a.ext_json, perimeter_lf=a.perimeter_lf,
              ext_geometry=a.ext_geometry, ext_mean_height=a.ext_mean_height, ext_vision=a.ext_vision,
              ifc_path=a.ifc_path, dxf_path=a.dxf_path)
    v = rep["verdict"]
    print(json.dumps(rep, indent=2, default=str))
    print("\n" + "=" * 70)
    print(f"  TAKEOFF VERDICT: {v['grade']}   wall_sf = "
          f"{('%s' % format(v['value'], ',.0f')) if v['value'] else 'NONE'}"
          f"{('  band ' + str(v['band'])) if v.get('band') else ''}")
    for r in v["reasons"]:
        print("   - " + r)
    print("=" * 70)
    ve = rep.get("verdict_exterior")
    if ve:
        print(f"  EXTERIOR VERDICT: {ve.get('grade')}   exterior_sf = "
              f"{('%s' % format(ve['value'], ',.0f')) if ve.get('value') else 'NONE'}"
              f"{('  band ' + str(ve['band'])) if ve.get('band') else ''}")
        for r in ve.get("reasons", []):
            print("   - " + r)
        print("=" * 70)


if __name__ == "__main__":
    main()
