#!/usr/bin/env python3
"""
CCF Document Downloader
Downloads all bid documents for a project from BuildingConnected, ConstructConnect, or email.
Indexes files by type (plans, specs, scope letters, addenda).

Usage:
  python doc_downloader.py --project "food-lion-2655" --source buildingconnected --portal-id abc123
  python doc_downloader.py --project "food-lion-2655" --source constructconnect --portal-id xyz789
  python doc_downloader.py --project "food-lion-2655" --source local --files "plan.pdf,spec.pdf"
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

PROJECTS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data" / "projects"

# File type classification patterns
FILE_TYPE_PATTERNS = {
    "plans": [r"plan", r"drawing", r"sheet", r"A\d", r"S\d", r"M\d", r"E\d", r"P\d",
              r"floor\s*plan", r"elevation", r"section", r"detail", r"schedule"],
    "specs": [r"spec", r"specification", r"division", r"section\s*\d{2}",
              r"09\s*91", r"09\s*96", r"09\s*72", r"masterformat"],
    "scope_letter": [r"scope", r"scope\s*letter", r"scope\s*of\s*work", r"SOW",
                     r"bid\s*form", r"bid\s*package"],
    "addendum": [r"addend", r"addenda", r"revision", r"revised", r"ASI",
                 r"bulletin", r"supplement"],
    "schedule": [r"schedule", r"timeline", r"milestone", r"phasing"],
    "finish_schedule": [r"finish\s*schedule", r"color\s*schedule", r"paint\s*schedule",
                        r"color\s*board"],
}


def classify_document(filename, content_text=None):
    """Classify a document by type based on filename and optional text content."""
    name_lower = filename.lower()
    text_lower = (content_text or "").lower()
    combined = f"{name_lower} {text_lower}"

    for doc_type, patterns in FILE_TYPE_PATTERNS.items():
        for pat in patterns:
            if re.search(pat, combined, re.IGNORECASE):
                return doc_type

    # Default based on extension
    ext = Path(filename).suffix.lower()
    if ext == ".pdf":
        return "document"
    elif ext in (".xlsx", ".xls", ".csv"):
        return "spreadsheet"
    return "other"


def download_from_buildingconnected(project_slug, portal_id):
    """Download all documents from a BuildingConnected opportunity."""
    from buildingconnected_client import download_documents

    output_dir = PROJECTS_DIR / project_slug / "bid_docs"
    result = download_documents(portal_id, str(output_dir))

    if "error" in result:
        return result

    # Classify downloaded files
    manifest = []
    for doc in result.get("downloaded", []):
        doc_type = classify_document(doc.get("name", ""))
        doc["doc_type"] = doc_type
        manifest.append(doc)

    return _save_manifest(project_slug, manifest, "buildingconnected")


def download_from_constructconnect(project_slug, portal_id):
    """Download all documents from a ConstructConnect project."""
    from constructconnect_client import api_download_documents

    output_dir = PROJECTS_DIR / project_slug / "bid_docs"
    result = api_download_documents(portal_id, str(output_dir))

    if "error" in result:
        return result

    manifest = []
    for doc in result.get("downloaded", []):
        doc_type = classify_document(doc.get("name", ""))
        doc["doc_type"] = doc_type
        manifest.append(doc)

    return _save_manifest(project_slug, manifest, "constructconnect")


def index_local_files(project_slug, file_paths):
    """Index locally uploaded files."""
    import shutil

    output_dir = PROJECTS_DIR / project_slug / "bid_docs"
    output_dir.mkdir(parents=True, exist_ok=True)

    manifest = []
    for fp in file_paths:
        src = Path(fp)
        if src.exists():
            dst = output_dir / src.name
            shutil.copy2(str(src), str(dst))
            doc_type = classify_document(src.name)
            manifest.append({
                "name": src.name,
                "path": str(dst),
                "size_kb": round(src.stat().st_size / 1024, 1),
                "doc_type": doc_type,
            })

    return _save_manifest(project_slug, manifest, "local")


def _save_manifest(project_slug, manifest, source):
    """Save document manifest to project directory."""
    manifest_file = PROJECTS_DIR / project_slug / "doc_manifest.json"
    manifest_file.parent.mkdir(parents=True, exist_ok=True)

    data = {
        "project": project_slug,
        "source": source,
        "downloaded_at": __import__("datetime").datetime.now().isoformat(),
        "documents": manifest,
        "summary": {
            "total": len(manifest),
            "by_type": {},
        }
    }

    for doc in manifest:
        dt = doc.get("doc_type", "other")
        data["summary"]["by_type"][dt] = data["summary"]["by_type"].get(dt, 0) + 1

    with open(manifest_file, "w") as f:
        json.dump(data, f, indent=2)

    return data


def get_manifest(project_slug):
    """Get the document manifest for a project."""
    manifest_file = PROJECTS_DIR / project_slug / "doc_manifest.json"
    if manifest_file.exists():
        with open(manifest_file) as f:
            return json.load(f)
    return None


def main():
    parser = argparse.ArgumentParser(description="CCF Document Downloader")
    parser.add_argument("--project", required=True, help="Project slug")
    parser.add_argument("--source", required=True, choices=["buildingconnected", "constructconnect", "local"])
    parser.add_argument("--portal-id", default=None, help="Portal project/opportunity ID")
    parser.add_argument("--files", default=None, help="Comma-separated file paths (for local source)")
    args = parser.parse_args()

    if args.source == "buildingconnected":
        if not args.portal_id:
            print(json.dumps({"error": "Need --portal-id for BuildingConnected"}))
            sys.exit(1)
        result = download_from_buildingconnected(args.project, args.portal_id)
    elif args.source == "constructconnect":
        if not args.portal_id:
            print(json.dumps({"error": "Need --portal-id for ConstructConnect"}))
            sys.exit(1)
        result = download_from_constructconnect(args.project, args.portal_id)
    elif args.source == "local":
        if not args.files:
            print(json.dumps({"error": "Need --files for local source"}))
            sys.exit(1)
        file_paths = [f.strip() for f in args.files.split(",")]
        result = index_local_files(args.project, file_paths)

    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
