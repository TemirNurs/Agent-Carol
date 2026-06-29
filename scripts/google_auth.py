#!/usr/bin/env python3
"""
Google auth helper for Carol — uses existing OAuth client + refresh-token flow.

Reads:
  data/config/gdrive_credentials.json   - OAuth installed-app client
  data/config/google_token.json          - cached token (will be created on first run)

If google_token.json doesn't exist or has insufficient scopes, runs a one-time
browser auth flow to get a token with both Drive + Sheets scopes. After that,
the token auto-refreshes itself; no further user interaction needed.

Usage:
  python scripts/google_auth.py                     # status check
  python scripts/google_auth.py --reauth            # force browser re-auth
  python scripts/google_auth.py --test              # quick read test on CRM sheet
"""

import argparse
import json
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = Path(__file__).resolve().parent.parent
CRED_FILE  = BASE / "data" / "config" / "gdrive_credentials.json"
TOKEN_FILE = BASE / "data" / "config" / "google_token.json"
LEGACY_TOKEN = BASE / "data" / "config" / "gdrive_token.json"

# 'drive' is a superset that includes spreadsheets cell-level access.
# Using just 'drive' avoids re-authorizing the OAuth consent screen.
SCOPES = [
    "https://www.googleapis.com/auth/drive",
]


def load_creds(force_reauth=False):
    """Return google.oauth2.credentials.Credentials, refreshing or running browser flow as needed."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    # Try cached token first
    if TOKEN_FILE.exists() and not force_reauth:
        try:
            creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
        except Exception:
            creds = None

    # Fall back to legacy token (drive scope only) — we'll use it but warn
    if not creds and LEGACY_TOKEN.exists() and not force_reauth:
        try:
            creds = Credentials.from_authorized_user_file(str(LEGACY_TOKEN), SCOPES)
            print("[auth] Using legacy gdrive_token.json (drive scope only — re-auth recommended for Sheets)")
        except Exception:
            creds = None

    # If creds expired but we have refresh_token, refresh
    if creds and not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                # Persist refreshed token
                TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
                print("[auth] Refreshed token saved")
            except Exception as e:
                print(f"[auth] Refresh failed: {e}")
                creds = None

    # Need full auth flow (one-time browser)
    if not creds or not creds.valid:
        if not CRED_FILE.exists():
            print(f"[auth] ERROR: {CRED_FILE} missing — can't auth")
            sys.exit(1)
        print("[auth] Running OAuth browser flow (one-time)...")
        flow = InstalledAppFlow.from_client_secrets_file(str(CRED_FILE), SCOPES)
        creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
        print(f"[auth] Token saved → {TOKEN_FILE.name}")

    return creds


def get_gspread_client(creds=None):
    """Returns an authorized gspread.Client."""
    import gspread
    if creds is None:
        creds = load_creds()
    return gspread.authorize(creds)


def get_drive_service(creds=None):
    """Returns Google Drive API service object."""
    from googleapiclient.discovery import build
    if creds is None:
        creds = load_creds()
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reauth", action="store_true", help="Force fresh browser auth flow")
    ap.add_argument("--test",   action="store_true", help="Test read of the CRM Google Sheet")
    args = ap.parse_args()

    creds = load_creds(force_reauth=args.reauth)
    print(f"[auth] OK — scopes: {creds.scopes}")
    print(f"[auth] expires: {creds.expiry}")

    if args.test:
        # Find Carol's CRM sheet in Drive
        drive = get_drive_service(creds)
        # Look for files named CRM-Bid-Log
        q = "name contains 'CRM-Bid-Log' and trashed=false"
        results = drive.files().list(
            q=q, pageSize=10,
            fields="files(id, name, mimeType, modifiedTime)").execute()
        files = results.get("files", [])
        print(f"\n[test] Found {len(files)} CRM-related files in Drive:")
        for f in files:
            print(f"  {f.get('mimeType','?'):50}  {f.get('modifiedTime','?')[:10]}  {f.get('name')}  (id: {f.get('id')[:20]}...)")


if __name__ == "__main__":
    main()
