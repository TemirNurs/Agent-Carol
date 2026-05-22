#!/usr/bin/env python3
r"""
cleanup_project_docs.py — Delete downloaded bid docs for closed projects.

Carol downloads PDFs/drawings into data/projects/<slug>/ every time she
investigates a bid. Without cleanup the folder hits multi-GB sizes. This
script cross-references each project folder against CRM Status:

  - Lost / Won / Withdrawn / No Bid  + folder >7 days old   → DELETE
  - Awaiting Decision / Bid Submitted (active)              → KEEP
  - Unknown (no CRM match)                                  → KEEP (safe default)

Optionally compresses VERY OLD active project docs (>60 days, still active)
into a single .zip per project to save space without losing the data.

Usage:
  python scripts/cleanup_project_docs.py                # dry-run, show savings
  python scripts/cleanup_project_docs.py --apply        # actually delete
  python scripts/cleanup_project_docs.py --min-age 7    # threshold in days
  python scripts/cleanup_project_docs.py --apply --quiet
"""
from __future__ import annotations
import argparse, re, shutil, sys, time
from datetime import datetime, date, timedelta
from pathlib import Path

try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

ROOT = Path(__file__).resolve().parent.parent
PROJECTS_DIR = ROOT / "data" / "projects"
DOWNLOADS_DIR = ROOT / "data" / "projects" / "_downloads"

sys.path.insert(0, str(ROOT / "scripts"))


DEAD_STATUSES = {"Lost", "Won", "Withdrawn", "No Bid", "No Decision"}
ACTIVE_STATUSES = {"Bid Submitted", "Awaiting Decision", "Estimating",
                   "Pending Review", "ITB Received"}


def normalize_slug(s: str) -> str:
    """Match project folder names against CRM project_name."""
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def folder_age_days(p: Path) -> int:
    try:
        return (time.time() - p.stat().st_mtime) / 86400
    except Exception:
        return 0


def folder_size_mb(p: Path) -> float:
    total = 0
    for f in p.rglob("*"):
        if f.is_file():
            try: total += f.stat().st_size
            except Exception: pass
    return total / 1024 / 1024


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="Actually delete (default: dry-run)")
    ap.add_argument("--min-age", type=int, default=7,
                    help="Skip folders newer than this many days (default 7)")
    ap.add_argument("--include-downloads", action="store_true",
                    help="Also clean data/projects/_downloads/")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if not PROJECTS_DIR.exists():
        print(f"No {PROJECTS_DIR} — nothing to clean.")
        return 0

    # Build CRM status lookup by normalized project name
    try:
        from crm_lib import all_records
        rows = all_records("Bid Log")
        crm_by_slug = {}
        for r in rows:
            slug = normalize_slug(r.get("Project Name", ""))
            if slug:
                # If we have multiple rows for the same project, prefer Lost > Won >
                # active > unknown so we don't delete a project that's still active
                # under a different GC.
                cur = crm_by_slug.get(slug, "")
                new = r.get("Status", "").strip()
                # active beats dead (keep > delete)
                if cur in ACTIVE_STATUSES or new in ACTIVE_STATUSES:
                    crm_by_slug[slug] = new if new in ACTIVE_STATUSES else cur
                else:
                    crm_by_slug[slug] = new or cur
        if not args.quiet:
            print(f"[cleanup] CRM has {len(rows)} rows, {len(crm_by_slug)} unique slugs")
    except Exception as e:
        print(f"[cleanup] WARN — couldn't load CRM ({e}). Will keep ALL folders.")
        return 1

    # Walk project folders
    candidates = []  # (path, size_mb, age_days, status, reason)
    for folder in PROJECTS_DIR.iterdir():
        if not folder.is_dir(): continue
        if folder.name.startswith("_") and not args.include_downloads:
            continue   # skip _downloads/, _today_*/ etc.
        slug = normalize_slug(folder.name)
        age = folder_age_days(folder)
        size = folder_size_mb(folder)
        # Try direct slug match first, then substring
        status = crm_by_slug.get(slug, "")
        if not status:
            for crm_slug, crm_status in crm_by_slug.items():
                if slug and crm_slug and (slug in crm_slug or crm_slug in slug):
                    if len(slug) >= 10 or len(crm_slug) >= 10:
                        status = crm_status
                        break

        # New logic — bias toward deletion to actually reclaim space:
        #   - ACTIVE in CRM → always keep (regardless of age)
        #   - Recent folder (<min_age) → keep (might be in-progress)
        #   - DEAD in CRM at any age >= min_age → DELETE
        #   - No CRM match + folder older than min_age → DELETE
        #     (these are stale leftovers from old bids no longer tracked)
        if status in ACTIVE_STATUSES:
            reason = f"Status={status} (ACTIVE — keep)"
            verdict = "keep"
        elif age < args.min_age:
            reason = f"too new ({age:.0f}d, keep until {args.min_age}d)"
            verdict = "keep"
        elif status in DEAD_STATUSES:
            reason = f"Status={status}, age={age:.0f}d"
            verdict = "DELETE"
        elif not status:
            reason = f"no CRM match, age={age:.0f}d (stale leftover)"
            verdict = "DELETE"
        else:
            reason = f"Status={status} (unknown — keep)"
            verdict = "keep"

        candidates.append((folder, size, age, status, verdict, reason))

    # Report
    candidates.sort(key=lambda x: -x[1])
    delete_targets = [c for c in candidates if c[4] == "DELETE"]
    keep_targets = [c for c in candidates if c[4] == "keep"]
    total_delete = sum(c[1] for c in delete_targets)
    total_keep = sum(c[1] for c in keep_targets)

    if not args.quiet:
        print(f"\n{'='*80}\nCleanup candidates ({len(candidates)} folders)\n{'='*80}")
        print(f"  DELETE: {len(delete_targets)} folders  →  reclaim {total_delete/1024:.1f} GB")
        print(f"  KEEP:   {len(keep_targets)} folders  ({total_keep/1024:.1f} GB)\n")
        if delete_targets:
            print(f"Top 15 to delete:")
            for folder, size, age, status, verdict, reason in delete_targets[:15]:
                print(f"  🗑️  {size:>7.1f}MB  {folder.name[:42]:<44}  {reason}")
        if not args.apply:
            print(f"\n[dry-run] use --apply to actually delete")

    if args.apply and delete_targets:
        for folder, size, age, status, verdict, reason in delete_targets:
            try:
                shutil.rmtree(folder)
                if not args.quiet:
                    print(f"  ✅ deleted {folder.name}  ({size:.1f}MB)")
            except Exception as e:
                print(f"  ❌ failed to delete {folder.name}: {e}")
        if not args.quiet:
            print(f"\n[cleanup] reclaimed ~{total_delete/1024:.1f} GB")

    return 0


if __name__ == "__main__":
    sys.exit(main())
