#!/usr/bin/env python3
r"""ifc_takeoff.py — M-IFC: the GOLD structured-source reader (god-level takeoff).

When the project ships a BIM model (IFC), the painted quantities are not measured
off a drawing — they are READ straight out of the authoring tool's geometry, with
openings already deducted by the modeler. That makes IFC the single most reliable
source we have, so M-IFC is a first-class measure in the reconciler.

WHAT WE READ (and why these property/quantity sets):
  IfcWall      -> Qto_WallBaseQuantities.NetSideArea — the PAINTABLE face area, with
                  door/window openings already subtracted by the exporter. We sum
                  NetSideArea (NOT GrossSideArea, which double-counts the opening).
                  This sum is the primary quantity = painted wall SF.
  IfcCovering  -> Qto_CoveringBaseQuantities.NetArea — the actual finish/paint
                  coverage when the model carries finishes as coverings. Preferred
                  finish SF when present (reported in the breakdown).
  IfcSpace     -> Qto_SpaceBaseQuantities.NetFloorArea — summed = floor_sf (the
                  in-scope floor, used as the reconciler's magnitude denominator).
  IfcDoor      -> exact instance COUNT (len of by_type) — never factored.

EXPORTER-BUG GUARDS (Revit/ArchiCAD IFC exporters are notoriously buggy):
  - NetArea == GrossArea on walls or spaces means the exporter duplicated the gross
    into the net (it never computed a real net) — openings were NOT deducted. We
    still return the number (it is the best available) but drop self_reconciled and
    flag lower confidence in the note.
  - A missing quantity set (no Qto_* at all) likewise lowers confidence.
  - NO walls / no wall quantities at all -> ok=False (nothing measurable).

INTERFACE: measure(ifc_path, scope='new', height_ft=None) -> R.Method('ifc','model',...)
family='model'; scale_locked is always True (a model carries true dimensions, not a
plotted scale). self_reconciled=True only when wall NetSideArea is present AND
distinct from GrossSideArea (a genuine opening-deducted net) — that internal
consistency is the corroboration the reconcile tripwire reads.
"""
from __future__ import annotations

try:
    from . import takeoff_reconcile as R
except ImportError:
    import takeoff_reconcile as R

# IMPORT-GUARD the heavy lib: never crash the takeoff if ifcopenshell is absent.
try:
    import ifcopenshell
    import ifcopenshell.util.element as _ifc_el
    _IFC_OK = True
except Exception:  # pragma: no cover - exercised only on a box without ifcopenshell
    ifcopenshell = None
    _ifc_el = None
    _IFC_OK = False


def _qset(element, only_names=None):
    """Return the quantity sets (Qto_*) for an element as {set: {prop: value}}.
    Robust across ifcopenshell versions: falls back to an empty dict on any error."""
    try:
        psets = _ifc_el.get_psets(element, qtos_only=True)
    except TypeError:
        # older signature without qtos_only
        try:
            psets = _ifc_el.get_psets(element)
        except Exception:
            return {}
    except Exception:
        return {}
    if not isinstance(psets, dict):
        return {}
    if only_names:
        return {k: v for k, v in psets.items() if k in only_names}
    return psets


def _num(v):
    try:
        f = float(v)
        return f if f == f else None  # drop NaN
    except (TypeError, ValueError):
        return None


def _wall_area(element):
    """(net_side_area, gross_side_area, has_qset) for one IfcWall.
    Prefers Qto_WallBaseQuantities; tolerates any Qto_*Wall* set name."""
    qs = _qset(element)
    if not qs:
        return None, None, False
    net = gross = None
    # exact base set first, then any wall-ish quantity set
    ordered = ([("Qto_WallBaseQuantities", qs["Qto_WallBaseQuantities"])]
               if "Qto_WallBaseQuantities" in qs else []) + \
              [(k, v) for k, v in qs.items() if k != "Qto_WallBaseQuantities"]
    for _name, props in ordered:
        if not isinstance(props, dict):
            continue
        if net is None:
            net = _num(props.get("NetSideArea"))
        if gross is None:
            gross = _num(props.get("GrossSideArea"))
    return net, gross, True


def _space_floor(element):
    """(net_floor, gross_floor) for an IfcSpace from Qto_SpaceBaseQuantities."""
    qs = _qset(element)
    net = gross = None
    for props in qs.values():
        if not isinstance(props, dict):
            continue
        if net is None:
            net = _num(props.get("NetFloorArea"))
        if gross is None:
            gross = _num(props.get("GrossFloorArea"))
    return net, gross


def _covering_area(element):
    """NetArea for an IfcCovering from Qto_CoveringBaseQuantities."""
    qs = _qset(element)
    for props in qs.values():
        if not isinstance(props, dict):
            continue
        a = _num(props.get("NetArea"))
        if a is not None:
            return a
    return None


def measure(ifc_path, scope: str = "new", height_ft: float | None = None) -> R.Method:
    if not _IFC_OK:
        return R.Method("ifc", "model", ok=False, scale_locked=True,
                        note="M-IFC: ifcopenshell not installed — vote withheld")
    try:
        model = ifcopenshell.open(ifc_path)
    except Exception as ex:
        return R.Method("ifc", "model", ok=False, scale_locked=True,
                        note=f"M-IFC: cannot open '{ifc_path}': {ex}")

    walls = model.by_type("IfcWall")
    if not walls:
        return R.Method("ifc", "model", ok=False, scale_locked=True,
                        note="M-IFC: model has no IfcWall — nothing measurable")

    # ---- WALLS: sum NetSideArea (paintable, openings deducted) ----
    wall_net_sum = 0.0
    wall_gross_sum = 0.0
    walls_with_qty = 0
    walls_net_eq_gross = 0       # exporter-bug signature: net duplicated from gross
    walls_missing_qset = 0
    for w in walls:
        net, gross, has_q = _wall_area(w)
        if not has_q:
            walls_missing_qset += 1
            continue
        if net is None and gross is None:
            walls_missing_qset += 1
            continue
        # if only gross is present, fall back to gross (best available, flagged)
        use = net if net is not None else gross
        if use is None:
            continue
        wall_net_sum += use
        walls_with_qty += 1
        if gross is not None:
            wall_gross_sum += gross
            if net is not None and abs(net - gross) < 1e-6:
                walls_net_eq_gross += 1

    if walls_with_qty == 0 or wall_net_sum <= 0:
        return R.Method("ifc", "model", ok=False, scale_locked=True,
                        note=f"M-IFC: {len(walls)} IfcWall but NO usable wall quantities "
                             f"(missing Qto_WallBaseQuantities / NetSideArea) — vote withheld")

    # ---- COVERINGS: preferred finish coverage when present ----
    coverings = model.by_type("IfcCovering")
    covering_net_sum = 0.0
    coverings_with_qty = 0
    for c in coverings:
        a = _covering_area(c)
        if a is not None and a > 0:
            covering_net_sum += a
            coverings_with_qty += 1

    # ---- SPACES: floor SF ----
    spaces = model.by_type("IfcSpace")
    floor_net_sum = 0.0
    floor_gross_sum = 0.0
    spaces_with_qty = 0
    spaces_net_eq_gross = 0
    for s in spaces:
        net, gross = _space_floor(s)
        use = net if net is not None else gross
        if use is not None and use > 0:
            floor_net_sum += use
            spaces_with_qty += 1
            if gross is not None:
                floor_gross_sum += gross
                if net is not None and abs(net - gross) < 1e-6:
                    spaces_net_eq_gross += 1
    floor_sf = round(floor_net_sum, 0) if floor_net_sum > 0 else None

    # ---- DOORS: exact count ----
    door_count = len(model.by_type("IfcDoor"))

    # ---- per-space wall split via IfcRelSpaceBoundary (if the model carries it) ----
    per_space = {}
    try:
        for rel in model.by_type("IfcRelSpaceBoundary"):
            sp = getattr(rel, "RelatingSpace", None)
            be = getattr(rel, "RelatedBuildingElement", None)
            if sp is None or be is None or not be.is_a("IfcWall"):
                continue
            net, gross, has_q = _wall_area(be)
            use = net if net is not None else gross
            if use is None:
                continue
            key = getattr(sp, "LongName", None) or getattr(sp, "Name", None) or sp.GlobalId
            per_space[key] = round(per_space.get(key, 0.0) + use, 1)
    except Exception:
        per_space = {}

    qty = round(wall_net_sum, 0)

    # ---- confidence: self_reconciled iff a genuine opening-deducted net exists ----
    net_is_real = (wall_net_sum > 0 and walls_net_eq_gross == 0 and walls_missing_qset == 0)
    self_rec = bool(net_is_real)

    flags = []
    if walls_missing_qset:
        flags.append(f"{walls_missing_qset}/{len(walls)} walls missing quantities")
    if walls_net_eq_gross:
        flags.append(f"{walls_net_eq_gross} walls NetSideArea==GrossSideArea "
                     f"(exporter did not deduct openings)")
    if spaces and spaces_net_eq_gross:
        flags.append(f"{spaces_net_eq_gross} spaces NetFloorArea==GrossFloorArea")
    if not spaces:
        flags.append("no IfcSpace (no floor anchor)")

    breakdown = {
        "wall_net_side_area_sf": qty,
        "wall_gross_side_area_sf": round(wall_gross_sum, 0) or None,
        "wall_count": len(walls),
        "walls_with_quantities": walls_with_qty,
        "walls_missing_qset": walls_missing_qset,
        "walls_net_eq_gross": walls_net_eq_gross,
        "covering_net_area_sf": round(covering_net_sum, 0) if coverings_with_qty else None,
        "coverings_with_quantities": coverings_with_qty,
        "floor_sf": floor_sf,
        "space_count": len(spaces),
        "door_count": door_count,
        "per_space_wall_sf": per_space or None,
        "exporter_flags": flags,
    }

    fin = (f", covering finish={covering_net_sum:,.0f} SF" if coverings_with_qty else "")
    flr = (f", floor={floor_sf:,.0f} SF wall:floor={qty/floor_sf:.2f}x"
           if floor_sf else ", no floor anchor")
    conf = "opening-deducted net (self-reconciled)" if self_rec else \
           "LOWER CONFIDENCE — " + ("; ".join(flags) if flags else "net unverified")
    note = (f"M-IFC: {walls_with_qty} walls NetSideArea={qty:,.0f} SF{fin}{flr}, "
            f"{door_count} doors; {conf}")

    m = R.Method("ifc", "model", ok=True, qty=qty, scale_locked=True,
                 self_reconciled=self_rec, floor_sf=floor_sf, note=note)
    m.breakdown = breakdown
    return m


# --------------------------------------------------------------------------- #
# self-test: synthesize a minimal IFC in memory with one IfcWall carrying a
# Qto_WallBaseQuantities.NetSideArea, write it to a temp file, read it back, and
# confirm measure() recovers the NetSideArea. Also exercise the empty-model and
# import-guard paths. Deterministic, builds its own fixture, no external file.
# --------------------------------------------------------------------------- #
def _build_fixture_ifc(path, net_side_area=420.0, gross_side_area=480.0):
    """Write a tiny valid IFC4 file with one IfcWall + Qto_WallBaseQuantities and
    one IfcDoor, using ifcopenshell.api. Returns (net_side_area expected)."""
    import ifcopenshell.api.root
    import ifcopenshell.api.unit
    import ifcopenshell.api.pset

    f = ifcopenshell.file(schema="IFC4")
    # minimal owner/units so the file is well-formed enough to reopen
    f.create_entity("IfcProject", GlobalId=ifcopenshell.guid.new(), Name="M-IFC selftest")
    try:
        unit = ifcopenshell.api.unit.add_si_unit(f, unit_type="AREAUNIT", name="SQUARE_METRE")
        ifcopenshell.api.unit.assign_unit(f, units=[unit])
    except Exception:
        pass

    wall = ifcopenshell.api.root.create_entity(f, ifc_class="IfcWall", name="W1")
    door = ifcopenshell.api.root.create_entity(f, ifc_class="IfcDoor", name="D1")

    qto = ifcopenshell.api.pset.add_qto(f, product=wall, name="Qto_WallBaseQuantities")
    ifcopenshell.api.pset.edit_qto(f, qto=qto, properties={
        "NetSideArea": float(net_side_area),
        "GrossSideArea": float(gross_side_area),
    })
    f.write(path)
    return net_side_area


if __name__ == "__main__":
    import os
    import sys
    import tempfile
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if not _IFC_OK:
        print("M-IFC self-test: ifcopenshell NOT installed — import-guard path.")
        m = measure("nonexistent.ifc")
        print(f"  guard -> ok={m.ok} note={m.note}")
        sys.exit(0)

    tmpdir = tempfile.mkdtemp(prefix="mifc_selftest_")
    fixture = os.path.join(tmpdir, "one_wall.ifc")
    passed = True

    # 1) full path: a real wall with NetSideArea, distinct from gross -> ok + self_reconciled
    try:
        expected = _build_fixture_ifc(fixture, net_side_area=420.0, gross_side_area=480.0)
        m = measure(fixture)
        print(f"[fixture] ok={m.ok} qty={m.qty:,.0f} self_reconciled={m.self_reconciled} "
              f"floor={m.floor_sf}")
        print(f"          note: {m.note}")
        print(f"          breakdown: doors={m.breakdown['door_count']} "
              f"walls={m.breakdown['wall_count']} "
              f"net_eq_gross={m.breakdown['walls_net_eq_gross']}")
        assert m.ok, "expected ok=True on a wall with quantities"
        assert abs(m.qty - expected) < 0.5, f"expected qty≈{expected}, got {m.qty}"
        assert m.self_reconciled, "net distinct from gross should self-reconcile"
        assert m.breakdown["door_count"] == 1, "expected 1 door counted"
        print("  PASS: NetSideArea recovered, door counted, self_reconciled.")
    except Exception as ex:
        passed = False
        print(f"  FAIL (fixture): {ex}")

    # 2) exporter-bug path: net == gross -> ok but NOT self_reconciled
    try:
        bug = os.path.join(tmpdir, "net_eq_gross.ifc")
        _build_fixture_ifc(bug, net_side_area=500.0, gross_side_area=500.0)
        m2 = measure(bug)
        print(f"[net==gross] ok={m2.ok} qty={m2.qty:,.0f} self_reconciled={m2.self_reconciled}")
        assert m2.ok and not m2.self_reconciled, "net==gross must drop self_reconciled"
        print("  PASS: exporter-bug (net==gross) flagged, lower confidence.")
    except Exception as ex:
        passed = False
        print(f"  FAIL (net==gross): {ex}")

    # 3) empty model -> ok=False
    try:
        empty = os.path.join(tmpdir, "empty.ifc")
        f = ifcopenshell.file(schema="IFC4")
        f.create_entity("IfcProject", GlobalId=ifcopenshell.guid.new(), Name="empty")
        f.write(empty)
        m3 = measure(empty)
        print(f"[empty] ok={m3.ok} note={m3.note}")
        assert not m3.ok, "empty model (no walls) must be ok=False"
        print("  PASS: empty model -> ok=False (honest, no fabrication).")
    except Exception as ex:
        passed = False
        print(f"  FAIL (empty): {ex}")

    print("ALL PASS" if passed else "SELF-TEST FAILED")
    sys.exit(0 if passed else 1)
