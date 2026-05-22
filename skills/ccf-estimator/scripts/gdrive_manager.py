#!/usr/bin/env python3
"""
CCF Google Drive Manager
Creates folders and uploads bid documents to Google Drive.
Uses OAuth2 with the estimates@carolinacommercialfinishes.com account.

FIRST TIME SETUP:
  1. Go to https://console.cloud.google.com/
  2. Create a project (or use existing)
  3. Enable "Google Drive API"
  4. Create OAuth 2.0 credentials (Desktop app)
  5. Download the JSON and save as data/config/gdrive_credentials.json
  6. Run: python gdrive_manager.py auth
     (Opens browser, you approve, token saved)

After auth, Carol can create folders and upload files without any browser interaction.

Agent Carol folder ID: 1Os5MMBxPzxJrBuE_u-2iqztlnV1_fup4

Usage:
  python gdrive_manager.py auth                    # One-time OAuth setup
  python gdrive_manager.py status                  # Check config
  python gdrive_manager.py setup-bid-day --date "3-31-2026" --projects '[...]'
  python gdrive_manager.py create-folder --name "3-31-2026" [--parent <id>]
  python gdrive_manager.py upload --folder-id <id> --files doc1.pdf doc2.pdf
  python gdrive_manager.py list --folder-id <id>
"""

import argparse
import json
import os
import sys
import io
from datetime import datetime
from pathlib import Path

CONFIG_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data" / "config"
CREDENTIALS_FILE = CONFIG_DIR / "gdrive_credentials.json"
TOKEN_FILE = CONFIG_DIR / "gdrive_token.json"
AGENT_CAROL_FOLDER_ID = "1Os5MMBxPzxJrBuE_u-2iqztlnV1_fup4"

SCOPES = ["https://www.googleapis.com/auth/drive"]


def _get_drive_service():
    """Get authenticated Google Drive service."""
    try:
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except ImportError:
        return None, "Install google packages: pip install google-auth google-auth-oauthlib google-api-python-client"

    creds = None

    # Load existing token
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    # Refresh or get new token
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                return None, f"No credentials file. Download OAuth credentials from Google Cloud Console and save to {CREDENTIALS_FILE}"
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)

        # Save token
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

    service = build("drive", "v3", credentials=creds)
    return service, None


def authenticate():
    """Run OAuth flow — opens browser for one-time approval."""
    service, error = _get_drive_service()
    if error:
        return {"error": error}

    # Test access
    try:
        about = service.about().get(fields="user").execute()
        return {
            "status": "authenticated",
            "user": about.get("user", {}).get("emailAddress", ""),
            "display_name": about.get("user", {}).get("displayName", ""),
            "token_saved": str(TOKEN_FILE),
        }
    except Exception as e:
        return {"error": str(e)}


def create_folder(name, parent_id=None):
    """Create a folder in Google Drive.
    Returns: {"id": "folder_id", "name": "folder_name", "url": "..."}
    """
    service, error = _get_drive_service()
    if error:
        return {"error": error}

    parent = parent_id or AGENT_CAROL_FOLDER_ID

    # Check if folder already exists
    query = f"name='{name}' and '{parent}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"
    results = service.files().list(q=query, fields="files(id,name)").execute()
    existing = results.get("files", [])

    if existing:
        fid = existing[0]["id"]
        return {
            "id": fid,
            "name": name,
            "url": f"https://drive.google.com/drive/folders/{fid}",
            "already_existed": True,
        }

    # Create new folder
    metadata = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent],
    }
    folder = service.files().create(body=metadata, fields="id,name,webViewLink").execute()
    return {
        "id": folder["id"],
        "name": folder["name"],
        "url": folder.get("webViewLink", f"https://drive.google.com/drive/folders/{folder['id']}"),
        "already_existed": False,
    }


def upload_file(file_path, folder_id):
    """Upload a file to a specific Drive folder."""
    from googleapiclient.http import MediaFileUpload

    service, error = _get_drive_service()
    if error:
        return {"error": error}

    fp = Path(file_path)
    if not fp.exists():
        return {"error": f"File not found: {file_path}"}

    # Determine MIME type
    mime_types = {
        ".pdf": "application/pdf",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xls": "application/vnd.ms-excel",
        ".csv": "text/csv",
        ".doc": "application/msword",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".dwg": "application/acad",
    }
    mime = mime_types.get(fp.suffix.lower(), "application/octet-stream")

    metadata = {
        "name": fp.name,
        "parents": [folder_id],
    }
    media = MediaFileUpload(str(fp), mimetype=mime, resumable=True)
    uploaded = service.files().create(body=metadata, media_body=media, fields="id,name,size,webViewLink").execute()

    return {
        "id": uploaded["id"],
        "name": uploaded["name"],
        "size": uploaded.get("size"),
        "url": uploaded.get("webViewLink"),
    }


def upload_files(file_paths, folder_id):
    """Upload multiple files to a folder."""
    results = []
    for fp in file_paths:
        result = upload_file(fp, folder_id)
        results.append(result)
    return {"uploaded": results, "total": len(results)}


def list_folder(folder_id=None):
    """List contents of a folder."""
    service, error = _get_drive_service()
    if error:
        return {"error": error}

    fid = folder_id or AGENT_CAROL_FOLDER_ID
    query = f"'{fid}' in parents and trashed=false"
    results = service.files().list(q=query, fields="files(id,name,mimeType,size,modifiedTime)", orderBy="name").execute()

    files = []
    for f in results.get("files", []):
        files.append({
            "id": f["id"],
            "name": f["name"],
            "type": "folder" if "folder" in f.get("mimeType", "") else "file",
            "size": f.get("size"),
            "modified": f.get("modifiedTime"),
        })

    return {"files": files, "total": len(files), "folder_id": fid}


def setup_bid_day(date_str, project_names):
    """Create date folder + project subfolders for a bid day.
    This is the main function Carol calls when user says 'bid on all'.

    Args:
        date_str: "3-31-2026" format
        project_names: list of project folder names

    Returns dict with all folder IDs and URLs.
    """
    # Create date folder inside Agent Carol
    date_result = create_folder(date_str, AGENT_CAROL_FOLDER_ID)
    if "error" in date_result:
        return date_result

    date_folder_id = date_result["id"]

    # Create project subfolders
    project_folders = []
    for name in project_names:
        proj_result = create_folder(name, date_folder_id)
        project_folders.append({
            "project": name,
            "folder_id": proj_result.get("id"),
            "url": proj_result.get("url"),
            "already_existed": proj_result.get("already_existed", False),
        })

    return {
        "status": "created",
        "date_folder": date_str,
        "date_folder_id": date_folder_id,
        "date_folder_url": date_result.get("url"),
        "project_folders": project_folders,
        "total_projects": len(project_folders),
    }


def check_status():
    """Check Google Drive configuration status."""
    has_creds = CREDENTIALS_FILE.exists()
    has_token = TOKEN_FILE.exists()

    result = {
        "credentials_file": str(CREDENTIALS_FILE),
        "has_credentials": has_creds,
        "has_token": has_token,
        "agent_carol_folder_id": AGENT_CAROL_FOLDER_ID,
    }

    if has_token:
        try:
            service, error = _get_drive_service()
            if error:
                result["auth_status"] = "error"
                result["error"] = error
            else:
                about = service.about().get(fields="user").execute()
                result["auth_status"] = "authenticated"
                result["email"] = about.get("user", {}).get("emailAddress", "")
        except Exception as e:
            result["auth_status"] = "expired"
            result["error"] = str(e)
    else:
        result["auth_status"] = "not_authenticated"
        if not has_creds:
            result["setup_instructions"] = [
                "1. Go to https://console.cloud.google.com/",
                "2. Create project or select existing",
                "3. Enable 'Google Drive API' (APIs & Services > Library)",
                "4. Create OAuth 2.0 credentials (APIs & Services > Credentials > Create > OAuth client ID > Desktop app)",
                "5. Download the JSON file",
                f"6. Save it as {CREDENTIALS_FILE}",
                "7. Run: python gdrive_manager.py auth",
            ]

    return result


def main():
    parser = argparse.ArgumentParser(description="CCF Google Drive Manager")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("auth")
    sub.add_parser("status")

    p_create = sub.add_parser("create-folder")
    p_create.add_argument("--name", required=True)
    p_create.add_argument("--parent", default=None)

    p_setup = sub.add_parser("setup-bid-day")
    p_setup.add_argument("--date", required=True)
    p_setup.add_argument("--projects", required=True, help="JSON array of project names")

    p_upload = sub.add_parser("upload")
    p_upload.add_argument("--folder-id", required=True)
    p_upload.add_argument("--files", nargs="+", required=True)

    p_list = sub.add_parser("list")
    p_list.add_argument("--folder-id", default=None)

    args = parser.parse_args()

    if args.command == "auth":
        result = authenticate()
    elif args.command == "status":
        result = check_status()
    elif args.command == "create-folder":
        result = create_folder(args.name, args.parent)
    elif args.command == "setup-bid-day":
        projects = json.loads(args.projects)
        result = setup_bid_day(args.date, projects)
    elif args.command == "upload":
        result = upload_files(args.files, args.folder_id)
    elif args.command == "list":
        result = list_folder(args.folder_id)
    else:
        parser.print_help()
        sys.exit(1)

    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
