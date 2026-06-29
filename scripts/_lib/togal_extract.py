#!/usr/bin/env python3
"""togal_extract.py — CORRECT extraction of Togal view geojson (2026-06-25).

Replaces the broken summing in togal_pipeline.extract_measurements(), which
lumped every Polygon together (footprint + region + room + room_gia →
quadruple-count) and converted via a GUESSED DPI → garbage (757k SF on a
24k building). See memory: reference_togal_quantity_retrieval.

GROUND TRUTH (verified on USC Sumter set 6/25):
  Each geojson feature properties carries:
    area, perimeter, doors, ml_classification = [label, confidence],
    types = a list, the REAL discriminator:
      'footprint'      -> 1 per sheet = page/site boundary  (ALWAYS EXCLUDE)
      'room_gia'       -> gross internal area per room       (USE for floor SF)
      'room','region'  -> overlap/duplicate room_gia         (do NOT also sum)
      'wall_perimeter' -> wall loops, but in an inconsistent (~10x finer)
                          unit -> NOT reliable raw; needs classification+scale
      ['ML','Single Swing Door'] / ['ML','Double Swing Door'] -> door counts
      ['ML','Toilet'] / ['ML','Sink'] -> fixture counts

UNITS: raw area is in unscaled drawing units. Do NOT trust a DPI guess.
Anchor to a KNOWN quantity: area_factor = known_gsf / sum(room_gia raw area).
Then area_sf = raw_area * area_factor. (On USC this reproduced 24,340 GSF and
56 doors exactly.) If no known_gsf is supplied we still return raw + counts and
flag that the area is uncalibrated.

WALLS: do not derive wall SF from raw geojson here — it is unreliable. Either
auto-classify walls in Togal (so the classification returns scaled SF, per
reference_togal_quantity_retrieval) or take wall SF from the dimension-string
takeoff. This module's reliable outputs for the godmode reconcile are FLOOR
AREA and COUNTS (a cross-check), not wall SF.
"""
from __future__ import annotations
from collections import defaultdict


def _has_type(props, t):
    ty = props.get("types") or []
    return t in ty if isinstance(ty, list) else False


def _ml_label(props):
    ty = props.get("types") or []
    if isinstance(ty, list) and len(ty) > 1 and ty[0] == "ML":
        return ty[1]
    return None


def extract_by_types(features: list, known_gsf: float | None = None) -> dict:
    """Correct, types-aware extraction. Returns floor area (calibrated if
    known_gsf given), counts, and the raw aggregates + calibration factor."""
    raw = {"room_gia": 0.0, "room": 0.0, "region": 0.0, "footprint": 0.0,
           "wall_perimeter": 0.0}
    n = defaultdict(int)
    counts = defaultdict(int)            # ML door/fixture counts
    by_class = defaultdict(lambda: [0, 0.0])  # ml_classification label -> (n, raw_area)

    for f in features or []:
        p = f.get("properties", {}) or {}
        a = float(p.get("area") or 0)
        pm = float(p.get("perimeter") or 0)
        for k in raw:
            if _has_type(p, k):
                raw[k] += a if k != "wall_perimeter" else 0
                if k == "wall_perimeter":
                    raw[k] += pm  # accumulate perimeter for this type
                n[k] += 1
        ml = _ml_label(p)
        if ml:
            counts[ml] += 1
        mlc = p.get("ml_classification")
        if isinstance(mlc, list) and mlc and isinstance(mlc[0], str):
            by_class[mlc[0]][0] += 1
            by_class[mlc[0]][1] += a

    # calibrate area to a known GSF using room_gia (the gross-internal-area set)
    area_factor = None
    if known_gsf and raw["room_gia"] > 0:
        area_factor = known_gsf / raw["room_gia"]

    def sf(raw_area):
        return round(raw_area * area_factor, 0) if area_factor else None

    doors_single = counts.get("Single Swing Door", 0)
    doors_double = counts.get("Double Swing Door", 0)

    return {
        "calibrated": area_factor is not None,
        "area_factor": area_factor,
        "floor_sf": sf(raw["room_gia"]),            # reliable (calibrated)
        "footprint_sf": sf(raw["footprint"]),       # building+walls / boundary
        "room_count": n["room_gia"],
        "doors": {"single": doors_single, "double": doors_double,
                  "total": doors_single + doors_double},
        "fixtures": {k: counts[k] for k in ("Toilet", "Sink", "Urinal") if k in counts},
        "by_ml_classification": {k: {"n": v[0], "raw_area": round(v[1], 0),
                                     "area_sf": sf(v[1])} for k, v in by_class.items()},
        "wall_perimeter_raw": round(raw["wall_perimeter"], 0),
        "wall_sf": None,   # intentionally not derived — see module docstring
        "raw_aggregates": raw,
        "feature_counts": dict(n),
        "notes": ("Floor area + counts are reliable. Wall SF NOT derived from raw "
                  "geojson (unreliable unit) — use dimension takeoff or auto-classified "
                  "wall classification. footprint excluded from floor_sf."
                  + ("" if area_factor else " AREA UNCALIBRATED — pass known_gsf.")),
    }
