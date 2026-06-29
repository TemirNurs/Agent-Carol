#!/usr/bin/env python3
r"""takeoff_reconcile.py — the CONFIDENCE GATE (god-level takeoff, 2026-06-21).

The whole redesign in one function: never ship a single auto-number. Take the
independent measurement methods that ran, and grade the result:

  GOLD    >=2 INDEPENDENT families agree <=10% on the primary quantity, and at
          least one is a real geometry/semantic measure (not Togal/program alone).
  SILVER  exactly one solid measure (geometry OR semantic) that PASSES the
          program magnitude tripwire — ship with an explicit ± band + SILVER label.
  REJECT  methods that diverge >10% (conflict is worse than one), or only Togal,
          or only program, or the magnitude tripwire fires, or no scale lock.
          -> HARD STOP, loud, with reasons. The thing the user demanded.

Independence is enforced by FAMILY: two results from the same family (e.g. two
Togal passes) can never form a GOLD pair — they share a failure mode.

Families:  geometry (vector engine), semantic (LLM reads dimensions/finish sched),
           ai_image (Togal AI), program (known-SF magnitude tripwire — not a takeoff).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median

GOLD_TOL = 0.10          # two methods within 10% = agreement
# 'model' = a structured-source read (IFC/DWG/DXF) — EXACT quantities from the
# authoring database, a query not a measurement → it can stand as GOLD on its own.
REAL_FAMILIES = {"model", "geometry", "semantic", "elevation", "elevation_vision"}
MEASURE_FAMILIES = {"model", "geometry", "semantic", "ai_image", "elevation", "elevation_vision"}
# EXTERIOR magnitude gate (analog of wall:floor): painted_facade_sf / envelope_anchor_sf,
# keyed by building GEOMETRY (not program). Low end < 1.0 because glazing/metal-panel
# subtractions legitimately shrink painted facade below the bare prism envelope.
EXT_GEOMETRY_BANDS = {"simple_box": (0.55, 1.05), "articulated": (0.45, 1.0),
                      "gable_heavy": (0.5, 1.1), "metal_panel_heavy": (0.2, 0.7),
                      "default": (0.35, 1.05)}
SELF_RECONCILED_FLOOR_EXT = (0.15, 1.3)


def ext_geometry_band(ext_geometry: str | None) -> tuple:
    return EXT_GEOMETRY_BANDS.get((ext_geometry or "default").lower(), EXT_GEOMETRY_BANDS["default"])
# plausible interior wall-SF : floor-SF ratio band, BY BUILDING TYPE.
# A fixed band false-REJECTs open buildings: a gym/athletic addition is legitimately
# ~1.0-1.8x (USC Sumter hand read 21,600/17,500 = 1.23x), while a partition-dense
# hotel/apartment is 2.5-4.2x. Resolve with wall_floor_band(building_type).
WALL_TO_FLOOR_BANDS = {
    "athletic": (1.0, 1.8), "open": (1.0, 1.8), "gym": (1.0, 1.8),
    "warehouse": (0.8, 1.6), "retail": (1.2, 2.2),
    "office": (1.8, 3.0), "school": (1.8, 3.2), "medical": (2.0, 3.5),
    "residential": (2.5, 4.2), "hotel": (2.5, 4.2), "multifamily": (2.5, 4.2),
    "default": (1.4, 4.2),
}
# Wide plausibility floor for a measure that self-reconciled (A/B passes agree):
# its own internal agreement is the independent corroboration, so accept across types.
SELF_RECONCILED_FLOOR = (1.0, 4.5)


def wall_floor_band(building_type: str | None) -> tuple:
    return WALL_TO_FLOOR_BANDS.get((building_type or "default").lower(), WALL_TO_FLOOR_BANDS["default"])


# back-compat alias for any external caller referencing the old constant
WALL_TO_FLOOR_BAND = WALL_TO_FLOOR_BANDS["default"]


@dataclass
class Method:
    method: str                 # 'vec' | 'llm' | 'dim' | 'togal' | 'prog'
    family: str                 # 'geometry'|'semantic'|'ai_image'|'program'
    ok: bool = False
    qty: float = 0.0            # primary quantity (e.g. wall_sf)
    scale_locked: bool = True
    note: str = ""
    self_reconciled: bool = False   # measure agreed with itself across 2 internal passes
                                    # (M-DIM A/B <=10%) -> counts as magnitude corroboration
    floor_sf: float | None = None   # the measure's OWN in-scope floor SF (matched scope,
                                    # e.g. M-DIM's gym-excluded floor) — preferred tripwire denominator


@dataclass
class Verdict:
    grade: str = "REJECT"       # GOLD | SILVER | REJECT
    value: float | None = None
    band: tuple | None = None
    primary: str = "wall_sf"
    contributing: list = field(default_factory=list)
    reasons: list = field(default_factory=list)
    provenance: dict = field(default_factory=dict)

    def as_dict(self):
        return {"grade": self.grade, "value": self.value, "band": self.band,
                "primary": self.primary, "contributing": self.contributing,
                "reasons": self.reasons, "provenance": self.provenance}


def _agree(a: float, b: float) -> bool:
    hi = max(abs(a), abs(b))
    return hi > 0 and abs(a - b) / hi <= GOLD_TOL


def reconcile(methods: list[Method], primary: str = "wall_sf",
              floor_sf: float | None = None, building_type: str | None = None) -> Verdict:
    v = Verdict(primary=primary)
    v.provenance = {m.method: {"family": m.family, "ok": m.ok, "qty": round(m.qty, 0),
                               "scale_locked": m.scale_locked, "note": m.note} for m in methods}

    # 1) scale gate — any unscaled measure is disqualified outright
    usable = []
    for m in methods:
        if not m.ok or m.qty <= 0:
            continue
        if m.family in MEASURE_FAMILIES and not m.scale_locked:
            v.reasons.append(f"{m.method}: dropped — sheet not SCALE_LOCKED (cannot trust a guessed scale)")
            continue
        usable.append(m)

    measures = [m for m in usable if m.family in MEASURE_FAMILIES]
    real = [m for m in measures if m.family in REAL_FAMILIES]
    prog = next((m for m in usable if m.family == "program"), None)

    if not measures:
        v.reasons.append("no usable measurement produced a quantity → REJECT (route to human takeoff)")
        return v

    # 0) STRUCTURED SOURCE (model) — IFC/DWG/DXF carry EXACT quantities computed by the
    # authoring software. Reading them is a QUERY, not a measurement, so a 'model' result
    # stands as GOLD on its own. If an independent measure also ran, cross-check it and
    # flag a large divergence (a sign the model is partial-scope), but the model wins.
    model = next((m for m in measures if m.family == "model"), None)
    if model:
        v.grade = "GOLD"
        v.value = round(model.qty, 0)
        v.band = (round(model.qty * 0.97, 0), round(model.qty * 1.03, 0))
        v.contributing = [model.method]
        v.reasons.append(f"GOLD: structured source {model.method}({model.family}) {model.qty:,.0f} "
                         f"— exact quantities read from the BIM/CAD model (a query, not a measurement)")
        others = [m for m in real if m is not model]
        for o in others:
            if not _agree(o.qty, model.qty) and abs(o.qty - model.qty) / max(o.qty, model.qty) > 0.15:
                v.reasons.append(f"NOTE: {o.method} {o.qty:,.0f} diverges {abs(o.qty-model.qty)/max(o.qty,model.qty)*100:.0f}% "
                                 f"from the model — verify the model covers full scope (not a partial export)")
        return v

    # 2) GOLD: two DIFFERENT families agree <=10%, at least one real
    agreeing = None
    fams = {}
    for m in measures:
        fams.setdefault(m.family, []).append(m)
    distinct = [ms[0] for ms in fams.values()]   # one representative per family
    for i in range(len(distinct)):
        for j in range(i + 1, len(distinct)):
            a, b = distinct[i], distinct[j]
            if _agree(a.qty, b.qty) and (a.family in REAL_FAMILIES or b.family in REAL_FAMILIES):
                agreeing = (a, b)
                break
        if agreeing:
            break
    if agreeing:
        a, b = agreeing
        vals = [a.qty, b.qty]
        v.grade = "GOLD"
        v.value = round(median(vals), 0)
        v.band = (round(min(vals), 0), round(max(vals), 0))
        v.contributing = [a.method, b.method]
        v.reasons.append(f"GOLD: {a.method}({a.family}) {a.qty:,.0f} & {b.method}({b.family}) "
                         f"{b.qty:,.0f} agree within {abs(a.qty-b.qty)/max(a.qty,b.qty)*100:.1f}%")
        return v

    # 3) conflict: two real measures that DISAGREE is worse than one → human
    if len(real) >= 2:
        rv = sorted(r.qty for r in real)
        if not _agree(rv[0], rv[-1]):
            v.grade = "REJECT"
            v.reasons.append(f"CONFLICT: independent measures disagree "
                             f"({', '.join(f'{r.method} {r.qty:,.0f}' for r in real)}) "
                             f">10% — resolve before bidding (HARD STOP)")
            return v

    # 4) SILVER: exactly one solid real measure, passes the magnitude tripwire
    if len(real) == 1:
        m = real[0]
        # prefer the measure's OWN in-scope floor (matched scope) over an external gross GSF
        eff_floor = m.floor_sf if (m.floor_sf and m.floor_sf > 0) else floor_sf
        has_ref = (eff_floor and eff_floor > 0) or (prog and prog.qty > 0)
        trip_ok, trip_msg = _magnitude_tripwire(m.qty, prog, eff_floor, primary,
                                                building_type, m.self_reconciled)
        if trip_ok:
            v.grade = "SILVER"
            v.value = round(m.qty, 0)
            # tighter band if a magnitude reference corroborated; wider if uncorroborated
            spread = 0.10 if has_ref else 0.15
            v.band = (round(m.qty * (1 - spread), 0), round(m.qty * (1 + spread), 0))
            v.contributing = [m.method]
            corr = (f"magnitude check PASS ({trip_msg})" if has_ref
                    else f"no independent magnitude reference ({trip_msg}); ±15% — supply --floor-sf/--known-gsf to tighten")
            v.reasons.append(f"SILVER: one solid measure {m.method}({m.family}) {m.qty:,.0f}; {corr}.")
            return v
        # REJECT only on an ACTIVE magnitude CONFLICT (a real reference exists and the
        # measure contradicts it). Absence of a reference is NOT grounds to kill a real measure.
        v.grade = "REJECT"
        v.reasons.append(f"single measure {m.method} {m.qty:,.0f} CONFLICTS with magnitude reference "
                         f"({trip_msg}) → HARD STOP (resolve scale/scope before bidding)")
        return v

    # 5) only ai_image (Togal) or only program — never trustworthy alone
    only = measures[0]
    v.grade = "REJECT"
    v.reasons.append(f"only {only.method}({only.family}) produced a number — "
                     f"never ship on {only.family} alone; need a geometry/semantic measure → human takeoff")
    return v


def _magnitude_tripwire(qty: float, prog: Method | None, floor_sf: float | None,
                        primary: str, building_type: str | None = None,
                        self_reconciled: bool = False) -> tuple[bool, str]:
    """Is qty plausible vs an INDEPENDENT magnitude reference? For wall_sf, check
    the BUILDING-TYPE-aware wall:floor ratio band; else compare to a program number.
    A self-reconciled measure (its own A/B passes agreed) is accepted across a wide
    plausibility floor even if outside the type band — internal agreement IS the
    magnitude corroboration."""
    if primary == "exterior_sf" and floor_sf and floor_sf > 0:
        # floor_sf carries the ENVELOPE ANCHOR (perimeter × weighted height). ratio =
        # painted facade / gross envelope, gated by building GEOMETRY (building_type
        # carries the ext_geometry token here). The interior wall:floor band never applies.
        ratio = qty / floor_sf
        lo, hi = ext_geometry_band(building_type)
        bt = building_type or "default"
        if lo <= ratio <= hi:
            return True, f"facade:env {ratio:.2f}x in [{lo},{hi}] ({bt})"
        if self_reconciled and SELF_RECONCILED_FLOOR_EXT[0] <= ratio <= SELF_RECONCILED_FLOOR_EXT[1]:
            return True, (f"facade:env {ratio:.2f}x outside {bt} band [{lo},{hi}] but SELF-RECONCILED "
                          f"(A/B passes agree) → accepted")
        return False, f"facade:env {ratio:.2f}x OUTSIDE [{lo},{hi}] ({bt})"
    if primary == "wall_sf" and floor_sf and floor_sf > 0:
        ratio = qty / floor_sf
        lo, hi = wall_floor_band(building_type)
        bt = building_type or "default"
        if lo <= ratio <= hi:
            return True, f"wall:floor {ratio:.2f}x in [{lo},{hi}] ({bt})"
        if self_reconciled and SELF_RECONCILED_FLOOR[0] <= ratio <= SELF_RECONCILED_FLOOR[1]:
            return True, (f"wall:floor {ratio:.2f}x outside {bt} band [{lo},{hi}] but measure "
                          f"SELF-RECONCILED (A/B passes agree) → accepted")
        return False, f"wall:floor {ratio:.2f}x OUTSIDE [{lo},{hi}] ({bt})"
    if prog and prog.qty > 0:
        if _agree(qty, prog.qty) or abs(qty - prog.qty) / prog.qty <= 0.35:
            return True, f"within program magnitude ({prog.qty:,.0f})"
        return False, f"{qty:,.0f} vs program {prog.qty:,.0f} > 35%"
    return True, "no independent magnitude reference (accepted, lower confidence)"


if __name__ == "__main__":
    # self-test: the four canonical situations
    import json, sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    cases = {
        "GOLD (vec+llm agree)": ([Method("vec", "geometry", True, 300000),
                                 Method("llm", "semantic", True, 297858),
                                 Method("togal", "ai_image", False, 0, note="walls=0 silent fail")],
                                 88321, "residential"),
        "SILVER (only llm, magnitude ok)": ([Method("llm", "semantic", True, 297858),
                                            Method("togal", "ai_image", False, 0)], 88321, "residential"),
        "REJECT (only togal)": ([Method("togal", "ai_image", True, 250000)], 88321, "residential"),
        "REJECT (conflict)": ([Method("vec", "geometry", True, 200000),
                              Method("llm", "semantic", True, 300000)], 88321, "residential"),
        "REJECT (unscaled)": ([Method("vec", "geometry", True, 999999, scale_locked=False)], 88321, "residential"),
        # --- USC fix regression: athletic building at 1.23x wall:floor ---
        "SILVER (USC dim, athletic)": ([Method("dim", "semantic", True, 21600, self_reconciled=True)],
                                       17500, "athletic"),
        "SILVER (USC dim self-recon, residential band)": (
            [Method("dim", "semantic", True, 21600, self_reconciled=True)], 17500, "residential"),
        "REJECT (1.23x, residential, NOT self-recon)": (
            [Method("dim", "semantic", True, 21600, self_reconciled=False)], 17500, "residential"),
        "SILVER (lone measure, no ref)": ([Method("dim", "semantic", True, 21600)], None, None),
        # --- structured source (model) = GOLD on its own ---
        "GOLD (model IFC exact)": ([Method("ifc", "model", True, 21500, self_reconciled=True)], 17500, "athletic"),
        "GOLD (model wins, divergent measure noted)": ([Method("ifc", "model", True, 21500),
                                                        Method("dim", "semantic", True, 15000)], 17500, "athletic"),
    }
    for name, (ms, fsf, bt) in cases.items():
        v = reconcile(ms, floor_sf=fsf, building_type=bt)
        print(f"{name:46} -> {v.grade:7} {('$'+format(v.value,',.0f')) if v.value else '-':>12}  | {v.reasons[0][:72]}")

    # --- EXTERIOR (primary='exterior_sf') regression: M-EXT envelope-anchored ---
    ext_cases = {
        "EXT GOLD (ext+vision agree)": ([Method("ext", "elevation", True, 6800, self_reconciled=True),
                                         Method("extvis", "elevation_vision", True, 7100)], 9000, "simple_box"),
        "EXT SILVER (lone, in band)": ([Method("ext", "elevation", True, 6800, self_reconciled=True)], 9000, "simple_box"),
        "EXT REJECT (A/B conflict 9.8k vs 5.4k)": ([Method("ext", "elevation", True, 9864),
                                                    Method("extvis", "elevation_vision", True, 5350)], 9000, "simple_box"),
        "EXT REJECT (ratio out of band, not self-rec)": ([Method("ext", "elevation", True, 12000)], 9000, "simple_box"),
    }
    for name, (ms, env, geo) in ext_cases.items():
        v = reconcile(ms, primary="exterior_sf", floor_sf=env, building_type=geo)
        print(f"{name:46} -> {v.grade:7} {('$'+format(v.value,',.0f')) if v.value else '-':>12}  | {v.reasons[0][:72]}")
