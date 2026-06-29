#!/usr/bin/env python3
"""
CCF Expired Bid Cleanup
Deletes bid documents (PDFs, ZIPs, screenshots) from project folders
after the bid due date has passed. Keeps project metadata (project.json,
scope_extract.json, doc_manifest.json, estimates) for historical reference.

Usage:
  python cleanup_expired_bids.py                  # preview what would be deleted
  python cleanup_expired_bids.py --delete          # actually delete expired docs
  python cleanup_expired_bids.py --days-grace 3    # keep docs 3 days after due date (default: 2)
  python cleanup_expired_bids.py --stats           # show disk usage stats
"""

import argparse
import json
import shutil
import sys
from datetime import datetime, date, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent.parent
PROJECTS_DIR = BASE_DIR / "data" / "projects"

# Files to ALWAYS keep (even after cleanup)
KEEP_FILES = {
    "project.json",
    "doc_manifest.json",
    "scope_extract.json",
    "estimate_input.json",
    "estimate_output.json",
    "SOW_Painting_Wallcovering.md",
    "takeoff_plan.md",
    "takeoff.csv",
    "proposal.md",
    "email.json",
}

# Extensions to delete
DELETE_EXTENSIONS = {".pdf", ".zip", ".dwg", ".dwf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}


def parse_due_date(date_str):
    """Parse due date from M/D/YYYY format."""
    if not date_str:
        return None
    for fmt in ("%m/%d/%Y", "%#m/%#d/%Y", "%-m/%-d/%Y"):
        try:
            return datetime.strptime(date_str, fmt).date()
        except (ValueError, TypeError):
            continue
    # Try other formats
    for fmt in ("%Y-%m-%d", "%b %d, %Y"):
        try:
            return datetime.strptime(date_str, fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def get_folder_size(path):
    """Get total size of a folder in MB."""
    total = 0
    for f in path.rglob("*"):
        if f.is_file():
            total += f.stat().st_size
    return total / (1024 * 1024)


def scan_projects(days_grace=2):
    """Scan all projects and categorize as active or expired."""
    today = date.today()
    cutoff = today - timedelta(days=days_grace)

    active = []
    expired = []
    no_date = []

    if not PROJECTS_DIR.exists():
        return active, expired, no_date

    for project_dir in PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue

        meta_file = project_dir / "project.json"
        due_date = None
        project_name = project_dir.name

        if meta_file.exists():
            try:
                meta = json.load(open(meta_file))
                project_name = meta.get("name", project_dir.name)
                due_date = parse_due_date(meta.get("due_date", ""))
            except:
                pass

        bid_docs = project_dir / "bid_docs"
        if not bid_docs.exists():
            continue

        # Calculate sizes
        docs_size = get_folder_size(bid_docs)
        total_size = get_folder_size(project_dir)

        info = {
            "dir": project_dir,
            "name": project_name,
            "due_date": due_date,
            "due_date_str": str(due_date) if due_date else "unknown",
            "docs_size_mb": round(docs_size, 1),
            "total_size_mb": round(total_size, 1),
            "file_count": sum(1 for f in bid_docs.rglob("*") if f.is_file()),
        }

        if due_date is None:
            no_date.append(info)
        elif due_date <= cutoff:
            expired.append(info)
        else:
            active.append(info)

    return active, expired, no_date


def cleanup_project(project_dir, dry_run=True):
    """Delete bid docs from a project folder, keeping metadata."""
    bid_docs = project_dir / "bid_docs"
    deleted = []
    kept = []
    freed_bytes = 0

    if not bid_docs.exists():
        return deleted, kept, 0

    # Delete files in bid_docs/ and subdirectories
    for f in bid_docs.rglob("*"):
        if f.is_file():
            if f.suffix.lower() in DELETE_EXTENSIONS or f.name.startswith("_"):
                freed_bytes += f.stat().st_size
                deleted.append(f.name)
                if not dry_run:
                    f.unlink()
            else:
                kept.append(f.name)

    # Remove empty subdirectories
    if not dry_run:
        for d in sorted(bid_docs.rglob("*"), reverse=True):
            if d.is_dir():
                try:
                    d.rmdir()  # only removes if empty
                except OSError:
                    pass

    # Also check root project dir for large files
    for f in project_dir.iterdir():
        if f.is_file() and f.name not in KEEP_FILES:
            if f.suffix.lower() in DELETE_EXTENSIONS:
                freed_bytes += f.stat().st_size
                deleted.append(f.name)
                if not dry_run:
                    f.unlink()

    return deleted, kept, freed_bytes


def main():
    parser = argparse.ArgumentParser(description="CCF Expired Bid Cleanup")
    parser.add_argument("--delete", action="store_true", help="Actually delete files (default: preview only)")
    parser.add_argument("--days-grace", type=int, default=2, help="Days after due date before cleanup (default: 2)")
    parser.add_argument("--stats", action="store_true", help="Show disk usage stats only")
    args = parser.parse_args()

    W = 70
    print("=" * W)
    print("  CCF BID DOCS CLEANUP")
    print("=" * W)

    active, expired, no_date = scan_projects(args.days_grace)

    if args.stats:
        total_active = sum(p["docs_size_mb"] for p in active)
        total_expired = sum(p["docs_size_mb"] for p in expired)
        total_nodate = sum(p["docs_size_mb"] for p in no_date)

        print(f"\n  DISK USAGE SUMMARY:")
        print(f"  {'=' * 50}")
        print(f"  Active bids ({len(active)} projects):     {total_active:>8.1f} MB")
        print(f"  Expired bids ({len(expired)} projects):    {total_expired:>8.1f} MB  <- can be freed")
        print(f"  No due date ({len(no_date)} projects):     {total_nodate:>8.1f} MB")
        print(f"  {'=' * 50}")
        print(f"  TOTAL:                            {total_active + total_expired + total_nodate:>8.1f} MB")
        print(f"  RECLAIMABLE:                      {total_expired:>8.1f} MB")

        if expired:
            print(f"\n  EXPIRED PROJECTS:")
            for p in sorted(expired, key=lambda x: x["docs_size_mb"], reverse=True):
                print(f"    {p['name'][:40]:<40s}  {p['docs_size_mb']:>6.1f} MB  (due: {p['due_date_str']})")
        return

    # Show active projects
    if active:
        print(f"\n  ACTIVE ({len(active)} projects - KEEPING):")
        for p in active:
            print(f"    {p['name'][:45]:<45s}  {p['docs_size_mb']:>6.1f} MB  due: {p['due_date_str']}")

    # Show expired projects
    if expired:
        total_freed = 0
        print(f"\n  EXPIRED ({len(expired)} projects - {'DELETING' if args.delete else 'WOULD DELETE'} docs):")
        print(f"  Grace period: {args.days_grace} days after due date")
        print(f"  {'-' * (W - 4)}")

        for p in expired:
            deleted, kept, freed = cleanup_project(p["dir"], dry_run=not args.delete)
            freed_mb = freed / (1024 * 1024)
            total_freed += freed

            action = "DELETED" if args.delete else "would delete"
            print(f"    {p['name'][:40]:<40s}  {action} {len(deleted)} files ({freed_mb:.1f} MB)")
            if kept:
                print(f"      Kept: {', '.join(kept[:5])}")

        total_freed_mb = total_freed / (1024 * 1024)
        print(f"  {'-' * (W - 4)}")
        if args.delete:
            print(f"  FREED: {total_freed_mb:.1f} MB from {len(expired)} expired projects")
        else:
            print(f"  WOULD FREE: {total_freed_mb:.1f} MB from {len(expired)} expired projects")
            print(f"\n  Run with --delete to actually remove files.")
    else:
        print(f"\n  No expired projects to clean up (grace period: {args.days_grace} days)")

    if no_date:
        print(f"\n  NO DUE DATE ({len(no_date)} projects - skipped):")
        for p in no_date:
            print(f"    {p['name'][:45]:<45s}  {p['docs_size_mb']:>6.1f} MB")

    print("=" * W)


if __name__ == "__main__":
    main()
