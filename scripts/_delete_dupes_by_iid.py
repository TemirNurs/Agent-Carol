#!/usr/bin/env python3
"""Delete duplicate CRM rows BY INTERNAL ID (never Bid#).
A through F per the audit. G (AMC scopes) and H (Savers) stay — different bids.

Also adds Tanner's 5/12 intel to the kept #2541 row."""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(r"C:/Agent Carol/scripts")))
from crm_lib import get_sheet, workbook
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Internal IDs to DELETE (duplicates to remove)
DELETE_IIDS = [
    ("686d31ed", "A) 1602 Chesterfield Farris dup (newer naming)"),
    ("04c70a5c", "B) #2219 Quinton Farris bids@ alt-inbox dup"),
    ("3ceb70cf", "C) #2235 Quinton Farris bids@ alt-inbox dup"),
    ("13344b98", "D) #2235 Quinton Salcoa dup (newer naming)"),
    ("30064386", "E) #2235 Quinton RCS today's dup (keep Withdrawn row)"),
    ("6b142354", "F) #2541 Chester Farris dup — keep Awaiting Decision row"),
]

# Internal ID to UPDATE with intel
INTEL_UPDATES = [
    ("5791611f", "Notes", "[5/22/2026] Tanner Barber 5/12 reply: 'We are currently waiting on Food Lion to respond' — kept as Awaiting Decision"),
]

ws = get_sheet("Bid Log")
hdr = ws.row_values(1)
rows = ws.get_all_values()
H = {h: i for i, h in enumerate(hdr)}
sheet_id = ws.id
wb = workbook()

# Find row index by Internal ID
iid_to_row = {}
for i, r in enumerate(rows[1:], start=2):
    iid = r[H["Internal ID"]] if H["Internal ID"] < len(r) else ""
    if iid:
        iid_to_row[iid] = i
        # also short-form lookup
        iid_to_row[iid[:8]] = i

def col_letter(idx):
    n = idx + 1; out = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out

# 1. Apply Notes update first (before row indices shift from deletions)
for iid_short, col_name, note_text in INTEL_UPDATES:
    ri = iid_to_row.get(iid_short)
    if ri is None:
        print(f"  ⚠️  Notes target iid={iid_short} not found")
        continue
    col = col_letter(H[col_name])
    existing_note = rows[ri-1][H[col_name]] if H[col_name] < len(rows[ri-1]) else ""
    new_note = (f"{existing_note}\n{note_text}".strip() if existing_note.strip() else note_text)
    ws.update_acell(f"{col}{ri}", new_note)
    print(f"  📝 iid={iid_short} row {ri}: added intel note")

time.sleep(2)

# 2. Build delete requests — collect rows by IID, sort DESC by row index
to_delete = []
for iid_short, label in DELETE_IIDS:
    ri = iid_to_row.get(iid_short)
    if ri is None:
        print(f"  ⚠️  iid={iid_short} ({label}) NOT FOUND — skipping")
        continue
    row_data = rows[ri-1]
    proj = row_data[H["Project Name"]] if H["Project Name"] < len(row_data) else ""
    gc = row_data[H["GC / Client"]] if H["GC / Client"] < len(row_data) else ""
    email = row_data[H["Contact Email"]] if H["Contact Email"] < len(row_data) else ""
    print(f"  🗑️  DELETING iid={iid_short} row {ri}: {proj[:40]} / {gc[:25]} / {email[:30]}  — {label}")
    to_delete.append((ri, iid_short, label))

# Sort DESC so deletion indices stay valid
to_delete.sort(key=lambda x: -x[0])
if to_delete:
    requests = []
    for ri, iid, label in to_delete:
        requests.append({
            "deleteDimension": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "ROWS",
                    "startIndex": ri - 1,
                    "endIndex": ri,
                }
            }
        })
    wb.batch_update({"requests": requests})
    print(f"\n  ✅ Deleted {len(to_delete)} duplicate row(s) by Internal ID")
else:
    print("\n  ⚠️  No rows deleted")

# 3. Re-sort
print("\nRe-applying sort...")
import subprocess
r = subprocess.run([sys.executable, "scripts/apply_crm_formatting.py", "--apply-sort"],
                   cwd=r"C:/Agent Carol", capture_output=True, text=True, encoding="utf-8")
# Print last 6 lines
for line in r.stdout.splitlines()[-6:]:
    print(f"  {line}")
