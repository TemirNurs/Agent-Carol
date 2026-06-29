#!/usr/bin/env python3
r"""ncs_layers.py — classify CAD/PDF layer names by the US National CAD Standard.

Construction drawings are authored in Revit/CAD where every line lives on a named
layer following the AIA/NCS convention: <DISCIPLINE>-<MAJOR>-<MINOR...>-<STATUS>,
e.g. A-WALL-FULL-N, A-DOOR, A-GLAZ, A-FLOR-OTLN, A-AREA-IDEN, C-WALL, S-COLS.

When those layers survive in a PDF (OCG) or DWG, we can isolate EXACTLY the
wall/door/room geometry a painter needs and drop dimensions, furniture, grid and
poché-hatch — the noise that makes a whole-sheet vector read swing. This maps a
layer name -> a takeoff ROLE so the engine keeps only what it should measure.

ROLES: WALL_ADD (measure as wall), DOOR_COUNT (count instances), GLAZ_SUBTRACT
(openings to deduct from gross wall), ROOM_BOUNDARY (floor-area polygons),
ROOM_TEXT/READ_ONLY (labels), CEILING, FINISH, IGNORE_NOISE (furniture/fixtures/
centerlines), IGNORE_ANNO (dims/text/grid/titleblock), KEEP_UNCERTAIN (unknown
A/I layer — surfaced, never silently dropped), DROP_DISCIPLINE (non-arch).
"""
from __future__ import annotations
import json
import os

# disciplines a painting sub measures off of (Architectural, Interiors). Everything
# else (Civil/Struct/Mech/Elec/Plumb/Fire/Landscape/Survey/Telecom/Equip/General/
# Hazmat/Other) is dropped for paint takeoff.
KEEP_DISCIPLINES = {"A", "I"}
ANNO_MINORS = {"DIMS", "TEXT", "NOTE", "KEYN", "LABL", "MARK", "TITL", "TTLB",
               "SCHD", "GRID", "IDEN", "ANNO", "REVS", "REVC", "NPLT"}
WALL_NOISE_MINORS = {"CAVI", "CNTR", "HEAD", "JAMB", "PATT", "HATC"}   # not wall FACES
FLOR_KEEP_MINORS = {"OTLN", "OVHD", ""}                                # floor outline = room boundary
NOISE_MAJORS = {"FURN", "EQPM", "HVAC", "LITE", "ROOF", "FIXT", "CASE", "WDWK",
                "TPTN", "STRS", "SPCQ", "PFIX", "SIGN"}


def _load_map():
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "data", "config", "ncs_layer_map.json")
    # walk up to repo /data/config
    p2 = os.path.join(os.path.dirname(__file__), "..", "..", "data", "config", "ncs_layer_map.json")
    for cand in (p, os.path.abspath(p2)):
        if os.path.exists(cand):
            try:
                return json.load(open(cand, encoding="utf-8"))
            except Exception:
                pass
    return {}


_OVERRIDE = _load_map()   # optional data-driven overrides {exact_name_or_major: role}


def classify_layer(name: str, scope: str = "new") -> dict:
    """Return {'role', 'discipline', 'major', 'status', 'reason'} for a layer name.
    scope: 'new' (default) or 'repaint' — controls whether EXISTING (-E) is kept."""
    raw = (name or "").strip()
    if not raw:
        return {"role": "IGNORE_ANNO", "reason": "empty"}
    # data override by exact (case-insensitive) name
    if raw.upper() in {k.upper() for k in _OVERRIDE}:
        for k, v in _OVERRIDE.items():
            if k.upper() == raw.upper():
                return {"role": v, "discipline": "?", "reason": "override"}
    # strip a CAD layer prefix like 'bor|' or 'xref$0$'
    n = raw.split("|")[-1].split("$")[-1].upper()
    fields = [f for f in n.replace("_", "-").split("-") if f != ""]
    if not fields:
        return {"role": "KEEP_UNCERTAIN", "reason": f"unparseable '{raw}'"}
    disc = fields[0][0]
    major = fields[1] if len(fields) > 1 else ""
    minors = set(fields[2:]) if len(fields) > 2 else set()
    status = fields[-1] if fields[-1] in {"N", "E", "D", "X", "F"} and len(fields) > 2 else ""
    out = {"discipline": disc, "major": major, "status": status}

    if disc not in KEEP_DISCIPLINES:
        out["role"] = "DROP_DISCIPLINE"; out["reason"] = f"discipline {disc}"; return out
    # status gate
    if status == "D":
        out["role"] = "EXCLUDE"; out["reason"] = "demolition (-D)"; return out
    if status == "X":
        out["role"] = "EXCLUDE"; out["reason"] = "not-in-contract (-X)"; return out
    if status == "E" and scope != "repaint":
        out["role"] = "EXCLUDE"; out["reason"] = "existing-to-remain (-E), new-work scope"; return out
    # annotation minors anywhere -> annotation
    if minors & ANNO_MINORS or major in ANNO_MINORS:
        out["role"] = "IGNORE_ANNO"; out["reason"] = "annotation/dim/grid/titleblock"; return out
    # role by major
    if major in {"WALL", "PRTN"}:
        out["role"] = "IGNORE_NOISE" if (minors & WALL_NOISE_MINORS) else "WALL_ADD"
        out["reason"] = "wall (centerline/jamb/hatch dropped)" if out["role"] == "IGNORE_NOISE" else "wall face"
    elif major == "COLS":
        out["role"] = "WALL_ADD"; out["reason"] = "column (painted face)"
    elif major == "DOOR":
        out["role"] = "DOOR_COUNT"; out["reason"] = "door instances"
    elif major == "GLAZ":
        out["role"] = "GLAZ_SUBTRACT"; out["reason"] = "glazing/storefront (subtract from wall, not painted)"
    elif major in {"CLNG", "CEIL"}:
        out["role"] = "CEILING"; out["reason"] = "ceiling"
    elif major in {"FINI", "FNSH"} or "FNSH" in minors:
        out["role"] = "FINISH"; out["reason"] = "finish"
    elif major == "AREA":
        out["role"] = "ROOM_TEXT" if "OCCP" in minors or "IDEN" in minors else "ROOM_BOUNDARY"
        out["reason"] = "area/room"
    elif major == "FLOR":
        out["role"] = "ROOM_BOUNDARY" if (minors & FLOR_KEEP_MINORS or not minors) else "IGNORE_NOISE"
        out["reason"] = "floor outline" if out["role"] == "ROOM_BOUNDARY" else "floor fixtures/casework"
    elif major in NOISE_MAJORS:
        out["role"] = "IGNORE_NOISE"; out["reason"] = f"{major} (furniture/fixtures/MEP)"
    else:
        out["role"] = "KEEP_UNCERTAIN"; out["reason"] = f"unknown {disc}-{major} (flag, don't drop)"
    return out


def has_arch_layers(names: list[str]) -> bool:
    """True if any layer resolves to a real architectural wall/door/glazing/room role
    (= a layered-vector PDF we can isolate)."""
    keep = {"WALL_ADD", "DOOR_COUNT", "GLAZ_SUBTRACT", "ROOM_BOUNDARY", "FINISH", "CEILING"}
    return any(classify_layer(nm).get("role") in keep for nm in (names or []))


def summarize(names: list[str]) -> dict:
    roles = {}
    for nm in names or []:
        r = classify_layer(nm)["role"]
        roles.setdefault(r, []).append(nm)
    return {"arch_layers_present": has_arch_layers(names),
            "roles": {k: len(v) for k, v in roles.items()},
            "wall_layers": roles.get("WALL_ADD", []), "door_layers": roles.get("DOOR_COUNT", []),
            "room_layers": roles.get("ROOM_BOUNDARY", [])}


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    tests = sys.argv[1:] or ["A-WALL-FULL-N", "A-WALL-CNTR", "A-DOOR", "A-GLAZ", "A-FLOR-OTLN",
                             "A-FLOR-FIXT", "A-AREA-IDEN", "A-ANNO-DIMS", "C-WALL", "S-COLS",
                             "bor|G-ANNO-TTLB", "v-sswr-strc", "I-FURN", "A-WALL-DEMO-D"]
    for t in tests:
        c = classify_layer(t)
        print(f"  {t:24} -> {c['role']:16} ({c.get('reason')})")
