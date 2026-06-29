#!/usr/bin/env python3
"""
togal_client.py — Togal.AI REST API client for automated takeoffs.

Workflow:
  1. Authenticate (get session token)
  2. Create or find project
  3. Create set within project
  4. Upload drawing pages (PDF)
  5. Run AI takeoff processing
  6. Retrieve measurements from views

Usage:
  python scripts/togal_client.py --auth                          # Test auth
  python scripts/togal_client.py --projects                      # List projects
  python scripts/togal_client.py --upload <slug> <pdf_path>      # Upload + process
  python scripts/togal_client.py --takeoff <slug>                # Run AI takeoff
  python scripts/togal_client.py --results <slug>                # Get measurements
  python scripts/togal_client.py --full <slug> <pdf_path>        # Full pipeline
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:
    print("ERROR: requests package required. Run: pip install requests")
    sys.exit(1)

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
CONFIG_DIR = DATA_DIR / "config"
PROJECTS_DIR = DATA_DIR / "projects"

TOGAL_API_BASE = "https://api-prod.togal.ai/api"
TOGAL_AUTH_FILE = CONFIG_DIR / "togal_auth.json"


class TogalClient:
    """REST client for Togal.AI API."""

    def __init__(self):
        self.base_url = TOGAL_API_BASE
        self.session_token = None
        self.api_key = None
        self._load_auth()

    # ------------------------------------------------------------------
    #  Auth
    # ------------------------------------------------------------------

    def _load_auth(self):
        """Load credentials from togal_auth.json."""
        if TOGAL_AUTH_FILE.exists():
            auth = json.loads(TOGAL_AUTH_FILE.read_text(encoding="utf-8"))
            self.api_key = auth.get("api_key")
            self.session_token = auth.get("session_token")
            self.email = auth.get("email")
            self.password = auth.get("password")
        else:
            # Check env vars
            self.api_key = os.environ.get("TOGAL_API_KEY")
            self.email = os.environ.get("TOGAL_EMAIL")
            self.password = os.environ.get("TOGAL_PASSWORD")

    def authenticate(self) -> bool:
        """Create a session via POST /v1/session. Sessions last 1 week."""
        if self.session_token:
            # Test existing session
            if self._test_session():
                return True

        if not self.email or not self.password:
            print("ERROR: No Togal credentials. Set up data/config/togal_auth.json")
            return False

        resp = self._post("/v1/session", json={
            "email": self.email,
            "password": self.password,
        }, auth=False)

        if resp and resp.get("id"):
            self.session_token = resp["id"]
            # Save session token for reuse
            self._save_session()
            print(f"Togal authenticated. Session expires in 7 days.")
            return True

        print(f"Togal authentication failed: {resp}")
        return False

    def _test_session(self) -> bool:
        """Test if current session is still valid.

        NOTE (2026-06-16 fix): Togal's API now REQUIRES organization_id on
        /v1/user and 400s without it ("You must supply an organization_id").
        The old test hit /v1/user, mis-read the 400 as an expired session, and
        made the whole pipeline re-login in a loop and read ZERO measurements
        (the 350 Hein "Togal silent failure"). The session token is fine —
        /v1/project works with just the `session` header — so test against that.
        """
        try:
            resp = self._get("/v1/project", params={"$offset": "0"})
            return isinstance(resp, dict) and "count" in resp
        except Exception:
            return False

    def _save_session(self):
        """Save session token back to auth file."""
        auth = {}
        if TOGAL_AUTH_FILE.exists():
            auth = json.loads(TOGAL_AUTH_FILE.read_text(encoding="utf-8"))
        auth["session_token"] = self.session_token
        TOGAL_AUTH_FILE.write_text(json.dumps(auth, indent=2), encoding="utf-8")

    # ------------------------------------------------------------------
    #  HTTP helpers
    # ------------------------------------------------------------------

    def _headers(self, auth=True) -> dict:
        headers = {"Content-Type": "application/json"}
        if auth and self.session_token:
            headers["session"] = self.session_token
        elif auth and self.api_key:
            headers["key"] = self.api_key
        return headers

    def _get(self, path: str, params: dict = None, auth=True):
        url = f"{self.base_url}{path}"
        try:
            r = requests.get(url, headers=self._headers(auth), params=params, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            return {"error": str(e)}

    def _post(self, path: str, json: dict = None, auth=True):
        url = f"{self.base_url}{path}"
        try:
            r = requests.post(url, headers=self._headers(auth), json=json, timeout=60)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            return {"error": str(e)}

    def _put(self, path: str, json: dict = None, auth=True):
        url = f"{self.base_url}{path}"
        try:
            r = requests.put(url, headers=self._headers(auth), json=json, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            return {"error": str(e)}

    def _delete(self, path: str, auth=True):
        url = f"{self.base_url}{path}"
        try:
            r = requests.delete(url, headers=self._headers(auth), timeout=30)
            r.raise_for_status()
            return r.json() if r.text else {"ok": True}
        except Exception as e:
            return {"error": str(e)}

    # ------------------------------------------------------------------
    #  Projects
    # ------------------------------------------------------------------

    def _get_all_rows(self, path: str, where: dict | None = None) -> list:
        """Paginated GET — Togal caps list endpoints at ~10 rows per page.
        Without the $offset loop a 14-sheet set 'never finished processing'
        (the list showed 10) and extraction read zero. Loops until count."""
        rows, offset = [], 0
        while offset < 2000:
            params = {"$offset": str(offset)}
            if where:
                params["$where"] = json.dumps(where)
            resp = self._get(path, params=params)
            if not isinstance(resp, dict):
                break
            batch = resp.get("rows", [])
            if not batch:
                break
            rows += batch
            offset += len(batch)
            if offset >= resp.get("count", 0):
                break
        return rows

    def list_projects(self) -> list:
        """GET /v1/project — list ALL projects (paginated)."""
        return self._get_all_rows("/v1/project")

    def create_project(self, name: str) -> dict:
        """POST /v1/project — create a new project."""
        return self._post("/v1/project", json={
            "name": name,
            "scale_units": "imperial",
        })

    def find_or_create_project(self, name: str) -> dict:
        """Find existing project by name, or create new one."""
        projects = self.list_projects()
        if isinstance(projects, list):
            name_lower = name.lower()
            for p in projects:
                if isinstance(p, dict):
                    p_name = p.get("name", "").lower()
                    # Exact match or fuzzy (one contains the other)
                    if p_name == name_lower or name_lower in p_name or p_name in name_lower:
                        return p
        result = self.create_project(name)
        # If 409 Conflict, project exists but name match failed — try listing again with broader match
        if result.get("error") and "409" in str(result["error"]):
            projects = self.list_projects()
            if isinstance(projects, list) and projects:
                # Return the most recently created project as best guess
                return projects[-1]
        return result

    # ------------------------------------------------------------------
    #  Sets (drawing collections within a project)
    # ------------------------------------------------------------------

    def create_set(self, project_id: str, name: str = "Bid Documents") -> dict:
        """POST /v1/set — create a set within a project. On 409 (a set with this
        name already exists for the project), reuse the existing set instead of
        aborting the whole takeoff (the bug that killed the WSSU retry)."""
        result = self._post("/v1/set", json={
            "name": name,
            "project_id": project_id,
        })
        if isinstance(result, dict) and result.get("error") and "409" in str(result.get("error")):
            sets = self.list_sets(project_id)
            for s in sets:
                if isinstance(s, dict) and (s.get("name") or "").lower() == name.lower():
                    return s
            if sets:
                return sets[-1]
        return result

    def list_sets(self, project_id: str) -> list:
        """GET /v1/set — list ALL sets for a project (paginated)."""
        return self._get_all_rows("/v1/set", {"project_id": project_id})

    # ------------------------------------------------------------------
    #  Pages (drawings / documents)
    # ------------------------------------------------------------------

    def upload_page(self, project_id: str, set_id: str,
                    pdf_path: str, page_name: str = None) -> dict:
        """
        Upload a PDF drawing to Togal using the web UI flow.
        1. POST /v1/files — create file record, get CDN + S3 URLs
        2. PUT to S3 upload_url — upload the PDF
        3. POST /v1/page/file/process — trigger page splitting with CDN URL
        """
        pdf = Path(pdf_path)
        if not pdf.exists():
            return {"error": f"File not found: {pdf_path}"}

        name = page_name or pdf.stem
        org_id = self._get_organization_id()
        if not org_id:
            return {"error": "Could not determine organization_id"}

        # Count PDF pages for pageNames array
        page_count = self._count_pdf_pages(str(pdf))

        # Step 1: Create file record via /v1/files (web UI endpoint)
        file_record = self._post("/v1/files", json={
            "relation_table": "project_id",
            "organization_id": org_id,
            "relation_id": project_id,
            "mime": "application/pdf",
        })

        if not file_record or file_record.get("error"):
            return {"error": f"Failed to create file record: {file_record}"}

        cdn_url = file_record.get("url")
        upload_url = file_record.get("upload_url")

        if not upload_url:
            return {"error": "No pre-signed upload URL returned from /v1/files"}

        # Step 2: Upload file to S3 (to /fileuploads/ path)
        try:
            with open(pdf, "rb") as f:
                r = requests.put(upload_url, data=f, headers={
                    "Content-Type": "application/pdf",
                }, timeout=180)
                r.raise_for_status()
        except Exception as e:
            return {"error": f"S3 upload failed: {e}"}

        # Step 3: Process the uploaded file using CDN URL
        page_names = [f"{name}-{i}" for i in range(page_count)] if page_count > 0 else [name]

        process_result = self._post("/v1/page/file/process", json={
            "url": cdn_url,
            "setId": set_id,
            "projectId": project_id,
            "folderId": None,
            "removedPageIndexes": [],
            "pageNames": page_names,
        })

        return {
            "file_id": file_record.get("id"),
            "set_id": set_id,
            "project_id": project_id,
            "name": name,
            "page_count": page_count,
            "upload_status": "success",
            "processing": process_result,
        }

    def _get_organization_id(self) -> str | None:
        """Get the organization_id from auth config or existing project."""
        if hasattr(self, '_org_id') and self._org_id:
            return self._org_id
        # Check auth file first
        if TOGAL_AUTH_FILE.exists():
            auth = json.loads(TOGAL_AUTH_FILE.read_text(encoding="utf-8"))
            org_id = auth.get("organization_id")
            if org_id:
                self._org_id = org_id
                return org_id
        # Fall back to extracting from a project
        projects = self.list_projects()
        if projects and isinstance(projects, list):
            for p in projects:
                org_id = p.get("organization_id")
                if org_id:
                    self._org_id = org_id
                    # Save for future use
                    if TOGAL_AUTH_FILE.exists():
                        auth = json.loads(TOGAL_AUTH_FILE.read_text(encoding="utf-8"))
                        auth["organization_id"] = org_id
                        TOGAL_AUTH_FILE.write_text(json.dumps(auth, indent=2), encoding="utf-8")
                    return org_id
        return None

    @staticmethod
    def _count_pdf_pages(pdf_path: str) -> int:
        """Count pages in a PDF file."""
        try:
            import fitz  # PyMuPDF
            doc = fitz.open(pdf_path)
            count = len(doc)
            doc.close()
            return count
        except Exception:
            return 1

    def list_pages(self, set_id: str) -> list:
        """GET /v1/page — list ALL pages in a set (paginated; the 10-row cap
        here is exactly what stalled the 14-sheet Clemson takeoff)."""
        return self._get_all_rows("/v1/page", {"set_id": set_id})

    # ------------------------------------------------------------------
    #  Views (user workspace with measurements)
    # ------------------------------------------------------------------

    def create_view(self, page_id: str, name: str = "Carol Takeoff") -> dict:
        """POST /v1/view — create a view for a page."""
        return self._post("/v1/view", json={
            "name": name,
            "page_id": page_id,
        })

    def get_view(self, view_id: str) -> dict:
        """GET /v1/view/{view} — get view with measurements."""
        return self._get(f"/v1/view/{view_id}", params={
            "$attributes": "data,geojson,metadata",
        })

    def get_view_geojson(self, view_id: str) -> dict:
        """GET /v1/view/{view}/geojson — get geojson measurements."""
        return self._get(f"/v1/view/{view_id}/geojson")

    # ------------------------------------------------------------------
    #  AI Takeoff Processing (the key feature)
    # ------------------------------------------------------------------

    def run_ai_takeoff(self, page_id: str, view_id: str = None,
                       workflow_type: str = "paint") -> dict:
        """
        POST /v1/page-processing/run — Run AI takeoff on a page.

        workflow_type: "full", "drywall", or "paint"
        Returns processing results with extracted measurements.
        """
        payload = {
            "page_id": page_id,
            "workflow_type": workflow_type,
        }
        if view_id:
            payload["view_id"] = view_id

        return self._post("/v1/page-processing/run", json=payload)

    def check_ai_status(self, page_id: str) -> dict:
        """Check AI processing status for a page."""
        resp = self._get("/v1/ai_action_event", params={
            "$where": json.dumps({"page_id": page_id}),
            "$order": json.dumps([["created_at", "DESC"]]),
            "$limit": 5,
        })
        if isinstance(resp, dict) and resp.get("rows"):
            return resp["rows"][0]
        return {"status": "unknown"}

    # ------------------------------------------------------------------
    #  Full Pipeline: upload → process → takeoff → extract measurements
    # ------------------------------------------------------------------

    def full_takeoff_pipeline(self, project_name: str, pdf_paths: list,
                              workflow_type: str = "paint") -> dict:
        """
        Complete pipeline: create project → upload drawings → run AI → get results.
        Returns structured measurements ready for the estimate engine.
        """
        result = {
            "project_name": project_name,
            "pages_processed": 0,
            "measurements": [],
            "errors": [],
        }

        # Step 1: Find or create project
        project = self.find_or_create_project(project_name)
        if project.get("error"):
            result["errors"].append(f"Project creation failed: {project['error']}")
            return result

        project_id = project["id"]
        result["togal_project_id"] = project_id

        # Step 2: Create set
        togal_set = self.create_set(project_id, "Bid Documents")
        if togal_set.get("error"):
            result["errors"].append(f"Set creation failed: {togal_set['error']}")
            return result

        set_id = togal_set["id"]
        result["togal_set_id"] = set_id

        # Step 3: Upload each PDF
        page_ids = []
        for pdf_path in pdf_paths:
            upload = self.upload_page(project_id, set_id, str(pdf_path))
            if upload.get("error"):
                result["errors"].append(f"Upload failed for {pdf_path}: {upload['error']}")
                continue
            page_ids.append(upload["page_id"])
            print(f"  Uploaded: {Path(pdf_path).name}")

        if not page_ids:
            result["errors"].append("No pages uploaded successfully")
            return result

        # Wait for processing to complete
        print("  Waiting for page processing...")
        time.sleep(10)

        # Step 4: Run AI takeoff on each page
        for page_id in page_ids:
            # Create view
            view = self.create_view(page_id)
            if view.get("error"):
                result["errors"].append(f"View creation failed: {view['error']}")
                continue

            view_id = view["id"]

            # Run AI takeoff
            print(f"  Running AI takeoff (workflow: {workflow_type})...")
            ai_result = self.run_ai_takeoff(page_id, view_id, workflow_type)
            if ai_result.get("error"):
                result["errors"].append(f"AI takeoff failed: {ai_result['error']}")
                continue

            # Wait for AI to complete
            max_wait = 120  # 2 minutes
            waited = 0
            while waited < max_wait:
                status = self.check_ai_status(page_id)
                if status.get("status") in ("completed", "failed"):
                    break
                time.sleep(10)
                waited += 10
                print(f"  AI processing... ({waited}s)")

            # Step 5: Get measurements from view
            view_data = self.get_view(view_id)
            if view_data and not view_data.get("error"):
                measurements = self._extract_measurements(view_data)
                result["measurements"].extend(measurements)
                result["pages_processed"] += 1

            # Also get geojson
            geojson = self.get_view_geojson(view_id)
            if geojson and not geojson.get("error"):
                geo_measurements = self._extract_geojson_measurements(geojson)
                result["measurements"].extend(geo_measurements)

        return result

    def _extract_measurements(self, view_data: dict) -> list:
        """Extract structured measurements from a Togal view."""
        measurements = []
        data = view_data.get("data", {})

        if not isinstance(data, dict):
            return measurements

        # Togal stores measurements in the view data
        # Structure varies by workflow type, but generally includes:
        # - areas (SF measurements)
        # - lengths (LF measurements)
        # - counts (door/window counts)
        for key, value in data.items():
            if isinstance(value, dict):
                if "area" in value or "sf" in str(value).lower():
                    measurements.append({
                        "type": "area",
                        "label": key,
                        "value_sf": value.get("area", value.get("value", 0)),
                        "source": "togal_ai",
                    })
                elif "length" in value or "lf" in str(value).lower():
                    measurements.append({
                        "type": "length",
                        "label": key,
                        "value_lf": value.get("length", value.get("value", 0)),
                        "source": "togal_ai",
                    })
                elif "count" in value:
                    measurements.append({
                        "type": "count",
                        "label": key,
                        "value": value.get("count", value.get("value", 0)),
                        "source": "togal_ai",
                    })

        return measurements

    def _extract_geojson_measurements(self, geojson: dict) -> list:
        """Extract measurements from geojson features."""
        measurements = []

        if not isinstance(geojson, dict):
            return measurements

        features = geojson.get("features", [])
        for feature in features:
            props = feature.get("properties", {})
            geom = feature.get("geometry", {})

            if props.get("area"):
                measurements.append({
                    "type": "area",
                    "label": props.get("name", props.get("label", "Unknown")),
                    "value_sf": props["area"],
                    "source": "togal_geojson",
                })

        return measurements

    # ------------------------------------------------------------------
    #  Convenience: measurements → estimate takeoff format
    # ------------------------------------------------------------------

    def measurements_to_takeoff(self, measurements: list, sow: dict = None) -> list:
        """
        Convert Togal measurements into Carol's takeoff item format.
        Maps areas to task codes based on SOW context.
        """
        takeoff_items = []

        for m in measurements:
            if m["type"] == "area":
                label = m.get("label", "").lower()
                sf = m.get("value_sf", 0)
                if sf <= 0:
                    continue

                # Classify based on label
                if "ceiling" in label or "rcp" in label:
                    task_code = "ceiling_drywall_spray"
                    method = "spray"
                elif "exterior" in label or "outside" in label:
                    task_code = "exterior_spray"
                    method = "spray"
                elif "floor" in label and "epoxy" in label:
                    task_code = "epoxy_floor"
                    method = "roll"
                else:
                    task_code = "walls_spray_2coat"
                    method = "spray"

                takeoff_items.append({
                    "area": m.get("label", "Unknown"),
                    "task_code": task_code,
                    "quantity": round(sf, 0),
                    "unit": "SF",
                    "method": method,
                    "coats": "1P+2F",
                    "notes": f"Togal AI measurement ({m.get('source', 'togal')})",
                })

            elif m["type"] == "count":
                label = m.get("label", "").lower()
                count = m.get("value", 0)
                if count <= 0:
                    continue

                if "door" in label:
                    takeoff_items.append({
                        "area": m.get("label", "Doors"),
                        "task_code": "door_paint",
                        "quantity": count,
                        "unit": "EA",
                        "method": "brush",
                        "coats": "2",
                        "notes": "Togal AI count",
                    })
                elif "window" in label:
                    takeoff_items.append({
                        "area": m.get("label", "Windows"),
                        "task_code": "window_frame",
                        "quantity": count,
                        "unit": "EA",
                        "method": "brush",
                        "coats": "2",
                        "notes": "Togal AI count",
                    })

            elif m["type"] == "length":
                label = m.get("label", "").lower()
                lf = m.get("value_lf", 0)
                if lf <= 0:
                    continue

                takeoff_items.append({
                    "area": m.get("label", "Perimeter"),
                    "task_code": "trim_base",
                    "quantity": round(lf, 0),
                    "unit": "LF",
                    "method": "brush",
                    "coats": "2",
                    "notes": "Togal AI measurement",
                })

        return takeoff_items


# ======================================================================
#  CLI
# ======================================================================

def main():
    parser = argparse.ArgumentParser(description="Togal.AI Client")
    parser.add_argument("--auth", action="store_true", help="Test authentication")
    parser.add_argument("--projects", action="store_true", help="List projects")
    parser.add_argument("--upload", nargs=2, metavar=("SLUG", "PDF"), help="Upload PDF")
    parser.add_argument("--takeoff", metavar="SLUG", help="Run AI takeoff")
    parser.add_argument("--results", metavar="SLUG", help="Get measurements")
    parser.add_argument("--full", nargs=2, metavar=("SLUG", "PDF_DIR"),
                        help="Full pipeline: upload all PDFs + run takeoff")
    args = parser.parse_args()

    client = TogalClient()

    if args.auth:
        if client.authenticate():
            print("Authentication successful.")
            user = client._get("/v1/user")
            if user and not user.get("error"):
                print(f"User: {user.get('email', '?')}")
        else:
            print("Authentication failed.")
            sys.exit(1)

    elif args.projects:
        if not client.authenticate():
            sys.exit(1)
        projects = client.list_projects()
        for p in projects:
            if isinstance(p, dict):
                print(f"  {p.get('name', '?')} (id: {p.get('id', '?')[:8]}...)")

    elif args.upload:
        slug, pdf_path = args.upload
        if not client.authenticate():
            sys.exit(1)
        project = client.find_or_create_project(slug)
        sets = client.list_sets(project["id"])
        set_id = sets[0]["id"] if sets else client.create_set(project["id"])["id"]
        result = client.upload_page(project["id"], set_id, pdf_path)
        print(json.dumps(result, indent=2))

    elif args.full:
        slug, pdf_dir = args.full
        if not client.authenticate():
            sys.exit(1)

        pdf_dir = Path(pdf_dir)
        pdfs = list(pdf_dir.glob("*.pdf"))
        if not pdfs:
            print(f"No PDFs found in {pdf_dir}")
            sys.exit(1)

        print(f"Found {len(pdfs)} PDFs. Starting full pipeline...")
        result = client.full_takeoff_pipeline(slug, pdfs)
        print(f"\nPages processed: {result['pages_processed']}")
        print(f"Measurements found: {len(result['measurements'])}")
        if result["errors"]:
            print(f"Errors: {len(result['errors'])}")
            for e in result["errors"]:
                print(f"  - {e}")

        # Save results
        proj_dir = PROJECTS_DIR / slug
        proj_dir.mkdir(parents=True, exist_ok=True)
        result_path = proj_dir / "togal_takeoff.json"
        result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"\nResults saved to: {result_path}")

        # Convert to takeoff format
        if result["measurements"]:
            takeoff_items = client.measurements_to_takeoff(result["measurements"])
            takeoff = {"source": "togal_ai", "items": takeoff_items}
            takeoff_path = proj_dir / "takeoff.json"
            takeoff_path.write_text(json.dumps(takeoff, indent=2), encoding="utf-8")
            print(f"Takeoff saved to: {takeoff_path}")

        print(f"\n__RESULT__:{json.dumps(result)}")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
