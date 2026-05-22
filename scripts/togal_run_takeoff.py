"""
Run Togal AI takeoff on Hopewell architectural pages for painting estimate.
Focus on floor plans, RCPs, finish plans, interior elevations, door schedule.
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from togal_client import TogalClient

OUTPUT_DIR = Path(r"C:\Agent Carol\data\projects\hopewell_elementary_phase2_gym")

# Key painting sheets to run takeoff on
PAINTING_SHEETS = [
    "A201",  # Floor Plan Level 1
    "A202",  # Floor Plan Level 2
    "A301",  # RCP Level 1
    "A302",  # RCP Level 2
    "A400",  # Exterior Elevations
    "A600",  # Enlarged Plans
    "A800",  # Door Schedule
    "A901",  # Finish Plan Level 1
    "A902",  # Finish Plan Level 2
    "A910",  # Interior Elevations
    "A911",  # Interior Elevations
    "A912",  # Interior Elevations
    "A930",  # Finish Schedule
]

def main():
    set_id = "375495bb-59a9-4439-bc42-1874125712f6"

    client = TogalClient()
    client.authenticate()
    print("[1] Authenticated")

    # Get all pages
    import requests as req
    url = f"{client.base_url}/v1/page"
    r = req.get(url, headers=client._headers(), params={
        "$where": json.dumps({"set_id": set_id}),
        "$limit": "100",
    }, timeout=30)
    data = r.json()
    pages = data.get("rows", [])
    print(f"[2] Total pages: {len(pages)}")

    # Filter to painting-relevant sheets
    target_pages = []
    for p in pages:
        name = p.get("name", "")
        for prefix in PAINTING_SHEETS:
            if name.startswith(prefix):
                target_pages.append(p)
                break

    print(f"\n[3] Target painting sheets: {len(target_pages)}")
    for p in target_pages:
        print(f"    {p['name']}")

    # Run AI takeoff on each
    print(f"\n[4] Running AI takeoff...")
    results = []
    errors = []

    for i, p in enumerate(target_pages):
        page_id = p["id"]
        name = p.get("name", "unknown")
        print(f"\n  [{i+1}/{len(target_pages)}] {name}...")

        # Create view
        try:
            view_url = f"{client.base_url}/v1/view"
            vr = req.post(view_url, headers=client._headers(), json={
                "name": f"Paint Takeoff - {name}",
                "page_id": page_id,
            }, timeout=30)

            if vr.status_code == 200:
                view_data = vr.json()
                view_id = view_data.get("id")
                print(f"    View: {view_id}")
            else:
                print(f"    View failed ({vr.status_code}): {vr.text[:200]}")
                view_id = None
        except Exception as e:
            print(f"    View error: {e}")
            view_id = None

        # Run AI takeoff
        try:
            takeoff_url = f"{client.base_url}/v1/page-processing/run"
            payload = {"page_id": page_id, "workflow_type": "paint"}
            if view_id:
                payload["view_id"] = view_id

            tr = req.post(takeoff_url, headers=client._headers(), json=payload, timeout=60)
            print(f"    Takeoff ({tr.status_code}): {tr.text[:200]}")

            if tr.status_code == 200:
                results.append({
                    "page_name": name,
                    "page_id": page_id,
                    "view_id": view_id,
                    "response": tr.json(),
                })
            else:
                errors.append({"page": name, "error": f"{tr.status_code}: {tr.text[:200]}"})
        except Exception as e:
            print(f"    Takeoff error: {e}")
            errors.append({"page": name, "error": str(e)})

        time.sleep(1)

    print(f"\n[5] Takeoff results: {len(results)} ok, {len(errors)} errors")

    if results:
        print("\n[6] Waiting 90s for AI processing...")
        time.sleep(90)

        print("\n[7] Retrieving measurements...")
        all_measurements = []

        for r_item in results:
            name = r_item["page_name"]
            view_id = r_item.get("view_id")
            page_id = r_item["page_id"]

            if not view_id:
                continue

            print(f"\n  {name}:")

            # Get view with geojson
            try:
                vr = req.get(f"{client.base_url}/v1/view/{view_id}",
                    headers=client._headers(),
                    params={"$attributes": "data,geojson,metadata"},
                    timeout=30)
                view_result = vr.json() if vr.status_code == 200 else {"error": vr.text[:200]}

                # Get geojson
                gjr = req.get(f"{client.base_url}/v1/view/{view_id}/geojson",
                    headers=client._headers(), timeout=30)
                gj = gjr.json() if gjr.status_code == 200 else {}

                features = gj.get("features", [])
                print(f"    Features: {len(features)}")

                for feat in features[:10]:
                    props = feat.get("properties", {})
                    area = props.get("area_sf") or props.get("area")
                    length = props.get("length_lf") or props.get("length")
                    cls = props.get("classification") or props.get("name") or props.get("type", "?")
                    measurement = area or length or "N/A"
                    print(f"      {cls}: {measurement}")

                all_measurements.append({
                    "page": name,
                    "page_id": page_id,
                    "view_id": view_id,
                    "features": features,
                    "view_data": view_result if isinstance(view_result, dict) else {},
                })

                # Check AI status
                air = req.get(f"{client.base_url}/v1/ai_action_event",
                    headers=client._headers(),
                    params={
                        "$where": json.dumps({"page_id": page_id}),
                        "$order": json.dumps([["created_at", "DESC"]]),
                        "$limit": "3",
                    }, timeout=30)
                if air.status_code == 200:
                    events = air.json().get("rows", [])
                    for ev in events:
                        print(f"    AI: {ev.get('status', '?')} - {ev.get('action_type', '?')}")

            except Exception as e:
                print(f"    Error: {e}")

        # Save results
        output_file = OUTPUT_DIR / "togal_measurements.json"
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(json.dumps({
            "project": "Hopewell Elementary Phase 2 Gym",
            "timestamp": time.strftime("%Y-%m-%d %H:%M"),
            "pages_processed": len(results),
            "measurements": all_measurements,
            "errors": errors,
        }, indent=2))
        print(f"\n[8] Saved: {output_file}")
    else:
        print("\n[NO RESULTS] All takeoff attempts failed.")
        for e in errors:
            print(f"  {e['page']}: {e['error']}")

    print("\n[DONE]")

if __name__ == "__main__":
    main()
