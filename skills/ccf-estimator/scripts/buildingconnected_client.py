#!/usr/bin/env python3
"""
BuildingConnected API Client (Autodesk Platform Services)
Connects to BuildingConnected to list bid opportunities and download documents.

Setup:
  1. Get API credentials from Autodesk Platform Services (APS)
  2. Save to data/config/bc_auth.json:
     {"client_id": "...", "client_secret": "...", "account_id": "..."}
  3. Run: python buildingconnected_client.py auth  (gets OAuth token)

Usage:
  python buildingconnected_client.py opportunities [--status open]
  python buildingconnected_client.py opportunity --id <opp_id>
  python buildingconnected_client.py documents --id <opp_id> --output <dir>
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import httpx
except ImportError:
    httpx = None

CONFIG_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data" / "config"
AUTH_FILE = CONFIG_DIR / "bc_auth.json"
TOKEN_FILE = CONFIG_DIR / "bc_token.json"

BASE_URL = "https://developer.api.autodesk.com/construction/buildingconnected/v2"
AUTH_URL = "https://developer.api.autodesk.com/authentication/v2/token"


def _load_config():
    if not AUTH_FILE.exists():
        return None
    with open(AUTH_FILE) as f:
        return json.load(f)


def _load_token():
    if not TOKEN_FILE.exists():
        return None
    with open(TOKEN_FILE) as f:
        data = json.load(f)
    if data.get("expires_at", 0) < time.time():
        return None  # expired
    return data.get("access_token")


def _save_token(token_data):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    token_data["expires_at"] = time.time() + token_data.get("expires_in", 3600) - 60
    with open(TOKEN_FILE, "w") as f:
        json.dump(token_data, f, indent=2)


def authenticate():
    """Get OAuth token from APS."""
    if httpx is None:
        return {"error": "httpx not installed. Run: pip install httpx"}

    config = _load_config()
    if not config:
        return {"error": f"No config found. Create {AUTH_FILE} with client_id, client_secret, account_id",
                "template": {"client_id": "YOUR_APS_CLIENT_ID", "client_secret": "YOUR_APS_CLIENT_SECRET", "account_id": "YOUR_BC_ACCOUNT_ID"}}

    response = httpx.post(AUTH_URL, data={
        "grant_type": "client_credentials",
        "client_id": config["client_id"],
        "client_secret": config["client_secret"],
        "scope": "data:read",
    })

    if response.status_code == 200:
        token_data = response.json()
        _save_token(token_data)
        return {"status": "authenticated", "expires_in": token_data.get("expires_in")}
    else:
        return {"error": f"Auth failed: {response.status_code}", "detail": response.text}


def _get_headers():
    token = _load_token()
    if not token:
        auth_result = authenticate()
        if "error" in auth_result:
            return None, auth_result
        token = _load_token()
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, None


def list_opportunities(status=None, limit=50):
    """List bid opportunities from BuildingConnected."""
    if httpx is None:
        return {"error": "httpx not installed. Run: pip install httpx"}

    headers, err = _get_headers()
    if err:
        return err

    config = _load_config()
    params = {"limit": limit}
    if status:
        params["filter[status]"] = status

    url = f"{BASE_URL}/opportunities"
    response = httpx.get(url, headers=headers, params=params)

    if response.status_code == 200:
        data = response.json()
        opportunities = []
        for opp in data.get("results", data.get("data", [])):
            opportunities.append({
                "id": opp.get("id"),
                "project_name": opp.get("name") or opp.get("projectName", ""),
                "gc": opp.get("companyName") or opp.get("company", {}).get("name", ""),
                "location": opp.get("location", {}).get("formattedAddress", ""),
                "bid_due": opp.get("bidsDueAt") or opp.get("dueDate", ""),
                "status": opp.get("status", ""),
                "trades": opp.get("trades", []),
                "documents_available": bool(opp.get("documents") or opp.get("filesCount", 0) > 0),
                "portal_url": f"https://app.buildingconnected.com/opportunities/{opp.get('id', '')}",
                "source": "buildingconnected",
            })
        return {"opportunities": opportunities, "total": len(opportunities)}
    else:
        return {"error": f"API error: {response.status_code}", "detail": response.text}


def get_opportunity(opp_id):
    """Get details for a specific opportunity."""
    if httpx is None:
        return {"error": "httpx not installed"}

    headers, err = _get_headers()
    if err:
        return err

    url = f"{BASE_URL}/opportunities/{opp_id}"
    response = httpx.get(url, headers=headers)

    if response.status_code == 200:
        return response.json()
    return {"error": f"API error: {response.status_code}", "detail": response.text}


def download_documents(opp_id, output_dir):
    """Download all documents for an opportunity."""
    if httpx is None:
        return {"error": "httpx not installed"}

    headers, err = _get_headers()
    if err:
        return err

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    # Get document list
    url = f"{BASE_URL}/opportunities/{opp_id}/documents"
    response = httpx.get(url, headers=headers)

    if response.status_code != 200:
        return {"error": f"API error: {response.status_code}", "detail": response.text}

    docs = response.json().get("results", response.json().get("data", []))
    downloaded = []

    for doc in docs:
        doc_name = doc.get("name") or doc.get("fileName", f"document_{doc.get('id', 'unknown')}")
        download_url = doc.get("downloadUrl") or doc.get("url", "")

        if download_url:
            try:
                file_response = httpx.get(download_url, headers=headers, follow_redirects=True)
                if file_response.status_code == 200:
                    file_path = output_path / doc_name
                    with open(file_path, "wb") as f:
                        f.write(file_response.content)
                    downloaded.append({
                        "name": doc_name,
                        "path": str(file_path),
                        "size_kb": round(len(file_response.content) / 1024, 1),
                        "type": doc.get("type", "unknown"),
                    })
            except Exception as e:
                downloaded.append({"name": doc_name, "error": str(e)})

    return {"downloaded": downloaded, "total": len(downloaded), "output_dir": str(output_path)}


def check_status():
    """Check if BuildingConnected is configured and accessible."""
    config = _load_config()
    if not config:
        return {
            "configured": False,
            "message": f"Create {AUTH_FILE} with your APS credentials",
            "template": {
                "client_id": "YOUR_APS_CLIENT_ID",
                "client_secret": "YOUR_APS_CLIENT_SECRET",
                "account_id": "YOUR_BC_ACCOUNT_ID"
            }
        }

    token = _load_token()
    return {
        "configured": True,
        "authenticated": token is not None,
        "client_id": config.get("client_id", "")[:8] + "...",
    }


def main():
    parser = argparse.ArgumentParser(description="BuildingConnected Client")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status")
    sub.add_parser("auth")

    opp_list = sub.add_parser("opportunities")
    opp_list.add_argument("--status", default=None, help="Filter: open, closed, all")
    opp_list.add_argument("--limit", type=int, default=50)

    opp_get = sub.add_parser("opportunity")
    opp_get.add_argument("--id", required=True)

    doc_dl = sub.add_parser("documents")
    doc_dl.add_argument("--id", required=True, help="Opportunity ID")
    doc_dl.add_argument("--output", required=True, help="Output directory")

    args = parser.parse_args()

    if args.command == "status":
        result = check_status()
    elif args.command == "auth":
        result = authenticate()
    elif args.command == "opportunities":
        result = list_opportunities(args.status, args.limit)
    elif args.command == "opportunity":
        result = get_opportunity(args.id)
    elif args.command == "documents":
        result = download_documents(args.id, args.output)
    else:
        parser.print_help()
        sys.exit(1)

    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
