#!/usr/bin/env python3
"""Resolve Savers Thrift duplicate. Same project bid by 2 Delauter PMs:
  - jhibbard@delauterinc.com (responded; confirmed Delauter LOST 5/14)
  - aklem@delauterinc.com (never responded)

Keep jhibbard row (has the intel + Lost status).
Delete aklem row.
Add A. Klem as secondary contact reference in kept row's Notes.
"""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(r"C:/Agent Carol/scripts")))
from crm_lib import get_sheet, workbook
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Same convention as before — Internal IDs (never Bid#).
KEEP_IID   = "5d7fd051"  # jhibbard@delauterinc.com, Lost
DELETE_IID = "17f91fe4"  # aklem@delauterinc.com, Bid Submitted (duplicate)

ws = get_sheet("Bid Log")
hdr = ws.row_values(1)
rows = ws.get_all_values()
H = {h: i for i, h in enumerate(hdr)}

iid_to_row = {}
for i, r in enumerate(rows[1:], start=2):
    iid = r[H["Internal ID"]] if H["Internal ID"] < len(r) else ""
    if iid:
        iid_to_row[iid] = i
        iid_to_row[iid[:8]] = i

def col_letter(idx):
    n = idx + 1; out = ""
    while n > 0:
        n, r = divmod(n - 1, 26); out = chr(65 + r) + out
    return out

# 1. Add A. Klem contact note to kept row
ri_keep = iid_to_row.get(KEEP_IID)
if ri_keep:
    notes_col = col_letter(H["Notes"])
    existing = rows[ri_keep-1][H["Notes"]] if H["Notes"] < len(rows[ri_keep-1]) else ""
    addendum = ("[5/22/2026] Same project also bid to A. Klem <aklem@delauterinc.com> "
                "(no response received). Justin Hibbard (PM) confirmed Delauter lost "
                "on 5/14: 'DeLauter was not awarded this project.'")
    new_notes = (f"{existing}\n{addendum}".strip() if existing.strip() else addendum)
    ws.update_acell(f"{notes_col}{ri_keep}", new_notes)
    print(f"  📝 Added intel + secondary contact to KEPT row {ri_keep} (iid={KEEP_IID})")
else:
    print(f"  ⚠️  KEEP iid={KEEP_IID} not found — abort.")
    sys.exit(1)

time.sleep(2)

# 2. Delete the duplicate
ri_del = iid_to_row.get(DELETE_IID)
if ri_del is None:
    print(f"  ⚠️  DELETE iid={DELETE_IID} not found — nothing to delete.")
    sys.exit(0)

row_data = rows[ri_del-1]
proj = row_data[H["Project Name"]] if H["Project Name"] < len(row_data) else ""
email = row_data[H["Contact Email"]] if H["Contact Email"] < len(row_data) else ""
print(f"  🗑️  Deleting row {ri_del}: {proj} / {email}")

sheet_id = ws.id
wb = workbook()
wb.batch_update({"requests": [{
    "deleteDimension": {
        "range": {
            "sheetId": sheet_id,
            "dimension": "ROWS",
            "startIndex": ri_del - 1,
            "endIndex": ri_del,
        }
    }
}]})
print(f"  ✅ Deleted A. Klem duplicate Savers row")

# 3. Resort
print("\nRe-sorting...")
import subprocess
r = subprocess.run([sys.executable, "scripts/apply_crm_formatting.py", "--apply-sort"],
                   cwd=r"C:/Agent Carol", capture_output=True, text=True, encoding="utf-8")
for line in r.stdout.splitlines()[-4:]:
    print(f"  {line}")
