#!/usr/bin/env python3
"""
togal_pipeline.py — Generalized Togal AI Takeoff Pipeline
==========================================================
Works for any project. Takes project slug, sheet list, and scale as inputs.
Reuses proven API patterns from togal_takeoff_proper.py.

Usage:
  python scripts/togal_pipeline.py --project food-lion-1513 --scale "1/8"
  python scripts/togal_pipeline.py --project food-lion-1513 --sheets "A201,A202,A301"
  python scripts/togal_pipeline.py --project food-lion-1513 --status
  python scripts/togal_pipeline.py --project food-lion-1513 --extract
  python scripts/togal_pipeline.py --project food-lion-1513 --dry-run
  python scripts/togal_pipeline.py --upload food-lion-1513 path/to/plans.pdf
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import requests
except ImportError:
    print("ERROR: requests required. Run: pip install requests")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
PROJECTS_DIR = DATA_DIR / "projects"
AUTH_FILE = DATA_DIR / "config" / "togal_auth.json"
PIPELINE_FILE = DATA_DIR / "memory" / "pipeline.json"

BASE_URL = "https://api-prod.togal.ai/api"

import re as _re


def find_project_dir(slug: str) -> Path:
    """Find the actual project directory regardless of slug format (dashes vs underscores).

    Tries multiple variants:
      1. Exact slug as-is
      2. Slug with dashes (fetch_project_docs style)
      3. Slug with underscores (estimator_agent style)
      4. Fuzzy match on existing directory names
    """
    # Try exact
    exact = PROJECTS_DIR / slug
    if exact.exists():
        return exact

    # Normalize to both variants
    dashed = _re.sub(r'[_\s]+', '-', slug.lower())
    underscored = _re.sub(r'[-\s]+', '_', slug.lower())

    for variant in [dashed, underscored]:
        p = PROJECTS_DIR / variant
        if p.exists():
            return p

    # Fuzzy: check all existing dirs for partial match
    slug_clean = _re.sub(r'[-_\s]+', '', slug.lower())
    if PROJECTS_DIR.exists():
        for d in PROJECTS_DIR.iterdir():
            if d.is_dir():
                dir_clean = _re.sub(r'[-_\s]+', '', d.name.lower())
                if slug_clean == dir_clean or slug_clean in dir_clean or dir_clean in slug_clean:
                    return d

    # Nothing found — return the dashed version (standard) and let mkdir handle it
    return PROJECTS_DIR / dashed

# Painting-relevant sheet prefixes (auto-detection patterns)
PAINTING_SHEET_PATTERNS = [
    "A201", "A202", "A203", "A204",  # Floor Plans
    "A301", "A302", "A303", "A304",  # RCPs
    "A901", "A902", "A903", "A904",  # Finish Plans
    "A912", "A913",                   # Interior Elevations
]

# Broader patterns for non-standard sheet naming (permit sets, small projects)
PAINTING_SHEET_KEYWORDS = [
    "floor plan", "finish plan", "finish schedule", "reflected ceiling",
    "rcp", "interior elev", "paint", "color schedule", "wall finish",
    "enlarged plan", "demolition", "partition",
    # Page-number style (permit sets often use plain numbers or A-prefixed)
    "a1", "a2", "a3", "a4", "a5", "a6", "a7", "a8", "a9",
]

# Common architectural scales
SCALE_PRESETS = {
    "1/8":  (0.125, 12, "architectural"),   # 1/8" = 1'-0"
    "1/4":  (0.25, 12, "architectural"),    # 1/4" = 1'-0"
    "3/16": (0.1875, 12, "architectural"),  # 3/16" = 1'-0"
    "1/16": (0.0625, 12, "architectural"),  # 1/16" = 1'-0"
    "3/8":  (0.375, 12, "architectural"),   # 3/8" = 1'-0"
    "1/2":  (0.5, 12, "architectural"),     # 1/2" = 1'-0"
}

# Classifications that are NOT painting scope
NON_PAINTING_CLASSIFICATIONS = {
    "Parking Lot", "Parking", "Shafts", "Shaft", "Elevator", "Elevators",
    "Balcony", "Balconies", "Openings", "Stairwell", "Staircase",
    "Mechanical", "Electrical Room", "Telecom",
}


# ---------------------------------------------------------------------------
# Classification retrieval (authoritative quantities from user-drawn takeoffs)
# ---------------------------------------------------------------------------
def get_classifications(token: str, org_id: str, set_id: str) -> list:
    """Get user-drawn classifications for a set. These are the authoritative quantities.

    Classifications are created when a user draws takeoff areas/lines/counts in Togal's
    UI. Each has properly calculated SF/LF values — no pixel conversion needed.
    Returns empty list if no classifications exist (nobody has done a takeoff yet).
    """
    rows = []
    offset = 0
    while True:
        r = requests.get(f"{BASE_URL}/v1/classification", headers=get_headers(token), params={
            "$where": json.dumps({"organization_id": org_id}),
            "$offset": str(offset),
        }, timeout=30)
        if r.status_code != 200:
            return []
        data = r.json()
        batch = data.get("rows", [])
        if not batch:
            break
        # Filter to our set
        for c in batch:
            c_set = c.get("data", {}).get("set_id") or c.get("set_id", "")
            if c_set == set_id:
                rows.append(c)
        total = data.get("count", 0)
        offset += len(batch)
        if offset >= total:
            break
    return rows


def extract_from_classifications(classifications: list) -> dict:
    """Extract takeoff quantities from Togal classifications.

    Returns a dict matching the same shape as extract_measurements() output
    but with properly named rooms and accurate SF/LF from user-drawn boundaries.
    """
    result = {"rooms": [], "walls": [], "counts": {}, "regions": [], "totals": {}}

    for cls in classifications:
        data = cls.get("data", {})
        name = cls.get("name", "Unknown")
        cls_type = data.get("type", "area")
        units = data.get("units", [])
        value = units[0].get("value", 0) if units else 0
        uom = units[0].get("uom", "") if units else ""

        if cls_type == "area" and value > 0:
            height = data.get("height", 0)
            entry = {
                "name": name,
                "area_sf": round(value, 1),
                "perimeter_lf": 0,  # Not always stored in classification
                "wall_height_ft": height,
                "classification": name,
                "source": "togal_classification",
            }
            # If height > 0, this is a wall measurement (area = wall SF, not floor SF)
            if height > 0:
                result["walls"].append(entry)
            else:
                result["rooms"].append(entry)
        elif cls_type == "line" and value > 0:
            result["walls"].append({
                "name": name,
                "area_sf": 0,
                "perimeter_lf": round(value, 1),
                "classification": name,
                "source": "togal_classification",
            })
        elif cls_type == "count" and value > 0:
            result["counts"][name] = int(value)

    result["totals"] = {
        "total_room_sf": sum(r["area_sf"] for r in result["rooms"]),
        "total_wall_sf": sum(w["area_sf"] for w in result["walls"]),
        "total_wall_lf": sum(w["perimeter_lf"] for w in result["walls"]),
        "room_count": len(result["rooms"]),
        "wall_count": len(result["walls"]),
    }

    return result


# ---------------------------------------------------------------------------
# API helpers (proven patterns from togal_takeoff_proper.py)
# ---------------------------------------------------------------------------
_API_KEY = None  # set by authenticate() when togal_auth.json provides a persistent API key


def get_headers(token):
    # Persistent API key (header 'key:') takes precedence per the Togal OpenAPI spec —
    # API keys never expire; session tokens die after 7 days.
    if _API_KEY:
        return {"Content-Type": "application/json", "key": _API_KEY}
    return {"Content-Type": "application/json", "session": token}


def authenticate(auth: dict) -> str | None:
    """Authenticate with Togal. Preference order:
      1. api_key      — persistent (header 'key:'), never expires
      2. session_token — header 'session:', 7-day TTL
      3. email+password → POST /v1/session for a fresh token
    Returns a truthy token/sentinel on success, None on failure (never raises).
    """
    global _API_KEY

    # 1. Persistent API key
    api_key = auth.get("api_key")
    if api_key:
        _API_KEY = api_key
        try:
            r = requests.get(f"{BASE_URL}/v1/user",
                             headers={"Content-Type": "application/json", "key": api_key}, timeout=15)
            if r.status_code == 200 and "id" in r.json():
                return api_key
        except Exception:
            pass
        print("[AUTH] API key was rejected by Togal — check the key value.")
        _API_KEY = None

    # 2. Existing session token
    token = auth.get("session_token")
    if token:
        try:
            r = requests.get(f"{BASE_URL}/v1/user",
                             headers={"Content-Type": "application/json", "session": token}, timeout=15)
            if r.status_code == 200 and "id" in r.json():
                return token
        except Exception:
            pass
        print("[AUTH] Saved session token expired/invalid.")

    # 3. Email + password → new session
    email = auth.get("email")
    password = auth.get("password")
    if email and password:
        try:
            r = requests.post(f"{BASE_URL}/v1/session", json={"email": email, "password": password},
                              headers={"Content-Type": "application/json"}, timeout=15)
            if r.status_code == 200:
                token = r.json().get("id")
                if token:
                    auth["session_token"] = token
                    AUTH_FILE.write_text(json.dumps(auth, indent=2))
                    return token
        except Exception:
            pass
        print("[AUTH] Email/password login failed.")

    return None


def get_all_pages(token: str, set_id: str) -> list:
    """Get all pages in a set, handling pagination."""
    r = requests.get(f"{BASE_URL}/v1/page", headers=get_headers(token), params={
        "$where": json.dumps({"set_id": set_id}),
    }, timeout=30)
    if r.status_code != 200:
        return []
    data = r.json()
    rows = data.get("rows", [])
    total = data.get("count", len(rows))
    # Fetch remaining pages if paginated
    while len(rows) < total:
        r = requests.get(f"{BASE_URL}/v1/page", headers=get_headers(token), params={
            "$where": json.dumps({"set_id": set_id}),
            "$offset": str(len(rows)),
        }, timeout=30)
        if r.status_code != 200:
            break
        batch = r.json().get("rows", [])
        if not batch:
            break
        rows.extend(batch)
    return rows


def get_page_status(token: str, page_id: str) -> dict:
    """Get status of a single page."""
    r = requests.get(f"{BASE_URL}/v1/page/{page_id}", headers=get_headers(token), timeout=30)
    return r.json() if r.status_code == 200 else {}


def filter_painting_pages(pages: list, sheet_filter: list[str] | None = None) -> list:
    """Filter pages to painting-relevant sheets.

    Uses a 3-tier fallback strategy:
    1. Strict prefix match (A201, A301, etc.) — ideal for large projects with standard naming
    2. Keyword match (floor plan, finish, rcp, etc.) — for permit sets with descriptive names
    3. ALL pages — for small projects where everything is relevant (better than nothing)
    """
    if sheet_filter:
        # User-specified sheets
        result = []
        for p in pages:
            name = p.get("name", "").upper()
            for prefix in sheet_filter:
                if name.startswith(prefix.upper()):
                    result.append(p)
                    break
        return result

    # Tier 1: Strict prefix match (standard architectural sheet numbering)
    result = []
    for p in pages:
        name = p.get("name", "").upper()
        for pattern in PAINTING_SHEET_PATTERNS:
            if name.startswith(pattern):
                result.append(p)
                break
    if result:
        return result

    # Tier 2: Keyword match (non-standard naming, permit sets)
    print("  No standard sheet names found. Trying keyword matching...")
    for p in pages:
        name = p.get("name", "").lower()
        for keyword in PAINTING_SHEET_KEYWORDS:
            if keyword in name:
                result.append(p)
                break
    if result:
        print(f"  Found {len(result)} pages via keyword match")
        return result

    # Tier 3: Use ALL pages (small projects, numbered pages, etc.)
    print(f"  No keyword matches either. Using ALL {len(pages)} pages for takeoff.")
    return pages


def set_page_scale(token: str, page: dict, scale_drawing: float,
                   scale_real: float, scale_type: str) -> bool:
    """Set scale on a page via PUT /v1/page/{id}."""
    if page.get("scale_drawing") and page.get("scale_real"):
        return True  # Already set

    payload = {
        "scale_drawing": scale_drawing,
        "scale_real": scale_real,
        "scale_type": scale_type,
        "scale_units": "imperial",
    }
    r = requests.put(f"{BASE_URL}/v1/page/{page['id']}",
                     headers=get_headers(token), json=payload, timeout=30)
    if r.status_code == 200:
        return True
    # Fallback to v2
    r2 = requests.put(f"{BASE_URL}/v2/page/{page['id']}",
                      headers=get_headers(token), json=payload, timeout=30)
    return r2.status_code == 200


def get_existing_views(token: str, page_id: str) -> list:
    """Get existing views for a page by scanning recent views."""
    # Togal's $where filter is broken for views, so scan recent entries
    offset = 0
    while offset < 500:
        r = requests.get(f"{BASE_URL}/v1/view", headers=get_headers(token), params={
            "$offset": str(offset),
        }, timeout=30)
        if r.status_code != 200:
            return []
        rows = r.json().get("rows", [])
        if not rows:
            return []
        matches = [v for v in rows if v.get("page_id") == page_id]
        if matches:
            return matches
        offset += len(rows)
    return []


def get_views_for_pages(token: str, page_ids: set) -> dict:
    """Find view IDs for a set of pages. Returns {page_id: view_id}.

    Scans views in reverse chronological order (recent first) which
    is efficient for newly processed pages.
    """
    found = {}
    offset = 0
    while len(found) < len(page_ids) and offset < 500:
        r = requests.get(f"{BASE_URL}/v1/view", headers=get_headers(token), params={
            "$offset": str(offset),
        }, timeout=30)
        if r.status_code != 200:
            break
        rows = r.json().get("rows", [])
        if not rows:
            break
        for v in rows:
            pid = v.get("page_id")
            if pid in page_ids and pid not in found:
                found[pid] = v["id"]
        offset += len(rows)
    return found


def create_view(token: str, page_id: str, name: str) -> dict | None:
    """Create a view. Returns view dict or None on failure.

    Handles 409 Conflict by fetching the existing view from page data.
    """
    r = requests.post(f"{BASE_URL}/v1/view", headers=get_headers(token), json={
        "name": name, "page_id": page_id,
    }, timeout=30)
    if r.status_code == 200:
        return r.json()
    if r.status_code == 409:
        # View already exists — find it from page data
        return _find_view_from_page(token, page_id, name)
    return None


def _find_view_from_page(token: str, page_id: str, view_name: str) -> dict | None:
    """Find existing view by checking the page's view list."""
    r = requests.get(f"{BASE_URL}/v1/page/{page_id}", headers=get_headers(token), timeout=30)
    if r.status_code == 200:
        page_data = r.json()
        # Views may be stored in page data under different keys
        views = page_data.get("views", {})
        if isinstance(views, dict):
            # Try to find by view name
            for vid, vdata in views.items():
                if isinstance(vdata, dict) and vdata.get("name") == view_name:
                    return {"id": vid, "name": view_name}
                # Sometimes the key IS the view_id
                return {"id": vid, "name": view_name}
        elif isinstance(views, list):
            for v in views:
                if isinstance(v, dict):
                    return v
    return None


def get_set_view_name(token: str, set_id: str) -> str:
    """Get the allowed view name from set permissions."""
    r = requests.get(f"{BASE_URL}/v1/set/{set_id}", headers=get_headers(token), timeout=15)
    if r.status_code == 200:
        data = r.json()
        perms = data.get("permissions", {})
        views = perms.get("views", {})
        if views:
            # Return first allowed view name
            return next(iter(views.keys()), "Paint Takeoff")
    return "Paint Takeoff"


def ensure_view(token: str, page_id: str, view_name: str) -> str | None:
    """Get existing view or create new one. Returns view_id."""
    # Try to create — handles 409 by finding existing
    view = create_view(token, page_id, view_name)
    if view:
        return view.get("id")

    # Fallback: check existing views
    existing = get_existing_views(token, page_id)
    if existing:
        return existing[0].get("id")

    return None


def run_vectorization(token: str, view_id: str) -> bool:
    """POST /v1/togal/scale/{view} — run vectorization with all measurement filters."""
    filters = {
        "gross_area": True, "gross_area_internal": True,
        "net_area": True, "walls_area": True,
        "count": True, "walls_centerline": True,
        "footprint": True, "doors_area": True,
        "doors_centerline": True, "walls_perimeter": True,
    }
    r = requests.post(f"{BASE_URL}/v1/togal/scale/{view_id}",
                      headers=get_headers(token), json=filters, timeout=60)
    if r.status_code == 200:
        return True
    # Fallback
    r2 = requests.post(f"{BASE_URL}/v1/togal/scale/{view_id}/v2",
                       headers=get_headers(token), json=filters, timeout=60)
    return r2.status_code == 200


def get_view_measurements(token: str, view_id: str) -> dict | None:
    """Get view data with geojson measurements.

    Uses POST /v1/view/{id}/geojson (not GET) to retrieve the FeatureCollection.
    """
    # Get base view info (state, filters, etc.)
    r = requests.get(f"{BASE_URL}/v1/view/{view_id}", headers=get_headers(token), timeout=30)
    if r.status_code != 200:
        return None
    view = r.json()

    # Get geojson measurements via POST (GET returns empty)
    r2 = requests.post(f"{BASE_URL}/v1/view/{view_id}/geojson",
                       headers=get_headers(token), json={}, timeout=60)
    if r2.status_code == 200:
        geojson = r2.json()
        features = geojson.get("features", []) if isinstance(geojson, dict) else geojson
        view["geojson_features"] = features
        view["geojson_feature_count"] = len(features)

    return view


def get_page_scale_info(token: str, page_id: str) -> dict | None:
    """Get page data with scale and DPI.

    Falls back to page list data since GET /v1/page/{id} often returns 404.
    """
    # Direct GET often fails — use the page data passed from get_all_pages() instead
    r = requests.get(f"{BASE_URL}/v1/page/{page_id}", headers=get_headers(token), timeout=30)
    if r.status_code == 200:
        data = r.json()
        if data.get("scale_drawing") or data.get("url_dpi"):
            return data
    return None


# ---------------------------------------------------------------------------
# Measurement extraction (proven conversion math)
# ---------------------------------------------------------------------------
def extract_measurements(view_data: dict, page_data: dict) -> dict:
    """Extract real-world SF/LF from Togal view data.

    Conversion (proven on Hopewell):
      area:  stored_area * (scale_factor / dpi)^2  = SF
      perim: stored_perim * scale_factor / dpi / 12 = LF
    where scale_factor = scale_real / scale_drawing (e.g. 96 for 1/8"=1'-0")
    """
    result = {"rooms": [], "regions": [], "walls": [], "counts": {}, "totals": {}}
    if not view_data:
        return result

    scale_drawing = page_data.get("scale_drawing") or page_data.get("predicted_scale_drawing") or 0.125
    scale_real = page_data.get("scale_real") or page_data.get("predicted_scale_real") or 12
    dpi = page_data.get("url_dpi") or page_data.get("ml_input_dpi") or 150

    scale_factor = scale_real / scale_drawing
    px_to_ft = (1.0 / dpi) * scale_factor / 12.0
    px2_to_sf = px_to_ft ** 2

    result["scale_info"] = {
        "scale_drawing": scale_drawing, "scale_real": scale_real,
        "dpi": dpi, "scale_factor": scale_factor,
        "px_to_ft": px_to_ft, "px2_to_sf": px2_to_sf,
    }

    # Totals from view data
    data = view_data.get("data", {})
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, (int, float)):
                result["totals"][key] = value

    # GeoJSON features from POST /v1/view/{id}/geojson FeatureCollection
    features = view_data.get("geojson_features", [])
    for feat in features:
        props = feat.get("properties", {})
        geom_type = feat.get("geometry", {}).get("type", "")
        # Togal's area property = actual pixel area / 144 (consistent API quirk)
        # Perimeter is in correct pixel units
        togal_area = props.get("area") or 0
        pixel_area = togal_area * 144  # correct to true pixel area
        real_sf = pixel_area * px2_to_sf if pixel_area else 0
        pixel_perim = props.get("perimeter") or 0
        real_lf = pixel_perim * px_to_ft if pixel_perim else 0

        entry = {
            "pixel_area": pixel_area, "area_sf": round(real_sf, 1),
            "pixel_perimeter": pixel_perim, "perimeter_lf": round(real_lf, 1),
            "classification": None,
        }

        ml_cls = props.get("ml_classification")
        if ml_cls and isinstance(ml_cls, list) and len(ml_cls) >= 1:
            entry["classification"] = ml_cls[0]
            entry["confidence"] = ml_cls[1] if len(ml_cls) > 1 else None

        # Classify feature by geometry and properties
        feat_types = props.get("types", [])
        if geom_type == "Polygon" and pixel_area > 0:
            result["rooms"].append(entry)
        elif geom_type == "LineString" and pixel_perim > 0:
            result["walls"].append(entry)
        elif geom_type == "Point":
            ct = entry.get("classification", "unknown")
            result["counts"][ct] = result["counts"].get(ct, 0) + 1

    result["totals"]["total_room_sf"] = sum(r["area_sf"] for r in result["rooms"])
    result["totals"]["total_region_sf"] = sum(r["area_sf"] for r in result["regions"])
    result["totals"]["room_count"] = len(result["rooms"])
    result["totals"]["region_count"] = len(result["regions"])
    result["totals"]["wall_count"] = len(result["walls"])
    result["totals"]["total_room_perim_lf"] = sum(r["perimeter_lf"] for r in result["rooms"])

    return result


def _filter_page_rooms(rooms: list) -> dict:
    """Filter a single page's rooms: remove non-painting classifications and outlines."""
    import statistics

    painting_rooms = []
    excluded_rooms = []
    for r in rooms:
        cls = (r.get("classification") or "").strip()
        if cls in NON_PAINTING_CLASSIFICATIONS:
            excluded_rooms.append(r)
        else:
            painting_rooms.append(r)

    # Remove building/site outlines using statistical outlier detection.
    # Rooms that are dramatically larger than peers are building boundaries.
    outlines_removed = []
    if len(painting_rooms) >= 3:
        areas = sorted([r.get("area_sf", 0) for r in painting_rooms if r.get("area_sf", 0) > 0])
        if len(areas) >= 3:
            q1_idx = len(areas) // 4
            q3_idx = (3 * len(areas)) // 4
            q1 = areas[q1_idx]
            q3 = areas[q3_idx]
            iqr = q3 - q1
            # Upper fence: Q3 + 4*IQR (conservative — 1.5x is standard, 4x keeps large rooms)
            upper_fence = q3 + 4 * iqr
            # Also enforce minimum threshold: don't remove rooms under 500 SF as outlines
            outline_threshold = max(upper_fence, 500)

            kept = []
            for r in painting_rooms:
                if r.get("area_sf", 0) > outline_threshold:
                    outlines_removed.append(r)
                else:
                    kept.append(r)
            painting_rooms = kept

    total_sf = sum(r.get("area_sf", 0) for r in painting_rooms)
    total_perim = sum(r.get("perimeter_lf", 0) for r in painting_rooms)
    return {
        "painting_rooms": painting_rooms,
        "excluded_rooms": excluded_rooms,
        "outlines_removed": outlines_removed,
        "total_sf": total_sf,
        "total_perim_lf": total_perim,
        "room_count": len(painting_rooms),
    }


def postprocess_takeoff(all_page_results: dict) -> dict:
    """Smart post-processing: filter classifications, deduplicate pages, remove outlines.

    Strategy:
    1. Per page: remove non-painting classifications + statistical outlier outlines
    2. Group pages showing the same view (similar SF ±15%)
    3. Pick ONE primary floor plan (most rooms among largest-area pages)
    4. Report primary page measurements — other pages are subsets that double-count
    """
    # Step 1: Filter each page
    page_stats = {}
    for page_name, measurements in all_page_results.items():
        page_stats[page_name] = _filter_page_rooms(measurements.get("rooms", []))

    # Step 2: Group pages with similar total SF (±15%) — they show the same view
    sorted_pages = sorted(page_stats.items(), key=lambda x: -x[1]["total_sf"])
    groups = []
    assigned = set()
    for name, stats in sorted_pages:
        if name in assigned or stats["total_sf"] == 0:
            continue
        group = [name]
        assigned.add(name)
        for other_name, other_stats in sorted_pages:
            if other_name in assigned or other_stats["total_sf"] == 0:
                continue
            if stats["total_sf"] > 0:
                ratio = other_stats["total_sf"] / stats["total_sf"]
                if 0.85 <= ratio <= 1.15:
                    group.append(other_name)
                    assigned.add(other_name)
        groups.append(group)

    # Step 3: Pick the PRIMARY floor plan using "typical room density" heuristic.
    # Floor plans have many rooms in the 20-1000 SF range (offices, restrooms, closets).
    # Site plans have few rooms, mostly >1000 SF (building sections).
    # Detail pages have few rooms, mostly <20 SF (callout artifacts).
    # Score each page by how many rooms fall in the "typical" range.
    def _floor_plan_score(stats):
        rooms = stats.get("painting_rooms", [])
        typical_count = sum(1 for r in rooms if 20 <= r.get("area_sf", 0) <= 1000)
        # Require at least 3 typical rooms to be a candidate
        return typical_count if typical_count >= 3 else 0

    candidates = [(n, s, _floor_plan_score(s)) for n, s in page_stats.items()
                  if s["total_sf"] > 0]
    scored = [c for c in candidates if c[2] > 0]
    if scored:
        primary_name = max(scored, key=lambda x: x[2])[0]
    elif candidates:
        # Fallback: page with most rooms
        primary_name = max(candidates, key=lambda x: x[1]["room_count"])[0]
    else:
        primary_name = sorted_pages[0][0] if sorted_pages else None

    # Build group info for reporting
    page_groups_info = []
    for group in groups:
        best = max(group, key=lambda n: page_stats[n]["room_count"])
        page_groups_info.append({
            "selected": best,
            "is_primary": best == primary_name,
            "duplicates": [n for n in group if n != best],
            "sf": page_stats[best]["total_sf"],
            "rooms": page_stats[best]["room_count"],
        })

    # Step 4: Use ONLY the primary floor plan page for totals.
    # Other pages are subsets or alternate views — summing them double-counts.
    primary = page_stats.get(primary_name, {})
    all_painting_rooms = primary.get("painting_rooms", [])
    total_sf = primary.get("total_sf", 0)
    total_perim = primary.get("total_perim_lf", 0)
    total_rooms = primary.get("room_count", 0)

    return {
        "primary_page": primary_name,
        "deduplicated_sf": round(total_sf, 1),
        "deduplicated_perim_lf": round(total_perim, 1),
        "deduplicated_room_count": total_rooms,
        "best_pages": [primary_name] if primary_name else [],
        "page_groups": page_groups_info,
        "all_painting_rooms": all_painting_rooms,
        "per_page_stats": {n: {
            "total_sf": s["total_sf"],
            "room_count": s["room_count"],
            "excluded_count": len(s["excluded_rooms"]),
            "outlines_removed": len(s["outlines_removed"]),
            "outline_sf": sum(r.get("area_sf", 0) for r in s["outlines_removed"]),
        } for n, s in page_stats.items()},
    }


# ---------------------------------------------------------------------------
# Pipeline stages
# ---------------------------------------------------------------------------
class TogalPipeline:
    """Generalized Togal AI takeoff pipeline for any project."""

    def __init__(self, project_slug: str, scale: str = "1/8",
                 sheets: list[str] | None = None):
        self.slug = project_slug
        self.scale = scale
        self.sheets = sheets
        self.project_dir = find_project_dir(project_slug)
        self.project_dir.mkdir(parents=True, exist_ok=True)

        # Load auth
        self.auth = json.loads(AUTH_FILE.read_text(encoding="utf-8"))
        self.token = None

        # Load project from pipeline.json if exists
        self.project_info = self._load_project_info()

    def _load_project_info(self) -> dict:
        """Get project info from pipeline.json."""
        if PIPELINE_FILE.exists():
            pipeline = json.loads(PIPELINE_FILE.read_text(encoding="utf-8"))
            return pipeline.get("projects", {}).get(self.slug, {})
        return {}

    def _get_set_id(self) -> str | None:
        """Get Togal set_id from THIS project's info only.

        CRITICAL: never fall back to auth.current_set_id — that's a global
        last-used value and would cause one project to inherit another
        project's uploaded plans (e.g. Dutch Bros running against the
        Sally Beauty set). Each project must own its own set_id.
        """
        return self.project_info.get("togal_set_id")

    def _get_scale_values(self) -> tuple[float, float, str]:
        """Parse scale string to (drawing, real, type)."""
        if self.scale in SCALE_PRESETS:
            return SCALE_PRESETS[self.scale]
        # Try parsing "X/Y" format
        try:
            parts = self.scale.split("/")
            if len(parts) == 2:
                drawing = float(parts[0]) / float(parts[1])
                return (drawing, 12, "architectural")
        except ValueError:
            pass
        return (0.125, 12, "architectural")  # default 1/8"

    # --- Pipeline operations ---

    def authenticate(self) -> bool:
        self.token = authenticate(self.auth)
        if self.token:
            print(f"[AUTH] OK")
            return True
        print(f"[AUTH] FAILED")
        return False

    def _find_drawings(self) -> list[Path]:
        """Find plan PDFs in the project's drawings/ or bid_docs/ folder."""
        for folder_name in ["drawings", "bid_docs"]:
            folder = self.project_dir / folder_name
            if folder.exists():
                pdfs = [f for f in folder.glob("*.pdf") if not f.name.startswith("_")]
                if pdfs:
                    return pdfs
        return []

    @staticmethod
    def _plan_score(pdf: Path) -> float:
        """Score a PDF by how likely it is to be architectural plans (higher = more likely).

        Avoids uploading non-plan docs like Phase I ESA reports, ACM reports,
        geotechnical surveys, spec books, etc. even when they're the largest file.
        """
        import re as _re
        name = pdf.name.lower()
        size_mb = pdf.stat().st_size / 1024 / 1024
        score = 0.0

        # Curated/merged floor-plan file we built ourselves — always the best input.
        if any(k in name for k in ("floor-plans", "floor_plans", "arch-plans",
                                   "architectural", "merged-plans")):
            score += 100

        # Discipline by leading sheet code (a101 / ta101 / mh102 / ad101 / id500 ...).
        # ONLY architectural floor plans are valid for area takeoff. Telecom (T/TA),
        # mech (M*), elec (E*), plumb (P*), fire (F*), structural (S), civil (C),
        # general (G), interior-design (ID), demolition (AD), AV — all WRONG sheets.
        # (Old bug: "ta101" contains "a101", so a telecom sheet scored as architectural.)
        m = _re.match(r"\s*([a-z]{1,3})[\s_-]?\d", name)
        code = m.group(1) if m else ""
        NON_ARCH = {"t", "ta", "m", "mh", "mp", "md", "me", "ms", "e", "ep", "el", "ed",
                    "es", "p", "ps", "pw", "pd", "pp", "f", "fa", "fp", "fs", "s", "c",
                    "g", "ad", "av", "id"}
        if code in NON_ARCH:
            score -= 80
        elif code.startswith("a"):          # A, A1, A2, A3 ... = architectural
            score += 25

        # Plan-content keywords — credit only when NOT a wrong-discipline sheet.
        if code not in NON_ARCH:
            for kw in ("floor plan", "plan set", "permit set", "construction set",
                       "cd set", "bid set", "drawings", "prototype"):
                if kw in name:
                    score += 15

        # Reject non-plan documents outright.
        reject_kw = [
            "esa", "phase i", "phase 1", "environmental", "acm ", "acm report",
            "asbestos", "lead paint", "subsurface", "geotech", "soil",
            "exploration", "survey", "topographic", "topo", "boundary",
            "spec book", "specifications only", "specs only", "addendum",
            "rfi", "meeting minutes", "contract", "insurance", "scope letter",
            "invitation to bid", "itb", "division ", "table of contents", "cover",
        ]
        for kw in reject_kw:
            if kw in name:
                score -= 100

        # Secondary plan types — usable but NOT the primary floor plan for measure.
        for kw in ("reflected ceiling", "rcp", "demolition", "demo", "furniture",
                   "site plan", "civil", "elevation", "section", "detail",
                   "schedule", "roof plan", "pattern plan"):
            if kw in name:
                score -= 8

        score += min(size_mb, 5)   # size tiebreaker, capped
        return score

    def _pick_best_plan(self, pdfs: list[Path]) -> Path | None:
        """Pick the most likely architectural plan PDF from candidates."""
        if not pdfs:
            return None
        scored = [(self._plan_score(p), p) for p in pdfs]
        scored.sort(key=lambda t: t[0], reverse=True)
        best_score, best_pdf = scored[0]
        print(f"[UPLOAD] Plan candidates (score / name):")
        for s, p in scored:
            marker = " <-- picked" if p == best_pdf else ""
            print(f"  {s:7.1f}  {p.name}{marker}")
        if best_score < -50:
            # All candidates look like reports, not plans
            print(f"[UPLOAD] No good plan-like PDF found (best score {best_score:.1f})")
            return None
        return best_pdf

    def _auto_upload_if_needed(self, force_new_set: bool = False) -> str | None:
        """Upload plans from drawings/ if no set_id exists. Returns set_id or None.

        If force_new_set=True, creates a new set under the existing Togal project
        (used when prior set is stuck/empty).
        """
        pdfs = self._find_drawings()
        if not pdfs:
            print("[UPLOAD] No PDFs found in drawings/ or bid_docs/")
            return None

        # Score candidates to avoid uploading ESA reports, spec books, etc.
        plan_pdf = self._pick_best_plan(pdfs)
        if plan_pdf is None:
            return None
        print(f"[UPLOAD] Auto-uploading: {plan_pdf.name} ({plan_pdf.stat().st_size / 1024 / 1024:.1f} MB)")

        if force_new_set:
            # Create a new set under existing project instead of re-creating project
            result = self._upload_new_set(str(plan_pdf))
        else:
            result = self.upload_plans(str(plan_pdf))

        if result.get("error"):
            print(f"[UPLOAD] Failed: {result['error']}")
            return None

        new_set_id = result.get("togal_set_id")
        if new_set_id:
            self._save_set_id(new_set_id, result.get("togal_project_id", ""))

            # Wait for Togal to process the uploaded PDF
            print(f"[UPLOAD] Waiting for Togal to process PDF...")
            self.wait_for_upload_processing(new_set_id)

        return new_set_id

    def _upload_new_set(self, pdf_path: str) -> dict:
        """Create a new set under the existing Togal project and upload PDF to it."""
        from scripts.togal_client import TogalClient
        client = TogalClient()
        client.session_token = self.token

        project_name = self.project_info.get("name", self.slug.replace("_", " ").title())
        project = client.find_or_create_project(project_name)
        if project.get("error"):
            return {"error": f"Project lookup failed: {project['error']}"}

        # Create a new set (with timestamp to avoid name collision)
        set_name = f"Bid Documents {time.strftime('%m%d')}"
        togal_set = client.create_set(project["id"], set_name)
        if togal_set.get("error"):
            return {"error": f"Set creation failed: {togal_set['error']}"}

        result = client.upload_page(project["id"], togal_set["id"], pdf_path)
        result["togal_project_id"] = project["id"]
        result["togal_set_id"] = togal_set["id"]
        return result

    def _save_set_id(self, set_id: str, project_id: str = ""):
        """Persist set_id to THIS project's project.json only.

        Do NOT write to auth config — a global current_set_id causes
        cross-project contamination (one project inheriting another's set).
        Each project owns its own togal_set_id in project.json.
        """
        proj_file = self.project_dir / "project.json"
        if proj_file.exists():
            try:
                proj = json.loads(proj_file.read_text(encoding="utf-8"))
                proj["togal_set_id"] = set_id
                if project_id:
                    proj["togal_project_id"] = project_id
                proj_file.write_text(json.dumps(proj, indent=2, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass
        # Also update in-memory project_info so subsequent _get_set_id() calls see it
        self.project_info["togal_set_id"] = set_id
        if project_id:
            self.project_info["togal_project_id"] = project_id

    def _log_painting_pages(self, all_pages: list[dict]) -> list[dict]:
        """Filter to painting pages and log results."""
        painting_pages = filter_painting_pages(all_pages, self.sheets)
        print(f"[PAGES] {len(painting_pages)} painting sheets found (of {len(all_pages)} total)")
        for p in painting_pages:
            dpi = p.get("url_dpi") or p.get("ml_input_dpi") or "?"
            print(f"  {p['name']}  dpi={dpi}  state={p.get('state', '?')}")
        return painting_pages

    def discover_pages(self) -> list[dict]:
        """Find painting-relevant pages in the Togal set.

        Auto-uploads plans from drawings/ folder if no set exists.
        """
        set_id = self._get_set_id()

        # If we have a set, check if it has pages
        if set_id:
            all_pages = get_all_pages(self.token, set_id)
            if all_pages:
                return self._log_painting_pages(all_pages)
            else:
                # Set exists but 0 pages — PDF is still being split by Togal
                print(f"[PAGES] Set {set_id[:12]}... has 0 pages — waiting for Togal to finish splitting PDF...")
                if self.wait_for_upload_processing(set_id):
                    all_pages = get_all_pages(self.token, set_id)
                    if all_pages:
                        return self._log_painting_pages(all_pages)
                # Existing set is stuck empty — create a new set and re-upload
                print("[PAGES] Existing set appears stuck. Creating new set and re-uploading...")
                new_set_id = self._auto_upload_if_needed(force_new_set=True)
                if new_set_id:
                    all_pages = get_all_pages(self.token, new_set_id)
                    if all_pages:
                        return self._log_painting_pages(all_pages)
                return []

        # No set at all — try auto-upload from drawings/
        print("[PAGES] No set found. Checking for local drawings to upload...")
        new_set_id = self._auto_upload_if_needed()
        if not new_set_id:
            print("[ERROR] No plans to upload and no existing Togal set. Cannot proceed.")
            return []

        # Query the new set for pages
        all_pages = get_all_pages(self.token, new_set_id)
        if not all_pages:
            print("[ERROR] Upload succeeded but Togal still processing. Try --extract in a few minutes.")
            return []

        return self._log_painting_pages(all_pages)

    def set_scales(self, pages: list[dict]) -> int:
        """Set scale on all pages. Returns count of pages updated."""
        drawing, real, stype = self._get_scale_values()
        updated = 0
        for p in pages:
            if set_page_scale(self.token, p, drawing, real, stype):
                updated += 1
        print(f"[SCALE] Set {drawing}/{real} on {updated}/{len(pages)} pages")
        return updated

    def create_views_and_vectorize(self, pages: list[dict]) -> dict[str, str]:
        """Find or create views and run vectorization. Returns {page_id: view_id}."""
        # Togal auto-creates views during page processing.
        # First try batch lookup (efficient — scans recent views)
        page_ids = {p["id"] for p in pages}
        view_map = get_views_for_pages(self.token, page_ids)

        if view_map:
            print(f"[VIEWS] Found {len(view_map)} existing views")
        else:
            # No auto-created views — create them manually
            set_id = self._get_set_id()
            view_name = get_set_view_name(self.token, set_id) if set_id else "Paint Takeoff"
            for p in pages:
                page_id = p["id"]
                view_id = ensure_view(self.token, page_id, view_name)
                if view_id:
                    view_map[page_id] = view_id

        # Run vectorization on all views
        for p in pages:
            page_id = p["id"]
            name = p.get("name", "?")
            view_id = view_map.get(page_id)
            if not view_id:
                print(f"  {name}: SKIP (no view)")
                continue

            if run_vectorization(self.token, view_id):
                print(f"  {name}: vectorization started (view={view_id[:8]}...)")
            else:
                print(f"  {name}: vectorization FAILED")

        # Save view map
        (self.project_dir / "view_ids.json").write_text(
            json.dumps(view_map, indent=2))

        return view_map

    def wait_for_upload_processing(self, set_id: str, max_wait: int = 300, interval: int = 15) -> bool:
        """Wait for Togal to finish splitting an uploaded multi-page PDF.

        Polls get_all_pages() until pages appear and are no longer PAGE_PENDING.
        """
        print(f"[WAIT] Polling for uploaded PDF processing (up to {max_wait}s)...")
        start = time.time()
        last_count = 0
        while time.time() - start < max_wait:
            pages = get_all_pages(self.token, set_id)
            if pages:
                pending = [p for p in pages if p.get("state") == "PAGE_PENDING"]
                ready = len(pages) - len(pending)
                if ready != last_count:
                    print(f"  {ready}/{len(pages)} pages ready...")
                    last_count = ready
                if not pending:
                    print(f"[WAIT] All {len(pages)} pages ready.")
                    return True
                if ready > 0 and len(pending) <= 1:
                    # Almost done — good enough to proceed
                    print(f"[WAIT] {ready}/{len(pages)} pages ready, proceeding.")
                    return True
            time.sleep(interval)
        # After timeout, check one more time
        pages = get_all_pages(self.token, set_id)
        if pages:
            print(f"[WAIT] Timeout. {len(pages)} pages exist, some may still be processing.")
            return True
        print("[WAIT] Timeout. No pages appeared.")
        return False

    def wait_for_processing(self, pages: list[dict], max_wait: int = 300, interval: int = 10):
        """Wait for Togal vectorization to complete by polling page states."""
        print(f"[WAIT] Polling vectorization status (up to {max_wait}s)...")
        start = time.time()
        while time.time() - start < max_wait:
            all_ready = True
            for p in pages:
                status = get_page_status(self.token, p["id"])
                state = status.get("state", "")
                if state in ("PAGE_PENDING", "PROCESSING", "VECTORIZING"):
                    all_ready = False
                    break
            if all_ready:
                elapsed = int(time.time() - start)
                print(f"[WAIT] All pages processed ({elapsed}s)")
                return True
            time.sleep(interval)
        elapsed = int(time.time() - start)
        print(f"[WAIT] Timeout after {elapsed}s — some pages may still be processing. Extracting what's available.")
        return False

    def extract(self, pages: list[dict], view_map: dict[str, str]) -> dict:
        """Extract measurements from all processed views."""
        all_results = {}

        for p in pages:
            page_id = p["id"]
            view_id = view_map.get(page_id)
            if not view_id:
                continue

            name = p.get("name", "?")
            page_data = get_page_scale_info(self.token, page_id) or p
            view_data = get_view_measurements(self.token, view_id)

            if view_data:
                measurements = extract_measurements(view_data, page_data)
                all_results[name] = measurements
                totals = measurements.get("totals", {})
                print(f"  {name}: {totals.get('room_count', 0)} rooms, "
                      f"{totals.get('total_room_sf', 0):.0f} SF, "
                      f"{totals.get('wall_count', 0)} walls")

        return all_results

    def try_classifications(self) -> dict | None:
        """Check if user-drawn classifications exist for this set.

        Classifications are the authoritative takeoff data — properly named rooms
        with accurate SF/LF from user-drawn boundaries. If they exist, use them
        instead of raw GeoJSON feature extraction.
        """
        org_id = self.auth.get("organization_id")
        set_id = self._get_set_id()
        if not org_id or not set_id:
            return None

        print("[CLS] Checking for user-drawn classifications...")
        cls_list = get_classifications(self.token, org_id, set_id)
        if not cls_list:
            print("[CLS] No classifications found — using AI-detected features instead")
            return None

        print(f"[CLS] Found {len(cls_list)} classifications (user-drawn takeoff data)")
        result = extract_from_classifications(cls_list)
        totals = result["totals"]
        print(f"  Rooms: {totals['room_count']}, Floor SF: {totals['total_room_sf']:.0f}, "
              f"Walls: {totals['wall_count']}, Wall SF: {totals.get('total_wall_sf', 0):.0f}")
        for r in result["rooms"]:
            print(f"    {r['name']}: {r['area_sf']:.0f} SF")
        for w in result["walls"]:
            if w["area_sf"] > 0:
                print(f"    {w['name']}: {w['area_sf']:.0f} SF (wall)")
            elif w["perimeter_lf"] > 0:
                print(f"    {w['name']}: {w['perimeter_lf']:.0f} LF (linear)")

        return result

    def save_results(self, results: dict, classification_data: dict = None) -> Path:
        """Save takeoff measurements to project directory.

        If classification_data is provided (from try_classifications), it takes
        priority as the authoritative takeoff. Raw GeoJSON results are kept as
        supplementary data.
        """
        # Smart post-processing of raw GeoJSON results
        processed = postprocess_takeoff(results)

        output = {
            "project": self.slug,
            "timestamp": time.strftime("%Y-%m-%d %H:%M"),
            "method": "togal_ai_pipeline",
            "scale": self.scale,
            "pages_measured": len(results),
        }

        # If we have user-drawn classifications, they're the authoritative data
        if classification_data:
            output["source"] = "classifications"
            output["takeoff"] = {
                "floor_sf": classification_data["totals"]["total_room_sf"],
                "wall_sf": classification_data["totals"].get("total_wall_sf", 0),
                "wall_lf": classification_data["totals"].get("total_wall_lf", 0),
                "room_count": classification_data["totals"]["room_count"],
                "wall_count": classification_data["totals"]["wall_count"],
                "rooms": classification_data["rooms"],
                "walls": classification_data["walls"],
                "counts": classification_data["counts"],
            }
            output["raw_geojson"] = results  # Keep raw data as reference
        else:
            output["source"] = "ai_detected_geojson"
            # Raw grand totals (before filtering)
            raw_grand = {"rooms": 0, "room_sf": 0, "walls": 0, "room_perim_lf": 0}
            for m in results.values():
                t = m.get("totals", {})
                raw_grand["rooms"] += t.get("room_count", 0)
                raw_grand["room_sf"] += t.get("total_room_sf", 0)
                raw_grand["walls"] += t.get("wall_count", 0)
                raw_grand["room_perim_lf"] += t.get("total_room_perim_lf", 0)
            output["raw_grand_totals"] = raw_grand

            # Clean totals (after filtering + dedup)
            output["takeoff"] = {
                "floor_sf": processed["deduplicated_sf"],
                "perimeter_lf": processed["deduplicated_perim_lf"],
                "room_count": processed["deduplicated_room_count"],
                "primary_page": processed.get("primary_page"),
                "best_pages_used": len(processed["best_pages"]),
                "total_pages": len(results),
                "page_groups": processed["page_groups"],
                "rooms": processed["all_painting_rooms"],
            }
            output["per_page_stats"] = processed["per_page_stats"]
            output["raw_measurements"] = results

        out_path = self.project_dir / "togal_takeoff.json"
        out_path.write_text(json.dumps(output, indent=2))
        print(f"\n[SAVED] {out_path}")

        if classification_data:
            t = classification_data["totals"]
            print(f"  Source: User-drawn classifications (authoritative)")
            print(f"  Rooms: {t['room_count']}, Floor SF: {t['total_room_sf']:.0f}, "
                  f"Walls: {t['wall_count']}, Wall SF: {t.get('total_wall_sf', 0):.0f}")
        else:
            raw_grand = output.get("raw_grand_totals", {})
            print(f"  Source: AI-detected features (no user classifications found)")
            print(f"  Raw: {raw_grand['rooms']} rooms, {raw_grand['room_sf']:.0f} SF across {len(results)} pages")
            primary = processed.get("primary_page", "?")
            print(f"  Primary floor plan: {primary[-25:]}")
            print(f"  Filtered: {processed['deduplicated_room_count']} rooms, "
                  f"{processed['deduplicated_sf']:.0f} SF, "
                  f"{processed['deduplicated_perim_lf']:.0f} LF")
            pps = processed.get("per_page_stats", {}).get(primary, {})
            if pps.get("outlines_removed"):
                print(f"  Outlines removed: {pps['outlines_removed']} polygons "
                      f"({pps.get('outline_sf', 0):.0f} SF)")
            total_dupes = sum(len(g["duplicates"]) for g in processed["page_groups"])
            if total_dupes:
                print(f"  Pages deduplicated: {total_dupes} duplicate views removed")

        return out_path

    # --- High-level operations ---

    def run_full(self, max_wait: int = 300) -> dict:
        """Full pipeline: auth → pages → scale → views → poll → extract → save.

        Uses smart polling instead of fixed sleep. Auto-uploads plans if needed.
        """
        try:
            if not self.authenticate():
                return {"error": "Authentication failed"}

            pages = self.discover_pages()
            if not pages:
                return {"error": "No painting pages found. Check that drawings/ or bid_docs/ has PDFs."}

            self.set_scales(pages)
            view_map = self.create_views_and_vectorize(pages)

            if not view_map:
                return {"error": "No views created — vectorization may have failed for all pages."}

            # CRITICAL: trigger the room/wall DETECTION AI. The scale endpoint
            # alone never detects geometry — every extraction returned 0 rooms
            # until POST /v1/page-processing/run was added (WSSU incident).
            for _pid, _vid in view_map.items():
                try:
                    r = requests.post(f"{BASE_URL}/v1/page-processing/run",
                                      headers=get_headers(self.token),
                                      json={"page_id": _pid, "view_id": _vid,
                                            "workflow_type": "full"}, timeout=120)
                    print(f"  [detect] {_pid[:8]}: AI takeoff "
                          f"{'ok' if r.status_code == 200 else 'HTTP ' + str(r.status_code)}")
                except Exception as _e:
                    print(f"  [detect] {_pid[:8]}: FAILED {_e}")

            self.wait_for_processing(pages, max_wait=max_wait)

            # Try user-drawn classifications first (authoritative)
            cls_data = self.try_classifications()

            results = self.extract(pages, view_map)

            if not results and not cls_data:
                return {"error": "Extraction returned no measurements. Pages may still be processing — try --extract in a few minutes."}

            out_path = self.save_results(results or {}, classification_data=cls_data)
            return {"status": "complete", "output": str(out_path), "pages": len(results),
                    "source": "classifications" if cls_data else "ai_detected"}
        except Exception as e:
            return {"error": f"Pipeline error: {str(e)}"}

    def run_extract_only(self) -> dict:
        """Extract measurements from existing views (skip vectorization)."""
        if not self.authenticate():
            return {"error": "Authentication failed"}

        pages = self.discover_pages()
        if not pages:
            return {"error": "No painting pages found"}

        # Load existing view map
        view_file = self.project_dir / "view_ids.json"
        if view_file.exists():
            view_map = json.loads(view_file.read_text())
        else:
            # Build from existing views
            view_map = {}
            for p in pages:
                existing = get_existing_views(self.token, p["id"])
                if existing:
                    view_map[p["id"]] = existing[0]["id"]

        if not view_map:
            return {"error": "No views found. Run full pipeline first."}

        # Try user-drawn classifications first (authoritative)
        cls_data = self.try_classifications()

        results = self.extract(pages, view_map)
        out_path = self.save_results(results, classification_data=cls_data)
        return {"status": "complete", "output": str(out_path), "pages": len(results),
                "source": "classifications" if cls_data else "ai_detected"}

    def get_status(self) -> dict:
        """Check current state of the takeoff."""
        status = {"project": self.slug, "set_id": self._get_set_id()}

        takeoff_file = self.project_dir / "togal_takeoff.json"
        if takeoff_file.exists():
            data = json.loads(takeoff_file.read_text())
            status["takeoff_complete"] = True
            status["pages_measured"] = data.get("pages_measured", 0)
            status["grand_totals"] = data.get("grand_totals", {})
            status["timestamp"] = data.get("timestamp", "?")
        else:
            status["takeoff_complete"] = False

        view_file = self.project_dir / "view_ids.json"
        status["views_created"] = view_file.exists()

        return status

    def upload_plans(self, pdf_path: str) -> dict:
        """Upload a PDF to Togal and create a new set."""
        if not self.authenticate():
            return {"error": "Authentication failed"}

        from scripts.togal_client import TogalClient
        client = TogalClient()
        client.session_token = self.token

        # Find or create project
        project_name = self.project_info.get("name", self.slug.replace("_", " ").title())
        project = client.find_or_create_project(project_name)
        if project.get("error"):
            return {"error": f"Project creation failed: {project['error']}"}

        # Create set — unique name so a re-run never collides with (or reuses) a
        # stale set from a prior failed upload (which may hold the wrong sheet).
        set_name = f"Bid Documents {time.strftime('%m%d-%H%M')}"
        togal_set = client.create_set(project["id"], set_name)
        if togal_set.get("error"):
            return {"error": f"Set creation failed: {togal_set['error']}"}

        # Upload
        result = client.upload_page(project["id"], togal_set["id"], pdf_path)
        result["togal_project_id"] = project["id"]
        result["togal_set_id"] = togal_set["id"]

        # Update pipeline.json with set_id
        if PIPELINE_FILE.exists():
            pipeline = json.loads(PIPELINE_FILE.read_text(encoding="utf-8"))
            proj = pipeline.get("projects", {}).get(self.slug)
            if proj:
                proj["togal_set_id"] = togal_set["id"]
                PIPELINE_FILE.write_text(json.dumps(pipeline, indent=2, ensure_ascii=False),
                                         encoding="utf-8")

        return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Togal AI Takeoff Pipeline")
    parser.add_argument("--project", "-p", required=True, help="Project slug")
    parser.add_argument("--scale", "-s", default="1/8",
                        help="Scale (1/8, 1/4, 3/16, etc.)")
    parser.add_argument("--sheets", help="Comma-separated sheet prefixes (A201,A202)")
    parser.add_argument("--status", action="store_true", help="Check takeoff status")
    parser.add_argument("--extract", action="store_true",
                        help="Extract from existing views (skip vectorization)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would happen without calling API")
    parser.add_argument("--upload", metavar="PDF", help="Upload plans PDF to Togal")
    parser.add_argument("--wait", type=int, default=300,
                        help="Seconds to wait for vectorization (default 60)")
    args = parser.parse_args()

    sheets = args.sheets.split(",") if args.sheets else None
    pipeline = TogalPipeline(args.project, scale=args.scale, sheets=sheets)

    if args.status:
        status = pipeline.get_status()
        print(json.dumps(status, indent=2))
        return

    if args.dry_run:
        print(f"DRY RUN — Project: {args.project}")
        print(f"  Scale: {args.scale} -> {pipeline._get_scale_values()}")
        print(f"  Set ID: {pipeline._get_set_id() or 'NOT SET'}")
        print(f"  Sheets filter: {sheets or 'auto-detect'}")
        print(f"  Output dir: {pipeline.project_dir}")

        if pipeline.authenticate():
            pages = pipeline.discover_pages()
            print(f"\n  Would process {len(pages)} pages")
        return

    if args.upload:
        result = pipeline.upload_plans(args.upload)
        print(json.dumps(result, indent=2))
        return

    if args.extract:
        result = pipeline.run_extract_only()
        print(json.dumps(result, indent=2))
        return

    # Full pipeline
    result = pipeline.run_full(max_wait=args.wait)
    print(f"\n__RESULT__:{json.dumps(result)}")


if __name__ == "__main__":
    main()
