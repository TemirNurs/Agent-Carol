"""
Find all sets and pages in the Hopewell project on Togal.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from togal_client import TogalClient

AUTH_PATH = Path(r"C:\Agent Carol\data\config\togal_auth.json")

def main():
    auth = json.loads(AUTH_PATH.read_text())
    project_id = auth.get("project_id")

    client = TogalClient()
    client.authenticate()

    # List all sets in the project
    print(f"[1] Listing sets in project {project_id}...")
    resp = client._get("/v1/set", params={
        "$where": json.dumps({"project_id": project_id}),
    })

    sets = resp.get("rows", []) if isinstance(resp, dict) else []
    print(f"    Found {len(sets)} sets")

    for s in sets:
        set_id = s.get("id", "?")
        name = s.get("name", "unknown")
        print(f"\n    SET: {name} (id: {set_id})")

        # List pages in this set
        pages = client.list_pages(set_id)
        print(f"    Pages: {len(pages)}")

        arch_count = 0
        for p in pages:
            pname = p.get("name", "unknown")
            state = p.get("state", "?")
            pid = p.get("id", "?")

            # Flag architectural pages
            is_arch = any(prefix in pname.upper() for prefix in [
                'A1', 'A2', 'A3', 'A4', 'A5', 'A6', 'A7', 'A8', 'A9',
                'A0', 'FLOOR', 'INTERIOR', 'ELEVAT', 'FINISH', 'REFLECT',
                'ENLARGED', 'SECTION'
            ])
            marker = " *** ARCH ***" if is_arch else ""
            if is_arch:
                arch_count += 1

            print(f"      [{state}] {pname}{marker}")

        print(f"    Architectural pages: {arch_count}")

    # Also try listing all pages without set filter
    print(f"\n[2] Listing ALL pages in project...")
    resp2 = client._get("/v1/page", params={
        "$where": json.dumps({"project_id": project_id}),
        "$limit": 100,
    })
    all_pages = resp2.get("rows", []) if isinstance(resp2, dict) else []
    print(f"    Total pages across all sets: {len(all_pages)}")

    # Group by set
    by_set = {}
    for p in all_pages:
        sid = p.get("set_id", "none")
        if sid not in by_set:
            by_set[sid] = []
        by_set[sid].append(p)

    for sid, pages in by_set.items():
        print(f"\n    Set {sid}: {len(pages)} pages")
        for p in pages[:5]:
            print(f"      {p.get('name', '?')}")
        if len(pages) > 5:
            print(f"      ... and {len(pages)-5} more")


if __name__ == "__main__":
    main()
