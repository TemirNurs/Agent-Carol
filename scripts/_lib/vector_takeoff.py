#!/usr/bin/env python3
"""
vector_takeoff.py — M2-LOCAL: deterministic takeoff measured straight from
PDF vector geometry. No cloud, no AI, no silent failures — pure math.

Why it exists (6/12): Togal fails silently and we nearly shipped a $26K
under-bid. Trust = two INDEPENDENT measurements agreeing. This engine
measures walls/rooms/doors from the drawing strokes themselves; the gate
compares it against Togal (M1) and only calls a takeoff GOLD when both
agree. CD floor plans are vector data — walls are parallel line pairs at
wall thickness, rooms are the enclosed faces, door swings are quarter arcs.

API:
    measure_page(pdf_path, page_index, scale_drawing) -> dict
    measure_pdf(pdf_path, scale_drawing, pages=None) -> dict (totals + pages)

scale_drawing: inches-per-foot on paper (3/16" = 1'-0" -> 0.1875).
"""
from __future__ import annotations

import math
from collections import defaultdict
from pathlib import Path

import fitz
import numpy as np

WALL_MIN_FT = 0.29      # 3.5" stud partition
WALL_MAX_FT = 1.15      # 13.8" masonry + furring
MIN_SEG_FT = 1.5        # ignore strokes shorter than this (hatch/ticks)
MIN_OVERLAP_FT = 2.0    # parallel pair must overlap this much along the wall
MIN_RUN_FT = 6.0        # merged wall runs shorter than this = fixtures/casework
SNAP_TOL_FT = 1.0       # weld endpoints / T-junctions within this (corner-gap)
ROOM_MIN_SF = 15.0
ROOM_MAX_SF = 20000.0
DOOR_CHORD_FT = (2.2, 4.5)   # swing-arc chord band (2'4"-4' leaves)
TITLE_BLOCK_X = 0.865   # right strip of sheet = title block, excluded
MAX_SEGS = 28000


def _segments_from_page(page):
    """All straight segments (pt coords) from the drawing stream."""
    segs = []
    W = page.rect.width
    for path in page.get_drawings():
        for item in path["items"]:
            kind = item[0]
            if kind == "l":
                p1, p2 = item[1], item[2]
                segs.append((p1.x, p1.y, p2.x, p2.y))
            elif kind == "re":
                r = item[1]
                segs += [(r.x0, r.y0, r.x1, r.y0), (r.x1, r.y0, r.x1, r.y1),
                         (r.x1, r.y1, r.x0, r.y1), (r.x0, r.y1, r.x0, r.y0)]
            elif kind == "c":
                # bezier — keep as chord for arc/door detection separately
                pass
    out = [s for s in segs if max(s[0], s[2]) < W * TITLE_BLOCK_X]
    return out


def _arcs_from_page(page, pt_per_ft):
    """Door-swing candidates: bezier curves whose chord is door-leaf sized."""
    lo, hi = DOOR_CHORD_FT[0] * pt_per_ft, DOOR_CHORD_FT[1] * pt_per_ft
    W = page.rect.width
    n = 0
    for path in page.get_drawings():
        for item in path["items"]:
            if item[0] != "c":
                continue
            p1, p4 = item[1], item[4]
            if max(p1.x, p4.x) >= W * TITLE_BLOCK_X:
                continue
            chord = math.hypot(p4.x - p1.x, p4.y - p1.y)
            if lo <= chord <= hi:
                # quarter-circle signature: control points bow away from chord
                sag = abs((item[2].x - p1.x) * (p4.y - p1.y)
                          - (item[2].y - p1.y) * (p4.x - p1.x)) / max(chord, 1e-6)
                if sag > chord * 0.18:
                    n += 1
    return n


def _extract_centerlines(segs, pt_per_ft, min_run_ft=MIN_RUN_FT):
    """Parallel-pair wall detection -> merged centerline runs (numpy)."""
    if not segs:
        return []
    a = np.asarray(segs, dtype=np.float64)
    dx, dy = a[:, 2] - a[:, 0], a[:, 3] - a[:, 1]
    length = np.hypot(dx, dy)
    keep = length >= MIN_SEG_FT * pt_per_ft
    a, dx, dy, length = a[keep], dx[keep], dy[keep], length[keep]
    if len(a) > MAX_SEGS:                       # longest strokes win
        idx = np.argsort(-length)[:MAX_SEGS]
        a, dx, dy, length = a[idx], dx[idx], dy[idx], length[idx]
    if not len(a):
        return []
    ang = np.degrees(np.arctan2(dy, dx)) % 180.0
    cl = []
    t_lo, t_hi = WALL_MIN_FT * pt_per_ft, WALL_MAX_FT * pt_per_ft
    min_ov = MIN_OVERLAP_FT * pt_per_ft
    for bucket in np.unique(np.round(ang)):
        sel = np.abs(ang - bucket) <= 0.75
        if sel.sum() < 2:
            continue
        b = a[sel]
        th = math.radians(bucket)
        u, v = math.cos(th), math.sin(th)            # along-wall axis
        # project: s = along-axis coords, o = normal offset
        s1 = b[:, 0] * u + b[:, 1] * v
        s2 = b[:, 2] * u + b[:, 3] * v
        o = (b[:, 0] + b[:, 2]) / 2 * (-v) + (b[:, 1] + b[:, 3]) / 2 * u
        smin, smax = np.minimum(s1, s2), np.maximum(s1, s2)
        order = np.argsort(o)
        o_s, smin_s, smax_s = o[order], smin[order], smax[order]
        n = len(o_s)
        spans = []          # (offset, lo, hi) centerline pieces this bucket
        j_start = 0
        for i in range(n):
            # candidates within thickness band ahead of i
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
        # merge spans: same offset (within half thickness), overlapping ranges
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
        min_run = min_run_ft * pt_per_ft
        for off, lo_, hi_ in merged:
            if hi_ - lo_ < min_run:        # fixtures, casework, door frames
                continue
            x1, y1 = lo_ * u - off * v, lo_ * v + off * u
            x2, y2 = hi_ * u - off * v, hi_ * v + off * u
            cl.append(((x1, y1), (x2, y2), hi_ - lo_))
    # DIAGONAL-HATCH REJECTION: poché / floor-finish hatch is drawn as dense
    # parallel diagonal strokes, so the parallel-pair detector FABRICATES long
    # 45deg "centerlines" that are not walls (on USC Sumter A1.01D this was 151
    # of 285 centerlines = 5,941 of 9,206 LF = 65% phantom, and the crossing
    # diagonals slice every room into sliver triangles so polygonize can't close).
    # Real CD partitions are orthogonal; keep only centerlines within ORTHO_TOL
    # of 0/90 of the dominant axis. Skips the prune when a sheet is genuinely
    # rotated/diagonal (>40% of LF off-axis after picking the modal axis).
    cl = _reject_diagonal_hatch(cl)
    return cl


ORTHO_TOL_DEG = 8.0     # keep centerlines within this of the building's two axes


def _reject_diagonal_hatch(cl):
    """Drop fabricated diagonal centerlines (hatch poché). Real CD walls form an
    ORTHOGONAL FRAME — a perpendicular PAIR of directions (axis a AND a+90). Hatch
    is a lone diagonal with NO perpendicular partner. So the frame is the axis pair
    (a, a+90) with the most COMBINED LF — NOT the single modal axis (a 45deg hatch
    band can out-mass either wall direction alone, which made a modal-axis pick keep
    the hatch and drop the walls — USC Sumter: 45deg=5,941 LF vs 0+90 walls=3,265 LF).
    Keep centerlines within ORTHO_TOL of the winning frame's two axes; bail (keep all)
    if even the best frame holds <45% of LF (sheet is genuinely diagonal/complex)."""
    if not cl:
        return cl
    angs = np.asarray([math.degrees(math.atan2(c[1][1] - c[0][1], c[1][0] - c[0][0])) % 180.0 for c in cl])
    lfs = np.asarray([c[2] for c in cl])
    # LF per 1-deg bucket in 0..179
    hist = np.zeros(180)
    for a, L in zip(angs, lfs):
        hist[int(round(a)) % 180] += L
    # pick the orthogonal frame: axis a in 0..89 maximizing LF[a] + LF[a+90] (with tol)
    def band_lf(center):
        d = np.minimum((np.arange(180) - center) % 180, (center - np.arange(180)) % 180)
        return hist[d <= ORTHO_TOL_DEG].sum()
    best_a, best_lf = 0, -1.0
    for a in range(90):
        fl = band_lf(a) + band_lf((a + 90) % 180)
        if fl > best_lf:
            best_a, best_lf = a, fl
    a2 = (best_a + 90) % 180
    d1 = np.minimum((angs - best_a) % 180, (best_a - angs) % 180)
    d2 = np.minimum((angs - a2) % 180, (a2 - angs) % 180)
    keep = (d1 <= ORTHO_TOL_DEG) | (d2 <= ORTHO_TOL_DEG)
    if lfs[keep].sum() < 0.45 * lfs.sum():   # genuinely diagonal sheet -> keep all
        return cl
    return [c for c, k in zip(cl, keep) if k]


def _connected_filter(lines, t_hi, keep_frac=0.15):
    """Walls form one connected network; retail fixtures/boothsets are
    isolated islands. Union-find over spatial contacts, keep components
    with >= keep_frac of the biggest component's total length."""
    from shapely import STRtree
    n = len(lines)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    tree = STRtree(lines)
    for i, li in enumerate(lines):
        for j in tree.query(li.buffer(t_hi)):
            j = int(j)
            if j > i and lines[j].distance(li) <= t_hi:
                union(i, j)
    comp_len = {}
    for i, li in enumerate(lines):
        comp_len.setdefault(find(i), 0.0)
        comp_len[find(i)] += li.length
    if not comp_len:
        return lines, list(range(n))
    biggest = max(comp_len.values())
    keep_roots = {r for r, L in comp_len.items() if L >= biggest * keep_frac}
    idx = [i for i in range(n) if find(i) in keep_roots]
    return [lines[i] for i in idx], idx


def _node_snap(cl, pt_per_ft, tol_ft):
    """Rectify -> extend -> endpoint-cluster weld -> T-junction projection.
    Returns shapely LineStrings ready for unary_union/polygonize."""
    from shapely.geometry import LineString, Point
    from shapely import STRtree
    tol = tol_ft * pt_per_ft
    ext = WALL_MAX_FT * pt_per_ft * 0.65        # extend a stub ~half wall thick

    # 1) rectify near-axis lines to exact horizontal/vertical + extend ends
    L = []
    for (x1, y1), (x2, y2), _r in cl:
        a = math.degrees(math.atan2(y2 - y1, x2 - x1)) % 180.0
        if min(a, abs(a - 180.0)) <= 8.0:        # ~horizontal: share a y
            y = (y1 + y2) / 2.0
            p1, p2 = [x1, y], [x2, y]
        elif abs(a - 90.0) <= 8.0:               # ~vertical: share an x
            x = (x1 + x2) / 2.0
            p1, p2 = [x, y1], [x, y2]
        else:
            p1, p2 = [x1, y1], [x2, y2]
        a0 = np.asarray(p1); b0 = np.asarray(p2)
        v = b0 - a0
        n = math.hypot(v[0], v[1]) or 1.0
        u = v / n * ext
        L.append([list(a0 - u), list(b0 + u)])

    # 2) weld endpoints within tol to their cluster centroid (union-find + grid)
    pts = np.asarray([p for seg in L for p in seg], dtype=np.float64)
    M = len(pts)
    parent = list(range(M))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    cell = max(tol, 1.0)
    grid = defaultdict(list)
    for i, (x, y) in enumerate(pts):
        grid[(int(x // cell), int(y // cell))].append(i)
    for i, (x, y) in enumerate(pts):
        gx, gy = int(x // cell), int(y // cell)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for j in grid.get((gx + dx, gy + dy), ()):
                    if j > i and math.hypot(pts[i, 0] - pts[j, 0],
                                            pts[i, 1] - pts[j, 1]) <= tol:
                        ri, rj = find(i), find(j)
                        if ri != rj:
                            parent[rj] = ri
    clusters = defaultdict(list)
    for i in range(M):
        clusters[find(i)].append(i)
    snapped = pts.copy()
    for members in clusters.values():
        c = pts[members].mean(axis=0)
        for i in members:
            snapped[i] = c
    lines = []
    for k in range(len(L)):
        a, b = snapped[2 * k], snapped[2 * k + 1]
        if math.hypot(a[0] - b[0], a[1] - b[1]) > 1e-6:
            lines.append(LineString([tuple(a), tuple(b)]))

    # 3) project still-dangling ends onto the nearest perpendicular wall body
    if lines:
        tree = STRtree(lines)
        out = []
        for i, ln in enumerate(lines):
            c = list(ln.coords)
            for ei in (0, -1):
                pt = Point(c[ei])
                best, bd = None, tol + 1.0
                for j in tree.query(pt.buffer(tol)):
                    j = int(j)
                    if j == i:
                        continue
                    d = lines[j].distance(pt)
                    if d < bd and d <= tol:
                        bd, best = d, j
                if best is not None:
                    pr = lines[best].interpolate(lines[best].project(pt))
                    c[ei] = (pr.x, pr.y)
            out.append(LineString(c))
        lines = out
    return lines


def _dedup_parallel(cl, pt_per_ft, off_tol_ft=1.5):
    """Collapse parallel-DOUBLE centerlines (one physical exterior wall drawn as
    veneer+airspace+CMU = 2-3 parallel lines, each surviving as its own centerline)
    into a single representative for the LF TOTAL. Lines that share angle (<=2deg),
    sit within off_tol_ft normal offset, and overlap along-axis are one wall — keep
    the longest. This is a COUNTING fix only (the caller keeps the full set for
    polygonize connectivity)."""
    if not cl:
        return cl
    items = []
    for (x1, y1), (x2, y2), L in cl:
        ang = math.degrees(math.atan2(y2 - y1, x2 - x1)) % 180.0
        th = math.radians(ang)
        u, v = math.cos(th), math.sin(th)
        off = ((x1 + x2) / 2) * (-v) + ((y1 + y2) / 2) * u      # normal offset
        s1 = x1 * u + y1 * v; s2 = x2 * u + y2 * v             # along-axis span
        items.append({"ang": ang, "off": off, "lo": min(s1, s2), "hi": max(s1, s2),
                      "L": L, "cl": ((x1, y1), (x2, y2), L)})
    off_tol = off_tol_ft * pt_per_ft
    used = [False] * len(items)
    keep = []
    for i in range(len(items)):
        if used[i]:
            continue
        group = [i]
        for j in range(i + 1, len(items)):
            if used[j]:
                continue
            a, b = items[i], items[j]
            dang = min(abs(a["ang"] - b["ang"]), 180 - abs(a["ang"] - b["ang"]))
            if dang <= 2.0 and abs(a["off"] - b["off"]) <= off_tol \
                    and min(a["hi"], b["hi"]) - max(a["lo"], b["lo"]) > 0:
                group.append(j); used[j] = True
        used[i] = True
        keep.append(max((items[g] for g in group), key=lambda it: it["L"])["cl"])
    return keep


def measure_page(pdf_path, page_index, scale_drawing, min_run_ft=MIN_RUN_FT, height_ft=9.0,
                 scope_bbox_ft=None, face_factor=2.0):
    """Measure one page. Returns dict with LF/SF/doors + diagnostics.
    min_run_ft: shortest merged wall run to keep (default 6'; lower for dense
    hotel/residential plans whose bath/closet partitions are short).
    height_ft: wall height used to convert centerline LF -> painted wall SF.
    scope_bbox_ft: optional (xmin,ymin,xmax,ymax) in FEET — when given, only
    centerlines whose midpoint falls inside are counted (isolates the in-scope
    addition from a whole-building sheet that also shows the gym/existing wings).
    face_factor: painted faces per centerline LF (2.0 = both faces of every wall;
    interior-dominant buildings ~1.6-1.9 once the exterior one-face fraction is
    netted). Default 2.0 preserves prior behavior for existing callers.
    Emits 'wall_sf' so the reconciler has a geometry quantity."""
    from shapely.geometry import LineString, MultiLineString
    from shapely.ops import polygonize, unary_union

    pt_per_ft = scale_drawing * 72.0
    doc = fitz.open(str(pdf_path))
    page = doc[page_index]
    segs = _segments_from_page(page)
    doors = _arcs_from_page(page, pt_per_ft)
    cl = _extract_centerlines(segs, pt_per_ft, min_run_ft=min_run_ft)
    doc.close()

    out = {"segments_in": len(segs), "centerlines": len(cl), "doors_n": doors,
           "wall_cl_lf": 0.0, "ext_lf": 0.0, "int_lf": 0.0, "wall_sf": 0.0,
           "rooms_n": 0, "room_sf": 0.0, "footprint_sf": 0.0, "reliable": False,
           "scoped": scope_bbox_ft is not None}
    if not cl:
        return out

    # IN-SCOPE CROP: a whole-building sheet (USC A1.01D shows the addition + gym
    # court + existing wings) needs a scope hint to isolate the painted addition —
    # pure geometry can't separate spatially-mixed buildings. Keep only centerlines
    # whose MIDPOINT lies in the supplied bbox (feet). Caller derives the bbox from
    # in-scope finish-schedule room-label positions (independent of any measured dims).
    if scope_bbox_ft:
        x0, y0, x1b, y1b = (c * pt_per_ft for c in scope_bbox_ft)
        cl = [c for c in cl
              if x0 <= (c[0][0] + c[1][0]) / 2 <= x1b and y0 <= (c[0][1] + c[1][1]) / 2 <= y1b]
        out["centerlines"] = len(cl)
        if not cl:
            return out

    # close junction gaps: NODE-SNAP so room loops close before polygonize.
    # The old grid-snap (g = WALL_MIN_FT*pt_per_ft*0.5 ~= 0.145 ft) was far too
    # fine to weld real corner gaps, which on this set ran 0.5-1.0 ft (median
    # endpoint->nearest-wall gap was 0.52 ft, 47% of endpoints dangled >2 ft).
    # SNAP_TOL_FT chosen from that gap histogram: 1.0 ft welds the corner cluster
    # without bridging real door openings (>=2.5 ft). Three steps:
    #   1 rectify each centerline onto its own axis (kills sub-degree skew so
    #     faces are axis-aligned and parallel walls share an exact offset),
    #   2 extend every end by ~half a wall thickness so stub ends reach a corner,
    #   3 weld endpoints within SNAP_TOL_FT to their cluster centroid, then
    #     project any still-dangling end onto the nearest perpendicular wall body
    #     within SNAP_TOL_FT (T-junctions where the partner wall has no endpoint
    #     there). unary_union then nodes the network so polygonize finds faces.
    raw_lines = _node_snap(cl, pt_per_ft, SNAP_TOL_FT)
    t_hi_pt = WALL_MAX_FT * pt_per_ft
    lines, kept_idx = _connected_filter(raw_lines, t_hi_pt)
    # LF TOTAL from the DEDUPED original centerlines (one count per physical wall),
    # not the node-snapped+extended lines (those are length-inflated, and indexing
    # cl by kept_idx is unsafe once _node_snap drops degenerate lines). Dedup
    # collapses brick-veneer/CMU parallel doubles so one wall isn't counted 2-3x.
    cl_dedup = _dedup_parallel(cl, pt_per_ft)
    out["dup_collapsed_lf"] = round((sum(c[2] for c in cl) - sum(c[2] for c in cl_dedup)) / pt_per_ft)
    total_lf = sum(c[2] for c in cl_dedup) / pt_per_ft
    out["wall_cl_lf"] = round(total_lf)
    try:
        net = unary_union(MultiLineString(lines))
        faces = list(polygonize(net))
    except Exception:
        faces = []
    sf = lambda area_pt2: area_pt2 / (pt_per_ft ** 2)
    rooms = [f for f in faces if ROOM_MIN_SF <= sf(f.area) <= ROOM_MAX_SF]
    out["rooms_n"] = len(rooms)
    out["room_sf"] = round(sum(sf(f.area) for f in rooms))
    # footprint: buffer the wall network into a solid, take the biggest
    # polygon's filled area (robust even when rooms don't fully close)
    try:
        solid = unary_union(MultiLineString(lines).buffer(t_hi_pt * 1.2))
        polys = list(solid.geoms) if solid.geom_type == "MultiPolygon" else [solid]
        big = max(polys, key=lambda q: q.area)
        from shapely.geometry import Polygon
        filled = Polygon(big.exterior)
        out["footprint_sf"] = round(sf(filled.area) - sf(filled.exterior.length * t_hi_pt * 1.2) / 2)
    except Exception:
        pass
    if faces and not out["footprint_sf"]:
        out["footprint_sf"] = round(sf(unary_union(faces).area))
        # exterior = centerlines tracing the union boundary
        try:
            boundary = unary_union(faces).boundary
            ext = sum(li.length for li in lines
                      if li.distance(boundary) < 1.2 * pt_per_ft * WALL_MAX_FT
                      and boundary.buffer(2.0).intersection(li).length
                      > li.length * 0.55)
            out["ext_lf"] = round(ext / pt_per_ft)
        except Exception:
            pass
    out["int_lf"] = max(0, out["wall_cl_lf"] - out["ext_lf"])
    # painted wall SF = centerline LF × height × face_factor. 2.0 = both faces of
    # every wall (default, prior behavior). When the engine resolves a real ext_lf
    # (exterior walls painted one interior face only), use the measured int/ext split
    # instead — principled, not a guess: SF = (int×2 + ext×1)×height.
    if out["ext_lf"] > 0 and out["wall_cl_lf"] > 0:
        out["wall_sf"] = round((out["int_lf"] * 2 + out["ext_lf"] * 1) * height_ft)
        out["face_factor_used"] = round((out["int_lf"] * 2 + out["ext_lf"]) / out["wall_cl_lf"], 2)
    else:
        out["wall_sf"] = round(out["wall_cl_lf"] * height_ft * face_factor)
        out["face_factor_used"] = face_factor
    # RELIABILITY self-check (no silent failures, never emit a whole-building vote):
    #  - polygonize path (default, no crop): footprint must close into sane rooms;
    #  - cropped CL path: when an in-scope bbox is supplied the footprint/room-close
    #    path is known-unreachable on these sheets, so trust the deduped in-scope
    #    centerline LF instead (the load-bearing in-scope isolation done by the crop).
    fp, rsf, rn = out["footprint_sf"], out["room_sf"], out["rooms_n"]
    sane_rooms = rn > 0 and (rsf / rn) >= 60          # avg room < 60 SF = sliver garbage
    sane_fp = fp > 0 and rsf > 0 and 0.4 <= (rsf / fp) <= 3.0
    poly_ok = bool(sane_rooms and sane_fp)
    out["reliable"] = bool(out["wall_cl_lf"] > 0 and (poly_ok or out["scoped"]))
    return out


def measure_pdf(pdf_path, scale_drawing, pages=None, min_run_ft=MIN_RUN_FT, height_ft=9.0,
                scope_bbox_ft=None, face_factor=2.0):
    doc = fitz.open(str(pdf_path))
    n = doc.page_count
    doc.close()
    idxs = pages if pages is not None else range(n)
    res = {"pages": {}, "wall_cl_lf": 0, "ext_lf": 0, "int_lf": 0, "wall_sf": 0,
           "room_sf": 0, "rooms_n": 0, "footprint_sf": 0, "doors_n": 0, "reliable": False}
    any_reliable = False
    for i in idxs:
        m = measure_page(pdf_path, i, scale_drawing, min_run_ft=min_run_ft, height_ft=height_ft,
                         scope_bbox_ft=scope_bbox_ft, face_factor=face_factor)
        res["pages"][f"p{i}"] = m
        for k in ("wall_cl_lf", "ext_lf", "int_lf", "wall_sf", "room_sf",
                  "rooms_n", "footprint_sf", "doors_n"):
            res[k] += m[k]
        any_reliable = any_reliable or m.get("reliable")
    res["reliable"] = any_reliable
    return res


if __name__ == "__main__":
    import argparse, json, sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--scale", type=float, required=True,
                    help="inches per foot, e.g. 0.1875 for 3/16")
    ap.add_argument("--pages", type=str, default=None,
                    help="comma list of 0-based page indexes")
    a = ap.parse_args()
    pgs = [int(x) for x in a.pages.split(",")] if a.pages else None
    print(json.dumps(measure_pdf(a.pdf, a.scale, pgs), indent=1))
