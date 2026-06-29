#!/usr/bin/env python3
"""Backfill City/State/Facility Type/Due Date on rows 62-75 by copying
from a related existing CRM row for the same project (different GC)."""
import sys
import re

sys.path.insert(0, "scripts")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from crm_lib import get_sheet, batch_update_rows
from crm_writeback import match_score

sh = get_sheet("Bid Log")
hdrs = sh.row_values(1)
rows = sh.get_all_values()

EXISTING = []
TARGET = []
for r_idx, row in enumerate(rows, start=1):
    if r_idx == 1:
        continue
    d = {h: (row[i] if i < len(row) else "") for i, h in enumerate(hdrs)}
    bid = d.get("Bid #", "").strip()
    proj = d.get("Project Name", "").strip()
    if not bid or not proj:
        continue
    if 62 <= r_idx <= 75:
        TARGET.append({"row_idx": r_idx, "data": d})
    else:
        EXISTING.append({"row_idx": r_idx, "data": d})

print(f"TARGET: {len(TARGET)} | EXISTING: {len(EXISTING)}")
print()

COPY_FIELDS = [
    "City", "State", "Facility Type",
    "Bid Due Date", "ITB Received Date",
    "Bid Amount ($)",   # copy when target is blank (same project to a different
                        # contact at same/different GC usually has same amount)
]
updates = []
for t in TARGET:
    t_proj = t["data"].get("Project Name", "")
    t_bid = t["data"]["Bid #"]
    best, best_score = None, 0
    for e in EXISTING:
        s = match_score(t_proj, e["data"].get("Project Name", ""))
        if s > best_score:
            best_score, best = s, e
    if not best or best_score < 0.40:
        print(f"  {t_bid} {t_proj[:35]:<35} -> no related row (best={best_score:.2f})")
        continue
    changes = []
    for f in COPY_FIELDS:
        target_val = (t["data"].get(f) or "").strip()
        source_val = (best["data"].get(f) or "").strip()
        if not target_val and source_val:
            updates.append((t["row_idx"], f, source_val))
            changes.append(f"{f}={source_val[:18]}")
    if changes:
        src_bid = best["data"]["Bid #"]
        print(f"  {t_bid} {t_proj[:30]:<30} <- {src_bid} ({best_score:.2f}): {', '.join(changes)}")
    else:
        print(f"  {t_bid} {t_proj[:30]:<30} (already has fields filled)")

print(f"\n{len(updates)} cell updates queued. Applying...")
if updates:
    batch_update_rows("Bid Log", updates)
print("Done.")
