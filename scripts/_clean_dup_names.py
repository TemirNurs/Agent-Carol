#!/usr/bin/env python3
"""One-off: clean project names that contain the 'X - Y - X - Y' double-concat
pattern. Updates active_bids.json AND CRM Bid Log rows in place."""
import json, re, sys
from pathlib import Path

try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

BASE = Path(r"C:/Agent Carol")
sys.path.insert(0, str(BASE / "scripts"))
from scrape_cc_inbox import clean_project_name
from crm_lib import get_sheet


def is_dup_name(s):
    if not s or " - " not in s:
        return False
    parts = s.split(" - ")
    if len(parts) < 3:
        return False
    # Check if "first - second - first - second" pattern
    return (parts[0].strip()[:12].lower() == parts[2].strip()[:12].lower())


# 1. active_bids.json
AB = BASE / "data" / "memory" / "active_bids.json"
bids = json.loads(AB.read_text(encoding="utf-8"))
ab_changed = 0
for b in bids:
    pn = b.get("project_name", "")
    if is_dup_name(pn):
        cleaned = clean_project_name(pn)
        if cleaned and cleaned != pn:
            print(f"  active_bids: {pn!r} → {cleaned!r}")
            b["project_name"] = cleaned
            ab_changed += 1
if ab_changed:
    AB.write_text(json.dumps(bids, indent=2), encoding="utf-8")
    print(f"[active_bids] cleaned {ab_changed} entries")
else:
    print("[active_bids] no changes needed")

# 2. CRM Bid Log
ws = get_sheet("Bid Log")
hdr = ws.row_values(1)
proj_c = hdr.index("Project Name") + 1
rows = ws.get_all_values()
import string
col_letter = ""
n = proj_c
while n > 0:
    n, r = divmod(n - 1, 26)
    col_letter = chr(65 + r) + col_letter

crm_updates = []
for r_idx, row in enumerate(rows[1:], start=2):
    if len(row) < proj_c: continue
    pn = row[proj_c - 1]
    if is_dup_name(pn):
        cleaned = clean_project_name(pn)
        if cleaned and cleaned != pn:
            print(f"  CRM row {r_idx}: {pn!r} → {cleaned!r}")
            crm_updates.append({"range": f"{col_letter}{r_idx}",
                                "values": [[cleaned]]})

if crm_updates:
    ws.batch_update(crm_updates, value_input_option="USER_ENTERED")
    print(f"[CRM] cleaned {len(crm_updates)} project names")
else:
    print("[CRM] no changes needed")
