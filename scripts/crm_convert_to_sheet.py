#!/usr/bin/env python3
"""
One-time: convert CRM-Bid-Log.xlsx in Drive to a native Google Sheet.

After conversion:
  - Native Sheet supports real-time multi-user editing
  - Carol writes via gspread (cell-level updates, no file conflicts)
  - The xlsx version stays as a backup (or we can delete it after)

Usage:
  python scripts/crm_convert_to_sheet.py              # convert
  python scripts/crm_convert_to_sheet.py --dry-run    # preview
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
sys.path.insert(0, str(BASE / "scripts"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from google_auth import load_creds, get_drive_service
    creds = load_creds()
    drive = get_drive_service(creds)

    # Find the CRM xlsx in Drive
    q = "name='CRM-Bid-Log.xlsx' and trashed=false"
    res = drive.files().list(q=q, pageSize=10,
        fields="files(id, name, mimeType, modifiedTime, parents)").execute()
    files = res.get("files", [])
    if not files:
        # Try fuzzy match
        q = "name contains 'CRM-Bid-Log' and trashed=false"
        res = drive.files().list(q=q, pageSize=10,
            fields="files(id, name, mimeType, modifiedTime, parents)").execute()
        files = res.get("files", [])
    if not files:
        print("[convert] No CRM-Bid-Log file found in Drive")
        return
    src = files[0]
    print(f"[convert] Source: {src['name']} ({src['mimeType']}) id={src['id']}")
    print(f"[convert] Modified: {src.get('modifiedTime','?')}")

    # Check if a native Sheet already exists
    q = "name='CRM-Bid-Log' and mimeType='application/vnd.google-apps.spreadsheet' and trashed=false"
    existing = drive.files().list(q=q, pageSize=5,
        fields="files(id, name, modifiedTime)").execute().get("files", [])
    if existing:
        e = existing[0]
        print(f"[convert] Native Sheet ALREADY exists: '{e['name']}' id={e['id']}")
        print(f"[convert] Skipping conversion — using existing Sheet")
        # Save the ID for Carol scripts
        cfg = BASE / "data" / "config" / "crm_sheet.json"
        cfg.write_text(json.dumps({
            "sheet_id": e["id"],
            "sheet_name": e["name"],
            "source": "existing",
        }, indent=2), encoding="utf-8")
        print(f"[convert] Saved sheet ID → {cfg.name}")
        return

    if args.dry_run:
        print("[dry-run] Would convert xlsx to native Google Sheet")
        return

    # Convert: Drive API copy with mimeType change
    body = {
        "name": "CRM-Bid-Log",
        "mimeType": "application/vnd.google-apps.spreadsheet",
    }
    # Preserve parent folder
    if src.get("parents"):
        body["parents"] = src["parents"]

    print(f"[convert] Creating native Google Sheet from xlsx...")
    new = drive.files().copy(fileId=src["id"], body=body,
        fields="id, name, mimeType, webViewLink").execute()
    print(f"[convert] CREATED: {new['name']} id={new['id']}")
    print(f"[convert] URL: {new.get('webViewLink','?')}")

    # Save the sheet ID for Carol scripts to find
    cfg = BASE / "data" / "config" / "crm_sheet.json"
    cfg.write_text(json.dumps({
        "sheet_id": new["id"],
        "sheet_name": new["name"],
        "url": new.get("webViewLink"),
        "source": "converted_from_xlsx",
        "original_xlsx_id": src["id"],
    }, indent=2), encoding="utf-8")
    print(f"[convert] Saved sheet config → {cfg.name}")
    print()
    print("Next steps:")
    print(f"  1. Open the Sheet:  {new.get('webViewLink','?')}")
    print(f"  2. Verify all sheets transferred (Bid Log, Completed Projects, GC Directory, Lookups)")
    print(f"  3. The xlsx version is still in Drive as backup — delete it later if everything looks good")


if __name__ == "__main__":
    main()
