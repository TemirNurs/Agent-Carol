#!/usr/bin/env python3
"""One-shot: move yesterday's FU stamps to the correct column based on age."""
import sys
from datetime import date, datetime

sys.path.insert(0, "scripts")
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

from crm_lib import get_sheet, batch_update_rows


def correct_fu_col(age):
    if age < 7:    return "FU1 Date"
    if age < 30:   return "FU2 Date"
    if age < 90:   return "FU3 Date"
    return "FU4 Date"


def parse_date(s):
    for fmt in ("%a, %d %b %Y", "%m/%d/%Y", "%Y-%m-%d"):
        try: return datetime.strptime(str(s).strip()[:30], fmt).date()
        except Exception: pass
    return None


def main():
    sh = get_sheet("Bid Log")
    hdrs = sh.row_values(1)
    rows = sh.get_all_values()
    send_date = date(2026, 5, 11)
    moves = []
    for ri, r in enumerate(rows, start=1):
        if ri == 1:
            continue
        d = {h: (r[i] if i < len(r) else "") for i, h in enumerate(hdrs)}
        bid_id = d.get("Bid #", "").strip()
        if not bid_id or not d.get("Project Name", "").strip():
            continue
        sub = parse_date(d.get("Bid Submitted Date", ""))
        if not sub:
            continue
        for fu_col in ("FU1 Date", "FU2 Date", "FU3 Date", "FU4 Date"):
            val = (d.get(fu_col, "") or "").strip()
            if val == "05/11/2026":
                age_at_send = (send_date - sub).days
                correct = correct_fu_col(age_at_send)
                if correct != fu_col:
                    moves.append({"row": ri, "bid": bid_id, "from": fu_col,
                                  "to": correct, "age": age_at_send})
                break
    print(f"Rows needing FU column correction: {len(moves)}")
    for m in moves:
        print(f"  Row {m['row']:>3} {m['bid']:<10} age={m['age']:>2}d  "
              f"{m['from']} -> {m['to']}")
    if moves:
        updates = []
        for m in moves:
            updates.append((m["row"], m["from"], ""))
            updates.append((m["row"], m["to"], "05/11/2026"))
        batch_update_rows("Bid Log", updates)
        print(f"\nApplied {len(updates)} cell changes ({len(moves)} rows moved).")
    else:
        print("No corrections needed.")


if __name__ == "__main__":
    main()
