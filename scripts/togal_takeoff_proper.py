"""
Togal AI Takeoff — Proper workflow:
1. Authenticate
2. Get page IDs for painting-relevant sheets
3. Set scale on each page (using Togal's predicted scale)
4. Create views
5. Run vectorization (scale endpoint) to get real-world measurements
6. Extract scaled room/wall/region measurements
7. Save results

No browser automation needed — pure API.
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import requests

BASE_URL = "https://app.togal.ai/api"
AUTH_PATH = Path(r"C:\Agent Carol\data\config\togal_auth.json")
OUTPUT_DIR = Path(r"C:\Agent Carol\data\projects\hopewell_elementary_phase2_gym")

# Painting-relevant sheets
PAINTING_SHEETS = [
    "A201",  # Floor Plan Level 1
    "A202",  # Floor Plan Level 2
    "A301",  # RCP Level 1
    "A302",  # RCP Level 2
    "A901",  # Finish Plan Level 1
    "A902",  # Finish Plan Level 2
    "A912",  # Interior Elevations
]

# Scale: 1/8" = 1'-0" for floor plans/RCPs, 1/4" = 1'-0" for elevations
SCALE_MAP = {
    "A201": (0.125, 12, "architectural"),
    "A202": (0.125, 12, "architectural"),
    "A301": (0.125, 12, "architectural"),
    "A302": (0.125, 12, "architectural"),
    "A901": (0.125, 12, "architectural"),
    "A902": (0.125, 12, "architectural"),
    "A912": (0.125, 12, "architectural"),  # 1/8 per extracted data
}


def get_headers(token):
    return {
        "Content-Type": "application/json",
        "session": token,
    }


def authenticate(auth):
    """Get a fresh session token."""
    # Try existing token first
    token = auth.get("session_token")
    if token:
        r = requests.get(f"{BASE_URL}/v1/user", headers=get_headers(token), timeout=15)
        if r.status_code == 200 and "id" in r.json():
            print("[AUTH] Existing session valid")
            return token

    # Fresh login
    r = requests.post(f"{BASE_URL}/v1/session", json={
        "email": auth["email"],
        "password": auth["password"],
    }, headers={"Content-Type": "application/json"}, timeout=15)
    if r.status_code == 200:
        data = r.json()
        token = data.get("id")
        if token:
            auth["session_token"] = token
            AUTH_PATH.write_text(json.dumps(auth, indent=2))
            print("[AUTH] New session created")
            return token
    print(f"[AUTH FAILED] {r.status_code}: {r.text[:200]}")
    return None


def get_painting_pages(token, set_id):
    """Get page records for painting-relevant sheets."""
    r = requests.get(f"{BASE_URL}/v1/page", headers=get_headers(token), params={
        "$where": json.dumps({"set_id": set_id}),
        "$limit": "100",
    }, timeout=30)
    if r.status_code != 200:
        print(f"[ERROR] List pages: {r.status_code} {r.text[:200]}")
        return []

    all_pages = r.json().get("rows", [])
    target = []
    for p in all_pages:
        name = p.get("name", "")
        for prefix in PAINTING_SHEETS:
            if name.upper().startswith(prefix):
                target.append(p)
                break
    return target


def set_page_scale(token, page):
    """Set scale on a page using PUT /v1/page/{id}."""
    page_id = page["id"]
    name = page.get("name", "")

    # Check if scale already set
    if page.get("scale_drawing") and page.get("scale_real"):
        print(f"  Scale already set: {page['scale_drawing']}/{page['scale_real']}")
        return True

    # Find matching scale
    drawing, real, scale_type = (0.125, 12, "architectural")  # default
    for prefix, vals in SCALE_MAP.items():
        if name.upper().startswith(prefix):
            drawing, real, scale_type = vals
            break

    payload = {
        "scale_drawing": drawing,
        "scale_real": real,
        "scale_type": scale_type,
        "scale_units": "imperial",
    }

    r = requests.put(f"{BASE_URL}/v1/page/{page_id}",
                     headers=get_headers(token),
                     json=payload, timeout=30)

    if r.status_code == 200:
        print(f"  Scale set: {drawing}/{real} ({scale_type})")
        return True
    else:
        print(f"  Scale FAILED ({r.status_code}): {r.text[:200]}")
        # Try v2
        r2 = requests.put(f"{BASE_URL}/v2/page/{page_id}",
                          headers=get_headers(token),
                          json=payload, timeout=30)
        if r2.status_code == 200:
            print(f"  Scale set via v2: {drawing}/{real}")
            return True
        print(f"  v2 also failed ({r2.status_code}): {r2.text[:200]}")
        return False


def create_view(token, page_id, name):
    """Create a view for a page."""
    r = requests.post(f"{BASE_URL}/v1/view", headers=get_headers(token), json={
        "name": name,
        "page_id": page_id,
    }, timeout=30)

    if r.status_code == 200:
        return r.json()
    print(f"  View create failed ({r.status_code}): {r.text[:200]}")
    return None


def get_existing_views(token, page_id):
    """Check if views already exist for a page."""
    r = requests.get(f"{BASE_URL}/v1/view/get-views", headers=get_headers(token), params={
        "$where": json.dumps({"page_id": page_id}),
        "$limit": "10",
    }, timeout=30)
    if r.status_code == 200:
        data = r.json()
        rows = data.get("rows", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
        return rows
    return []


def run_vectorization(token, view_id):
    """POST /v1/togal/scale/{view} — run vectorization with all measurement filters."""
    filters = {
        "gross_area": True,
        "gross_area_internal": True,
        "net_area": True,
        "walls_area": True,
        "count": True,
        "walls_centerline": True,
        "footprint": True,
        "doors_area": True,
        "doors_centerline": True,
        "walls_perimeter": True,
    }

    r = requests.post(f"{BASE_URL}/v1/togal/scale/{view_id}",
                      headers=get_headers(token),
                      json=filters, timeout=60)

    if r.status_code == 200:
        print(f"  Vectorization started")
        return True
    else:
        print(f"  Vectorization failed ({r.status_code}): {r.text[:300]}")
        # Try v2
        r2 = requests.post(f"{BASE_URL}/v1/togal/scale/{view_id}/v2",
                           headers=get_headers(token),
                           json=filters, timeout=60)
        if r2.status_code == 200:
            print(f"  Vectorization started (v2)")
            return True
        print(f"  v2 also failed ({r2.status_code}): {r2.text[:300]}")
        return False


def get_view_measurements(token, view_id):
    """Get the full view data with measurements."""
    r = requests.get(f"{BASE_URL}/v1/view/{view_id}", headers=get_headers(token), params={
        "$attributes": "data,geojson,metadata,geojson_rooms,geojson_walls,geojson_regions,geojson_counts,geojson_wall_polygons",
    }, timeout=30)
    if r.status_code == 200:
        return r.json()
    print(f"  View get failed ({r.status_code}): {r.text[:200]}")
    return None


def get_page_with_scale(token, page_id):
    """Get page data including scale and DPI."""
    r = requests.get(f"{BASE_URL}/v1/page/{page_id}", headers=get_headers(token), params={
        "$attributes": "scale_drawing,scale_real,scale_type,scale_units,url_dpi,ml_input_dpi,predicted_scale_drawing,predicted_scale_real",
    }, timeout=30)
    if r.status_code == 200:
        return r.json()
    return None


def extract_measurements(view_data, page_data):
    """Extract real-world measurements from view data."""
    result = {
        "rooms": [],
        "regions": [],
        "walls": [],
        "counts": {},
        "totals": {},
    }

    if not view_data:
        return result

    # Get scale info
    scale_drawing = page_data.get("scale_drawing") or page_data.get("predicted_scale_drawing") or 0.125
    scale_real = page_data.get("scale_real") or page_data.get("predicted_scale_real") or 12
    dpi = page_data.get("url_dpi") or page_data.get("ml_input_dpi") or 150  # default 150 DPI

    # Scale factor: how many real-world inches per drawing inch
    scale_factor = scale_real / scale_drawing  # e.g. 12/0.125 = 96 (1/8" = 1'-0")
    # Pixels to real feet: 1 pixel = (1/dpi) inches on paper * scale_factor inches real / 12 inches/foot
    px_to_ft = (1.0 / dpi) * scale_factor / 12.0
    px2_to_sf = px_to_ft ** 2  # pixel^2 to square feet

    result["scale_info"] = {
        "scale_drawing": scale_drawing,
        "scale_real": scale_real,
        "dpi": dpi,
        "px_to_ft": px_to_ft,
        "px2_to_sf": px2_to_sf,
        "scale_factor": scale_factor,
    }

    # Check if view has pre-calculated measurements (from vectorization)
    data = view_data.get("data", {})
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, (int, float)):
                result["totals"][key] = value

    # Extract from geojson fields
    for field_name in ["geojson_rooms", "geojson_regions", "geojson_walls", "geojson_counts", "geojson_wall_polygons"]:
        gj = view_data.get(field_name)
        if not gj or not isinstance(gj, dict):
            continue
        features = gj.get("features", [])

        for feat in features:
            props = feat.get("properties", {})
            pixel_area = props.get("area", 0)
            real_area_sf = pixel_area * px2_to_sf if pixel_area else 0

            entry = {
                "id": props.get("id"),
                "pixel_area": pixel_area,
                "area_sf": round(real_area_sf, 1),
                "classification": None,
                "perimeter": props.get("perimeter"),
            }

            # ML classification
            ml_cls = props.get("ml_classification")
            if ml_cls and isinstance(ml_cls, list) and len(ml_cls) >= 1:
                entry["classification"] = ml_cls[0]
                entry["confidence"] = ml_cls[1] if len(ml_cls) > 1 else None

            # Room type
            types = props.get("types", "")

            if field_name == "geojson_rooms":
                result["rooms"].append(entry)
            elif field_name == "geojson_regions":
                result["regions"].append(entry)
            elif field_name in ("geojson_walls", "geojson_wall_polygons"):
                result["walls"].append(entry)
            elif field_name == "geojson_counts":
                count_type = entry.get("classification", "unknown")
                result["counts"][count_type] = result["counts"].get(count_type, 0) + 1

    # Calculate totals
    result["totals"]["total_room_sf"] = sum(r["area_sf"] for r in result["rooms"])
    result["totals"]["total_region_sf"] = sum(r["area_sf"] for r in result["regions"])
    result["totals"]["room_count"] = len(result["rooms"])
    result["totals"]["region_count"] = len(result["regions"])
    result["totals"]["wall_count"] = len(result["walls"])

    return result


def main():
    auth = json.loads(AUTH_PATH.read_text())
    set_id = auth.get("current_set_id", "375495bb-59a9-4439-bc42-1874125712f6")

    # Step 1: Authenticate
    token = authenticate(auth)
    if not token:
        return

    # Step 2: Get painting pages
    print("\n[1] Getting painting-relevant pages...")
    pages = get_painting_pages(token, set_id)
    print(f"    Found {len(pages)} painting sheets")
    for p in pages:
        scale_d = p.get("scale_drawing")
        scale_r = p.get("scale_real")
        dpi = p.get("url_dpi") or p.get("ml_input_dpi")
        print(f"    {p['name']}  scale={scale_d}/{scale_r}  dpi={dpi}  state={p.get('state')}")

    if not pages:
        print("[ERROR] No painting pages found")
        return

    # Step 3: Set scale on each page
    print("\n[2] Setting scale on pages...")
    for p in pages:
        name = p.get("name", "")
        print(f"  {name}:")
        set_page_scale(token, p)

    # Re-fetch pages to confirm scale
    print("\n[3] Verifying scale...")
    pages = get_painting_pages(token, set_id)
    for p in pages:
        print(f"    {p['name']}: scale={p.get('scale_drawing')}/{p.get('scale_real')} dpi={p.get('url_dpi')}")

    # Step 4: Create views and run vectorization
    print("\n[4] Creating views and running vectorization...")
    view_map = {}  # page_id -> view_id
    for p in pages:
        page_id = p["id"]
        name = p.get("name", "unknown")
        print(f"\n  {name}:")

        # Check for existing views
        existing = get_existing_views(token, page_id)
        if existing:
            view_id = existing[0].get("id")
            print(f"    Existing view: {view_id}")
            view_map[page_id] = view_id
        else:
            view = create_view(token, page_id, f"Paint Takeoff - {name}")
            if view:
                view_id = view.get("id")
                print(f"    New view: {view_id}")
                view_map[page_id] = view_id
            else:
                print(f"    SKIP (no view)")
                continue

        # Run vectorization
        if page_id in view_map:
            run_vectorization(token, view_map[page_id])

    # Step 5: Wait for processing
    if view_map:
        print("\n[5] Waiting 60s for vectorization to complete...")
        time.sleep(60)

        # Step 6: Extract measurements
        print("\n[6] Extracting measurements...")
        all_results = {}

        for p in pages:
            page_id = p["id"]
            name = p.get("name", "")
            view_id = view_map.get(page_id)
            if not view_id:
                continue

            print(f"\n  {name}:")

            # Get page data with scale/DPI
            page_data = get_page_with_scale(token, page_id) or p

            # Get view with all measurements
            view_data = get_view_measurements(token, view_id)

            if view_data:
                measurements = extract_measurements(view_data, page_data)
                all_results[name] = measurements

                # Print summary
                totals = measurements.get("totals", {})
                print(f"    Rooms: {totals.get('room_count', 0)}, Total SF: {totals.get('total_room_sf', 0):.0f}")
                print(f"    Regions: {totals.get('region_count', 0)}, Total SF: {totals.get('total_region_sf', 0):.0f}")
                print(f"    Walls: {totals.get('wall_count', 0)}")

                # Show region classifications
                regions = measurements.get("regions", [])
                by_class = {}
                for r in regions:
                    cls = r.get("classification", "Unknown")
                    if cls not in by_class:
                        by_class[cls] = {"count": 0, "sf": 0}
                    by_class[cls]["count"] += 1
                    by_class[cls]["sf"] += r["area_sf"]
                for cls, data in sorted(by_class.items(), key=lambda x: -x[1]["sf"]):
                    print(f"      {cls}: {data['count']}x = {data['sf']:.0f} SF")
            else:
                print(f"    No data")

        # Step 7: Save results
        output_file = OUTPUT_DIR / "togal_takeoff_measurements.json"
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(json.dumps({
            "project": "Hopewell Elementary Phase 2 Gym",
            "timestamp": time.strftime("%Y-%m-%d %H:%M"),
            "method": "togal_ai_takeoff",
            "pages_measured": len(all_results),
            "measurements": all_results,
        }, indent=2))
        print(f"\n[7] Saved: {output_file}")

        # Print grand totals
        print("\n=== GRAND TOTALS ===")
        grand_room_sf = 0
        grand_region_sf = 0
        grand_rooms = 0
        grand_walls = 0
        for name, m in all_results.items():
            t = m.get("totals", {})
            grand_room_sf += t.get("total_room_sf", 0)
            grand_region_sf += t.get("total_region_sf", 0)
            grand_rooms += t.get("room_count", 0)
            grand_walls += t.get("wall_count", 0)
        print(f"  Total rooms detected: {grand_rooms}")
        print(f"  Total room floor SF: {grand_room_sf:.0f}")
        print(f"  Total region SF: {grand_region_sf:.0f}")
        print(f"  Total walls detected: {grand_walls}")

    print("\n[DONE]")


if __name__ == "__main__":
    main()
