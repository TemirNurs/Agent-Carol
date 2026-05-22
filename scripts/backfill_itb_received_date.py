#!/usr/bin/env python3
r"""
backfill_itb_received_date.py — One-shot backfill of the ITB Received Date
column in CRM Bid Log for rows that have a blank/garbled value.

Matching priority:
  1. Internal ID → active_bids by ingested_at / email_date
  2. Project + GC fuzzy match → active_bids
  3. Bid Due Date - 21 days (default lead time) if nothing else available
  4. Today (last resort, so the row at least sorts somewhere sensible)

Usage:
  python scripts/backfill_itb_received_date.py             # dry-run
  python scripts/backfill_itb_received_date.py --apply     # write
"""
from __future__ import annotations
import argparse, json, re, sys
from datetime import datetime, date, timedelta
from pathlib import Path

try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "scripts"))
from crm_lib import get_sheet, workbook

ACTIVE = BASE / "data" / "memory" / "active_bids.json"


def _norm(s):
    if not s: return ""
    s = re.sub(r"#\s*\d+", "", str(s).lower())
    s = re.sub(r"[(),\-/_:]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _parse_any(s):
    if not s: return None
    s = str(s).strip()
    # ISO
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try: return datetime.strptime(s[:len(fmt)+2 if "T" in fmt else len(fmt)], fmt).date()
        except Exception: pass
    # RFC-2822 email date
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(s)
        return dt.date() if dt else None
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    bids = json.load(open(ACTIVE, encoding="utf-8"))
    # Build a lookup by normalized project name
    by_proj = {}
    for b in bids:
        key = _norm(b.get("project_name", ""))
        if not key: continue
        ts = _parse_any(b.get("email_date") or b.get("ingested_at"))
        if not ts: continue
        # Keep earliest invitation (first time we heard about it)
        if key not in by_proj or ts < by_proj[key]:
            by_proj[key] = ts

    ws = get_sheet("Bid Log")
    rows = ws.get_all_values()
    hdr = rows[0]
    proj_c = hdr.index("Project Name")
    itb_c  = hdr.index("ITB Received Date")
    due_c  = hdr.index("Bid Due Date")
    status_c = hdr.index("Status")

    MMDDYYYY = re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4}$")
    updates = []
    for r_idx, row in enumerate(rows[1:], start=2):
        if r_idx - 2 >= len(rows) - 1: break
        existing_itb = row[itb_c].strip() if len(row) > itb_c else ""
        # Re-normalize ANY non-MM/DD/YYYY text (RFC-2822 like "Mon, 18 Ma..." is
        # not sortable cleanly in Sheets — convert to standard form so the date
        # column is human-readable and Sheets recognizes it as a real date).
        if existing_itb and MMDDYYYY.match(existing_itb):
            continue  # already clean MM/DD/YYYY
        if existing_itb:
            d = _parse_any(existing_itb)
            if d:
                new_val = d.strftime("%m/%d/%Y")
                updates.append((r_idx, new_val, (row[proj_c][:40] if len(row)>proj_c else "")))
                continue
        proj = row[proj_c] if len(row) > proj_c else ""
        if not proj: continue
        key = _norm(proj)
        # 1. Direct match to active_bids
        d = by_proj.get(key)
        # 2. Substring / contains match
        if not d:
            for k, ts in by_proj.items():
                if (key in k or k in key) and len(k) > 6:
                    d = ts
                    break
        # 3. Due Date - 21 days
        if not d:
            due_str = row[due_c] if len(row) > due_c else ""
            due_d = _parse_any(due_str)
            if due_d:
                d = due_d - timedelta(days=21)
        if not d:
            continue
        new_val = d.strftime("%m/%d/%Y")
        updates.append((r_idx, new_val, proj[:40]))

    print(f"[backfill] {len(updates)} rows to update")
    for r_idx, val, proj in updates[:15]:
        print(f"  row {r_idx:>3}  {proj:<42}  → {val}")
    if len(updates) > 15:
        print(f"  ... and {len(updates) - 15} more")

    if not args.apply:
        print("\n[dry-run] use --apply to write")
        return

    # Batch update — one cell at a time would hit gspread quotas; use update with a list of ranges
    import gspread
    col_letter = ""
    n = itb_c + 1
    while n > 0:
        n, r = divmod(n - 1, 26)
        col_letter = chr(65 + r) + col_letter

    data = [{"range": f"{col_letter}{r_idx}", "values": [[val]]}
            for r_idx, val, _ in updates]
    # gspread batch_update accepts batches
    BATCH = 50
    for i in range(0, len(data), BATCH):
        ws.batch_update(data[i:i+BATCH], value_input_option="USER_ENTERED")
    print(f"[backfill] wrote {len(updates)} ITB Received Date values")


if __name__ == "__main__":
    main()
