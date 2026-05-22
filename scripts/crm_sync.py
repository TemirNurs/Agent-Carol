#!/usr/bin/env python3
"""
Sync Carol's bid pipeline with the CRM workbook (CRM-Bid-Log.xlsx).

The CRM is the source of truth for:
  - Bid status (Bid Submitted, Awaiting Decision, Lost, Won, Estimating, ITB Received)
  - Loss reasons + feedback
  - Final bid amounts + contract values
  - GC win rates + relationship status
  - Historical completed projects (informs production rates)

This script:
  1. Reads CRM workbook from a configurable location (default: ~/Downloads/CRM-Bid-Log.xlsx)
  2. Fuzzy-matches CRM "Project Name" against active_bids.json projects
  3. Writes overrides to data/memory/bid_status.json (consumed by bid_status.py)
  4. Writes GC win-rate overlay to data/memory/gc_crm.json
  5. Writes completed-projects history to data/memory/completed_projects.json
  6. Reports: matched / unmatched / new-in-Carol-not-in-CRM

Usage:
  python scripts/crm_sync.py                          # default path
  python scripts/crm_sync.py --file path/to/crm.xlsx
  python scripts/crm_sync.py --dry-run                # preview, don't write
  python scripts/crm_sync.py --auto                   # daemon mode (silent)
"""

import argparse
import difflib
import json
import re
import sys
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = Path(__file__).resolve().parent.parent
ACTIVE_BIDS  = BASE / "data" / "memory" / "active_bids.json"
STATUS_FILE  = BASE / "data" / "memory" / "bid_status.json"
GC_CRM       = BASE / "data" / "memory" / "gc_crm.json"
COMPLETED    = BASE / "data" / "memory" / "completed_projects.json"
DEFAULT_CRM  = Path.home() / "Downloads" / "CRM-Bid-Log (3).xlsx"

# Map CRM Status → Carol lifecycle status
CRM_STATUS_MAP = {
    "ITB Received":      "invited",
    "Reviewing":         "invited",
    "Declined":          "declined",
    "Estimating":        "docs_pulled",
    "Pending Review":    "estimate_done",
    "Bid Submitted":     "submitted",
    "Awaiting Decision": "submitted",
    "Lost":              "lost",
    "Won":               "won",
    "Awarded":           "won",
    "No Bid":            "no_bid",
    "Withdrawn":         "no_bid",
}


def normalize(s):
    """Aggressive normalize for fuzzy matching."""
    if not s: return ""
    s = s.lower()
    # Drop store numbers (#1234), parens, commas, dashes, slashes
    s = re.sub(r"#\s*\d+", "", s)
    s = re.sub(r"[(),\-/_]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def match_score(a, b):
    """Score 0.0-1.0 for how well two project names match."""
    na, nb = normalize(a), normalize(b)
    if not na or not nb: return 0.0
    base = difflib.SequenceMatcher(None, na, nb).ratio()
    # Bonus for shared distinctive tokens (length >= 5 chars)
    tokens_a = {t for t in na.split() if len(t) >= 5}
    tokens_b = {t for t in nb.split() if len(t) >= 5}
    if tokens_a and tokens_b:
        overlap = len(tokens_a & tokens_b) / max(len(tokens_a), len(tokens_b))
        base = (base + 0.5 * overlap) / 1.5
    # Bonus for shared store numbers
    nums_a = set(re.findall(r"\d{3,}", a or ""))
    nums_b = set(re.findall(r"\d{3,}", b or ""))
    if nums_a and nums_b and (nums_a & nums_b):
        base += 0.15
    return min(base, 1.0)


def read_crm(path=None):
    """Returns (bid_log_rows, completed_rows, gc_rows) as lists of dicts.
    Now reads from Google Sheets (path arg ignored, kept for compat).
    """
    sys.path.insert(0, str(BASE / "scripts"))
    from crm_lib import all_records

    bid_log = [r for r in all_records("Bid Log")
               if r.get("Bid #") and r.get("Project Name")]
    try:
        completed = [r for r in all_records("Completed Projects")
                     if r.get("Project Name")]
    except Exception:
        completed = []
    try:
        gcs = [r for r in all_records("GC Directory")
               if r.get("GC / Company")]
    except Exception:
        gcs = []
    return bid_log, completed, gcs


def slugify(name):
    if not name: return ""
    s = re.sub(r"[^a-z0-9\s-]", "", name.lower())
    s = re.sub(r"\s+", "-", s).strip("-")
    s = re.sub(r"-+", "-", s)
    return s[:80]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=str(DEFAULT_CRM), help="(legacy, ignored) Path to CRM xlsx")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--auto", action="store_true", help="Quiet mode for daemon use")
    ap.add_argument("--threshold", type=float, default=0.55, help="Fuzzy match threshold")
    args = ap.parse_args()

    bid_log, completed, gcs = read_crm()
    if not args.auto:
        print(f"[crm] Bid Log: {len(bid_log)} rows | Completed: {len(completed)} | GCs: {len(gcs)}")

    # Filter to bid log entries with actual status
    actionable = [r for r in bid_log if r.get("Status")]
    if not args.auto:
        print(f"[crm] {len(actionable)} CRM entries have a Status set")

    # Load Carol's active bids
    active_bids = json.load(open(ACTIVE_BIDS, encoding="utf-8"))

    # Fuzzy match each CRM row to an active bid
    matched, unmatched_crm = [], []
    used_carol_idx = set()
    for crm_row in actionable:
        crm_name = crm_row.get("Project Name", "")
        crm_gc   = crm_row.get("GC / Client", "") or ""
        best_idx, best_score = None, 0
        for i, ab in enumerate(active_bids):
            if i in used_carol_idx: continue
            s = match_score(crm_name, ab.get("project_name", ""))
            # GC bonus
            if crm_gc and ab.get("gc"):
                if normalize(crm_gc)[:8] == normalize(ab.get("gc"))[:8]:
                    s += 0.10
            if s > best_score:
                best_score, best_idx = s, i
        if best_score >= args.threshold and best_idx is not None:
            used_carol_idx.add(best_idx)
            matched.append((crm_row, active_bids[best_idx], best_score))
        else:
            unmatched_crm.append((crm_row, best_score))

    if not args.auto:
        print(f"\n=== MATCHED CRM → Carol ({len(matched)}) ===")
        for crm, ab, score in matched[:20]:
            crm_status = crm.get("Status", "?")
            mapped = CRM_STATUS_MAP.get(crm_status, crm_status.lower().replace(" ", "_"))
            print(f"  [{score:.2f}] {crm.get('Bid #')} {crm_status:<18} -> {mapped:<14} | CRM:{crm.get('Project Name','?')[:40]:40} | Carol:{ab.get('project_name','?')[:40]}")
        if len(matched) > 20:
            print(f"  ... + {len(matched)-20} more")

        print(f"\n=== UNMATCHED CRM entries ({len(unmatched_crm)}) ===")
        print(f"  These exist in CRM but not in Carol's active_bids — historical or different naming")
        for crm, score in unmatched_crm[:10]:
            print(f"  [{score:.2f}] {crm.get('Bid #'):<10} {crm.get('Status','?'):<18} {crm.get('Project Name','?')[:60]}")

    # Build override file for bid_status.py
    if STATUS_FILE.exists():
        try:
            override_data = json.load(open(STATUS_FILE, encoding="utf-8"))
        except Exception:
            override_data = {"overrides": {}, "history": []}
    else:
        override_data = {"overrides": {}, "history": []}

    overrides = override_data.setdefault("overrides", {})
    crm_overrides_added = 0
    for crm, ab, score in matched:
        carol_slug = slugify(ab.get("project_name", ""))
        if not carol_slug: continue
        crm_status = crm.get("Status", "")
        carol_status = CRM_STATUS_MAP.get(crm_status, crm_status.lower().replace(" ", "_"))
        amount = crm.get("Bid Amount ($)") or crm.get("Contract Value ($)")
        try: amount = int(amount) if amount else None
        except: amount = None
        overrides[carol_slug] = {
            "status": carol_status,
            "amount": amount,
            "reason": f"CRM sync: {crm.get('Bid #')} status={crm_status}",
            "loss_reason": crm.get("Loss Reason"),
            "submitted_date": str(crm.get("Bid Submitted Date") or ""),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "crm_bid_id": crm.get("Bid #"),
        }
        crm_overrides_added += 1

    # GC overlay
    gc_overlay = {}
    for g in gcs:
        name = g.get("GC / Company", "")
        if not name: continue
        gc_overlay[name] = {
            "primary_contact": g.get("Primary Contact"),
            "email": g.get("Email"),
            "phone": g.get("Phone"),
            "city": g.get("City"),
            "state": g.get("State"),
            "total_bids": g.get("Total Bids"),
            "wins": g.get("Wins"),
            "win_rate": g.get("Win Rate"),
            "total_revenue": g.get("Total Revenue ($)"),
            "relationship_status": g.get("Relationship Status"),
            "last_contact_date": str(g.get("Last Contact Date") or ""),
            "notes": g.get("Notes"),
        }

    # Completed projects
    completed_overlay = []
    for c in completed:
        completed_overlay.append({
            "project_name": c.get("Project Name", "").strip() if c.get("Project Name") else "",
            "year": c.get("Year"),
            "city": c.get("City"),
            "state": c.get("State"),
            "gc": c.get("GC / Client"),
            "contract_value": c.get("Contract Value ($)"),
            "scope": c.get("Scope of Work"),
            "facility_type": c.get("Facility Type"),
            "notes": c.get("Notes"),
        })

    if args.dry_run:
        if not args.auto:
            print(f"\n[dry-run] Would write:")
            print(f"  - {crm_overrides_added} status overrides to {STATUS_FILE.name}")
            print(f"  - {len(gc_overlay)} GCs to {GC_CRM.name}")
            print(f"  - {len(completed_overlay)} completed projects to {COMPLETED.name}")
        return

    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    json.dump(override_data, open(STATUS_FILE, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    json.dump(gc_overlay, open(GC_CRM, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    json.dump(completed_overlay, open(COMPLETED, "w", encoding="utf-8"), indent=2, ensure_ascii=False)

    if not args.auto:
        print(f"\n[crm] Wrote {crm_overrides_added} status overrides")
        print(f"[crm] Wrote {len(gc_overlay)} GC records to gc_crm.json")
        print(f"[crm] Wrote {len(completed_overlay)} completed projects")
    else:
        print(f"crm_sync: {crm_overrides_added} statuses, {len(gc_overlay)} GCs, {len(completed_overlay)} completed projects")


if __name__ == "__main__":
    main()
