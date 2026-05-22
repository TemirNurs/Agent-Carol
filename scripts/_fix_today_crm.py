#!/usr/bin/env python3
"""Fix today's CRM mess:
  1. Delete 3 duplicate #2235 Quinton rows (BID-0091/0092/0093) caused by
     _project_core dedupe bug
  2. Mark CONFIRMED LOST bids: Chewy (Brian) + Tanner's #2219/#2235 (4 rows)
  3. Add Carvana intel note (Brian said FRP high — pending owner)
  4. Don't touch #1602 Chesterfield / #2541 Chester — Tanner didn't confirm
     those, my brief over-classified them
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(r"C:/Agent Carol/scripts")))
from crm_lib import get_sheet, workbook
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ws = get_sheet("Bid Log")
hdr = ws.row_values(1)
rows = ws.get_all_values()
H = {h: i for i, h in enumerate(hdr)}

# Build {Internal ID: row_index} since Bid# may shift
by_iid = {}
by_bid_id = {}  # for verification labels only
for i, r in enumerate(rows[1:], start=2):
    iid = r[H["Internal ID"]] if "Internal ID" in H and len(r) > H["Internal ID"] else ""
    bid_id = r[H["Bid #"]] if len(r) > H["Bid #"] else ""
    if iid: by_iid[iid] = i
    if bid_id: by_bid_id[bid_id] = (i, iid)

# 1. Delete BID-0091/0092/0093 (duplicates)
to_delete_row_indices = []
for label in ("BID-0091", "BID-0092", "BID-0093"):
    if label in by_bid_id:
        ri, _ = by_bid_id[label]
        to_delete_row_indices.append(ri)
        print(f"  Will delete row {ri}: {label} = {rows[ri-1][H['Project Name']]}")
# Delete from bottom-up so indices stay valid
import time
sheet_id = ws.id
wb = workbook()
if to_delete_row_indices:
    requests = []
    for ri in sorted(to_delete_row_indices, reverse=True):
        requests.append({
            "deleteDimension": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "ROWS",
                    "startIndex": ri - 1,   # 0-indexed
                    "endIndex": ri,
                }
            }
        })
    wb.batch_update({"requests": requests})
    print(f"  Deleted {len(to_delete_row_indices)} duplicate row(s)")

# Re-read after deletion
time.sleep(2)
rows = ws.get_all_values()
by_iid.clear(); by_bid_id.clear()
for i, r in enumerate(rows[1:], start=2):
    iid = r[H["Internal ID"]] if "Internal ID" in H and len(r) > H["Internal ID"] else ""
    bid_id = r[H["Bid #"]] if len(r) > H["Bid #"] else ""
    if iid: by_iid[iid] = i
    if bid_id: by_bid_id[bid_id] = (i, iid)

# 2. Confirmed-LOST updates — set Status, Win/Loss, Loss Reason, Notes
import re
LOST_UPDATES = [
    # (matcher_function, loss_reason, source_msg_excerpt)
    (lambda r: "Chewy Vet Care" in r[H["Project Name"]] and "pkwycon.com" in r[H["Contact Email"]],
     "Wrong scope — bid floor tile, missed wall tile",
     "Brian Richardson 5/21: 'incorrect flooring scope... did not consider your bid'"),
    (lambda r: "2219" in r[H["Project Name"]] and "fiicgc.com" in r[H["Contact Email"]],
     "Not awarded per Farris (Tanner)",
     "Tanner Barber 5/14: 'We were not awarded these projects' (re: #2219 + #2235 Quinton)"),
    (lambda r: "2235" in r[H["Project Name"]] and "fiicgc.com" in r[H["Contact Email"]],
     "Not awarded per Farris (Tanner)",
     "Tanner Barber 5/14: 'We were not awarded these projects' (re: #2219 + #2235 Quinton)"),
]

# 3. Carvana intel note (NOT lost — pending with intel)
CARVANA_NOTE = ("Brian Richardson 5/21 intel: 'FRP was high. Painting & wall covering "
                "were competitive. No owner result yet.' → revise FRP if it goes to "
                "another round; painting/WC OK")

updates = []  # list of {range, values}
def col_letter(name):
    idx = H[name] + 1
    letter = ""
    while idx > 0:
        idx, r = divmod(idx - 1, 26)
        letter = chr(65 + r) + letter
    return letter

LST_COL = col_letter("Status")
WL_COL = col_letter("Win/Loss")
LR_COL = col_letter("Loss Reason")
NT_COL = col_letter("Notes")

now = "5/22/2026"
for r_idx, row in enumerate(rows[1:], start=2):
    if len(row) < len(hdr): continue
    proj = row[H["Project Name"]]
    email = row[H["Contact Email"]] or ""
    bid_id = row[H["Bid #"]]
    cur_status = row[H["Status"]]
    cur_notes = row[H["Notes"]] or ""

    for matcher, lr, src in LOST_UPDATES:
        if matcher(row):
            if cur_status != "Lost":
                updates.append({"range": f"{LST_COL}{r_idx}", "values": [["Lost"]]})
                updates.append({"range": f"{WL_COL}{r_idx}", "values": [["LOSS"]]})
                updates.append({"range": f"{LR_COL}{r_idx}", "values": [[lr]]})
                new_notes = (f"{cur_notes}\n[{now}] {src}".strip()
                             if cur_notes.strip() else f"[{now}] {src}")
                updates.append({"range": f"{NT_COL}{r_idx}", "values": [[new_notes]]})
                print(f"  LOST  row {r_idx}  {bid_id}  {proj[:40]}  → {lr}")
            break

    # Carvana intel note
    if "Carvana" in proj and "pkwycon.com" in email and CARVANA_NOTE not in cur_notes:
        new_notes = (f"{cur_notes}\n[{now}] {CARVANA_NOTE}".strip()
                     if cur_notes.strip() else f"[{now}] {CARVANA_NOTE}")
        updates.append({"range": f"{NT_COL}{r_idx}", "values": [[new_notes]]})
        print(f"  NOTE  row {r_idx}  {bid_id}  {proj[:40]}  → Carvana FRP intel")

if updates:
    BATCH = 50
    for i in range(0, len(updates), BATCH):
        ws.batch_update(updates[i:i+BATCH], value_input_option="USER_ENTERED")
    print(f"  Wrote {len(updates)} cell updates")

print("\nDone. Now re-sorting...")
import subprocess
r = subprocess.run([sys.executable, "scripts/apply_crm_formatting.py", "--apply-sort"],
                   cwd=r"C:/Agent Carol", capture_output=True, text=True, encoding="utf-8")
print(r.stdout[-400:])
