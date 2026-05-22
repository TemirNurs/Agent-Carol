#!/usr/bin/env python3
"""Restore the 3 Target rows I wrongly deleted (Bid# row-formula bit me).
Data recovered from user's screenshot + active_bids history."""
import sys, uuid
from pathlib import Path
sys.path.insert(0, str(Path(r"C:/Agent Carol/scripts")))
from crm_lib import get_sheet
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ws = get_sheet("Bid Log")
hdr = ws.row_values(1)

# 3 rows to restore — data from the user's screenshot of the CRM
RESTORE = [
    {
        "Project Name": "Target Denham Springs",
        "City": "Denham Springs", "State": "AL",
        "Facility Type": "Retail / Big Box",
        "GC / Client": "Williams Company",
        "Contact Name": "Ruben Morhain", "Contact Email": "rmorhain@williamsco.com",
        "Contact Phone": "407-295-2530",
        "Bid Source": "Invitation (GC)",
        "Bid Submitted Date": "02/11/2026",
        "Bid Amount ($)": "$269,960",
        "Status": "Lost",
        "Win/Loss": "LOSS",
        "Internal ID": str(uuid.uuid4()),
        "Notes": "[5/22/2026] Restored after accidental deletion (used Bid# as key, see AGENTS_LESSONS.md R1)",
    },
    {
        "Project Name": "Target Lincolton NC",
        "City": "Lincolnton", "State": "NC",
        "Facility Type": "Retail / Big Box",
        "GC / Client": "LF Jennings",
        "Contact Name": "Dan Ahles", "Contact Email": "dahles@lfjennings.com",
        "Contact Phone": "919-830-6466",
        "Bid Source": "Invitation (GC)",
        "Bid Submitted Date": "02/02/2026",
        "Bid Amount ($)": "$269,500",
        "Status": "Lost",
        "Win/Loss": "LOSS",
        "Internal ID": str(uuid.uuid4()),
        "Notes": "[5/22/2026] Restored after accidental deletion (used Bid# as key, see AGENTS_LESSONS.md R1)",
    },
    {
        "Project Name": "Target Winston Salem",
        "City": "Winston Salem", "State": "NC",
        "Facility Type": "Retail / Big Box",
        "GC / Client": "LF Jennings",
        "Contact Name": "Dan Ahles", "Contact Email": "dahles@lfjennings.com",
        "Contact Phone": "919-830-6466",
        "Bid Source": "Invitation (GC)",
        "Bid Submitted Date": "01/15/2026",
        "Bid Amount ($)": "$160,973",
        "Status": "Lost",
        "Win/Loss": "LOSS",
        "Internal ID": str(uuid.uuid4()),
        "Notes": "[5/22/2026] Restored after accidental deletion (used Bid# as key, see AGENTS_LESSONS.md R1)",
    },
]

# Build rows in header order
for rec in RESTORE:
    row_values = [rec.get(h, "") for h in hdr]
    ws.append_row(row_values, value_input_option="USER_ENTERED")
    print(f"  Restored: {rec['Project Name']}  ${rec['Bid Amount ($)']}")

print(f"\nRestored {len(RESTORE)} Target rows.")
