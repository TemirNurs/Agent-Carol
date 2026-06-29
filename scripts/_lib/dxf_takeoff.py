#!/usr/bin/env python3
r"""dxf_takeoff.py — M-DXF: the STRUCTURED CAD GEOMETRY measurement engine.

When a DWG/DXF survives with its NCS layer names intact, the wall/door/room
geometry a painter needs is already labeled — no scale guessing, no poché-hatch
noise, no whole-sheet vector swing. This reads a .dxf with ezdxf, classifies
every entity's layer via ncs_layers.classify_layer(), and measures ONLY the
architectural roles:

  WALL_ADD      sum LINE / LWPOLYLINE / POLYLINE lengths = wall centerline LF
                -> wall_sf = LF x height_ft x face_factor (interior-dominant ~2.0)
  DOOR_COUNT    count INSERT (block reference) instances = exact plan-counted doors
  GLAZ_SUBTRACT note glazing openings (storefront not painted)
  ROOM_BOUNDARY closed LWPOLYLINE area via shoelace = floor_sf

family = 'model' — clean layer-labeled CAD geometry is a structured source. This
is a real geometry-class measure for the reconciler. self_reconciled=True when
wall layers were found AND the floor area is plausible (wall:floor ratio sane),
which gives the lone-measure tripwire its internal corroboration.

UNITS: respect ezdxf doc.units (1=inches, 2=feet, 4=mm, 5=cm, 6=meters ...) and
convert every length/area to FEET / SF. Unknown units -> assume feet and FLAG.

A DWG must first be converted to DXF by the ODA File Converter (out of scope);
this engine accepts .dxf only.
"""
from __future__ import annotations
import math
import os

# IMPORT-GUARD the heavy lib: if ezdxf is missing, measure() withholds its vote
# instead of crashing.
try:
    import ezdxf
    _EZDXF_OK = True
    _EZDXF_ERR = ""
except Exception as e:           # pragma: no cover - environment guard
    ezdxf = None
    _EZDXF_OK = False
    _EZDXF_ERR = str(e)

try:
    from . import takeoff_reconcile as R
except ImportError:
    import takeoff_reconcile as R
try:
    from . import ncs_layers as NCS
except ImportError:
    import ncs_layers as NCS


# ezdxf $INSUNITS code -> feet-per-unit conversion factor.
_UNIT_TO_FEET = {
    0: (1.0, "unitless"),       # unspecified -> assume feet, FLAG
    1: (1.0 / 12.0, "inches"),
    2: (1.0, "feet"),
    3: (5280.0, "miles"),
    4: (1.0 / 304.8, "mm"),
    5: (1.0 / 30.48, "cm"),
    6: (1.0 / 0.3048, "meters"),
    7: (1000.0 / 0.3048, "km"),
    8: (1.0 / 304800.0, "microinches"),
    9: (1.0 / 12000.0, "mils"),
    10: (3.0, "yards"),
    14: (1.0 / 3048000.0, "decimeters?"),  # rarely used; safe fallback
}
DEFAULT_HEIGHT = 10.0
# wall:floor sanity band for the self-reconciliation flag (wide — accept any
# building type; the reconciler applies the type-specific band itself).
WALL_FLOOR_SANE = (0.6, 5.0)


def _unit_factor(doc) -> tuple[float, str, bool]:
    """Return (feet_per_unit, unit_name, assumed_flag)."""
    try:
        code = int(doc.units)
    except Exception:
        code = 0
    if code in _UNIT_TO_FEET and code not in (0,):
        f, name = _UNIT_TO_FEET[code]
        return f, name, False
    # unknown / unitless -> assume drawing units are feet, FLAG it
    return 1.0, _UNIT_TO_FEET.get(code, (1.0, "unknown"))[1], True


def _poly_segment_lengths(points) -> float:
    """Sum segment lengths of an (x,y[,...]) point list. Open polyline: n-1 segs."""
    total = 0.0
    pts = [(float(p[0]), float(p[1])) for p in points]
    for i in range(1, len(pts)):
        ax, ay = pts[i - 1]
        bx, by = pts[i]
        total += math.hypot(bx - ax, by - ay)
    return total


def _shoelace_area(points) -> float:
    """Absolute polygon area via the shoelace formula (drawing units^2)."""
    pts = [(float(p[0]), float(p[1])) for p in points]
    n = len(pts)
    if n < 3:
        return 0.0
    s = 0.0
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def measure(dxf_path: str, height_ft: float = DEFAULT_HEIGHT, scope: str = "new",
            face_factor: float = 2.0) -> R.Method:
    if not _EZDXF_OK:
        return R.Method("dxf", "model", ok=False,
                        note=f"M-DXF: ezdxf not installed — vote withheld ({_EZDXF_ERR})")
    if not dxf_path or not os.path.exists(dxf_path):
        return R.Method("dxf", "model", ok=False,
                        note=f"M-DXF: file not found ({dxf_path})")
    try:
        doc = ezdxf.readfile(dxf_path)
    except Exception as e:
        return R.Method("dxf", "model", ok=False,
                        note=f"M-DXF: could not read DXF ({type(e).__name__}: {e})")

    msp = doc.modelspace()
    fpu, unit_name, unit_assumed = _unit_factor(doc)

    wall_lf = 0.0          # in FEET
    door_count = 0
    glaz_count = 0
    floor_sf = 0.0         # in SF
    room_count = 0
    layers_seen: dict[str, str] = {}     # layer name -> classified role
    disciplines_seen: set[str] = set()

    for e in msp:
        try:
            layer = e.dxf.layer
        except Exception:
            continue
        cls = NCS.classify_layer(layer, scope=scope)
        role = cls.get("role", "KEEP_UNCERTAIN")
        if layer not in layers_seen:
            layers_seen[layer] = role
            disc = cls.get("discipline")
            if disc:
                disciplines_seen.add(disc)
        etype = e.dxftype()

        if role == "WALL_ADD":
            if etype == "LINE":
                s, t = e.dxf.start, e.dxf.end
                wall_lf += math.hypot(t[0] - s[0], t[1] - s[1]) * fpu
            elif etype == "LWPOLYLINE":
                try:
                    pts = list(e.get_points("xy"))
                except Exception:
                    pts = [(p[0], p[1]) for p in e.get_points()]
                seg = _poly_segment_lengths(pts)
                if getattr(e, "closed", False) or e.dxf.flags & 1:
                    # closed polyline: add the closing segment
                    if len(pts) >= 2:
                        seg += math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1])
                wall_lf += seg * fpu
            elif etype == "POLYLINE":
                vpts = [(v.dxf.location[0], v.dxf.location[1]) for v in e.vertices]
                seg = _poly_segment_lengths(vpts)
                if e.is_closed and len(vpts) >= 2:
                    seg += math.hypot(vpts[0][0] - vpts[-1][0], vpts[0][1] - vpts[-1][1])
                wall_lf += seg * fpu

        elif role == "DOOR_COUNT":
            if etype == "INSERT":
                door_count += 1

        elif role == "GLAZ_SUBTRACT":
            if etype == "INSERT":
                glaz_count += 1

        elif role == "ROOM_BOUNDARY":
            if etype == "LWPOLYLINE":
                try:
                    pts = list(e.get_points("xy"))
                except Exception:
                    pts = [(p[0], p[1]) for p in e.get_points()]
                a = _shoelace_area(pts) * (fpu ** 2)
                if a > 0:
                    floor_sf += a
                    room_count += 1
            elif etype == "POLYLINE" and e.is_closed:
                vpts = [(v.dxf.location[0], v.dxf.location[1]) for v in e.vertices]
                a = _shoelace_area(vpts) * (fpu ** 2)
                if a > 0:
                    floor_sf += a
                    room_count += 1
            elif etype == "HATCH":
                # area-fill room polys sometimes arrive as hatches; use bounding paths
                pass

    wall_layers = [n for n, r in layers_seen.items() if r == "WALL_ADD"]

    # GUARD: no architectural wall layers -> honest withhold
    if not wall_layers or wall_lf <= 0:
        disc_list = ", ".join(sorted(d for d in disciplines_seen if d)) or "none"
        return R.Method("dxf", "model", ok=False,
                        note=f"M-DXF: no architectural wall layers in DXF "
                             f"(wall_lf={wall_lf:.0f}); disciplines seen: {disc_list}; "
                             f"layers: {', '.join(sorted(layers_seen)[:8])}")

    wall_sf = wall_lf * height_ft * face_factor

    # self-reconciliation: wall layers found AND floor area present + plausible ratio
    self_rec = False
    ratio_note = ""
    if floor_sf > 0:
        ratio = wall_sf / floor_sf
        ratio_note = f" wall:floor={ratio:.2f}x"
        if WALL_FLOOR_SANE[0] <= ratio <= WALL_FLOOR_SANE[1]:
            self_rec = True

    unit_flag = " [UNITS ASSUMED feet]" if unit_assumed else ""
    role_counts: dict[str, int] = {}
    for r in layers_seen.values():
        role_counts[r] = role_counts.get(r, 0) + 1
    note = (f"M-DXF: {len(wall_layers)} wall layer(s), wall_lf={wall_lf:,.0f} LF x "
            f"{height_ft:g}ft x {face_factor:g} = {wall_sf:,.0f} SF net-of-openings-later; "
            f"doors(counted)={door_count}, glazing={glaz_count}, rooms={room_count}, "
            f"floor={floor_sf:,.0f} SF{ratio_note}; units={unit_name}{unit_flag}")

    m = R.Method("dxf", "model", ok=True, qty=round(wall_sf, 0), scale_locked=True,
                 self_reconciled=self_rec,
                 floor_sf=(round(floor_sf, 0) if floor_sf > 0 else None), note=note)
    m.breakdown = {
        "wall_lf": round(wall_lf, 1),
        "wall_sf": round(wall_sf, 0),
        "door_count": door_count,
        "glaz_count": glaz_count,
        "floor_sf": round(floor_sf, 0),
        "room_count": room_count,
        "height_ft": height_ft,
        "face_factor": face_factor,
        "units": unit_name,
        "units_assumed": unit_assumed,
        "layers_seen": layers_seen,
        "role_counts": role_counts,
        "wall_layers": wall_layers,
    }
    return m


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if not _EZDXF_OK:
        m = measure("/nonexistent.dxf")
        print(f"M-DXF self-test: ok={m.ok}  {m.note}")
        sys.exit(0)

    import tempfile
    # Build a tiny DXF in-memory: a 10ft x 12ft rectangle of LWPOLYLINEs on
    # 'A-WALL', plus one door INSERT on 'A-DOOR'.
    doc = ezdxf.new()
    doc.units = 2  # feet
    msp = doc.modelspace()
    doc.layers.add("A-WALL")
    doc.layers.add("A-DOOR")
    # rectangle 10 (x) by 12 (y): perimeter = 2*(10+12) = 44 LF
    msp.add_lwpolyline([(0, 0), (10, 0), (10, 12), (0, 12)], close=True,
                       dxfattribs={"layer": "A-WALL"})
    # one door block reference
    doc.blocks.new(name="DOOR36")
    msp.add_blockref("DOOR36", (2, 0), dxfattribs={"layer": "A-DOOR"})

    tmp = os.path.join(tempfile.gettempdir(), "_mdxf_selftest.dxf")
    doc.saveas(tmp)

    m = measure(tmp, height_ft=10.0)
    print(f"M-DXF self-test: ok={m.ok} wall_sf={m.qty:,.0f} self_reconciled={m.self_reconciled}")
    print(f"  {m.note}")
    bd = getattr(m, "breakdown", {})
    print(f"  wall_lf={bd.get('wall_lf')}  door_count={bd.get('door_count')}  "
          f"floor_sf={bd.get('floor_sf')}  units={bd.get('units')}")

    ok = (m.ok and abs(bd.get("wall_lf", 0) - 44) < 0.5 and bd.get("door_count") == 1)
    print(f"  PASS={ok} (expected wall_lf~=44, door_count=1)")
    try:
        os.remove(tmp)
    except OSError:
        pass
    sys.exit(0 if ok else 1)
