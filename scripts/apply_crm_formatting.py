#!/usr/bin/env python3
r"""
apply_crm_formatting.py - Color-code the CRM Bid Log + add a sort-priority
column so active bids sort to the TOP and dead ones drop to the bottom.

Rules applied:

ROW BACKGROUND (whole row based on Status):
  Bid Submitted        → light blue   (in-flight, awaiting GC response)
  Awaiting Decision    → light yellow (waiting on GC award decision)
  Won                  → light green  (we won)
  Lost                 → light red    (we lost)
  Withdrawn / No Bid / No Decision → light gray (dead)

DAYS UNTIL DUE (column AF):
  ≤ 0  (today or overdue) → red bg, white text
  1-2  days               → orange bg
  3-7  days               → yellow bg
  8+   days               → green bg, dim text

WIN/LOSS (column S):
  WIN   → bold green
  LOSS  → bold red
  PENDING → italic gray

Sort priority (new column added):
  0  active in-flight (Bid Submitted, Awaiting Decision)
  1  unknown/empty Status
  2  dead (Won, Lost, Withdrawn, No Bid, No Decision)

After this script runs, you sort by [Sort Priority ASC, Bid Submitted Date DESC]
and active bids appear at the top, dead bids fall to the bottom.

Usage:
  python scripts/apply_crm_formatting.py
  python scripts/apply_crm_formatting.py --apply-sort   # also sort the sheet now
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# Colors (Google Sheets RGB 0-1 floats)
COLORS = {
    "submitted":     {"red": 0.85, "green": 0.92, "blue": 0.99},   # light blue
    "awaiting":      {"red": 1.0,  "green": 0.97, "blue": 0.80},   # light yellow
    "won":           {"red": 0.83, "green": 0.96, "blue": 0.83},   # light green
    "lost":          {"red": 0.98, "green": 0.83, "blue": 0.83},   # light red/pink
    "dead_gray":     {"red": 0.90, "green": 0.90, "blue": 0.90},   # light gray
    "overdue_red":   {"red": 0.91, "green": 0.30, "blue": 0.24},   # strong red
    "due_orange":    {"red": 0.99, "green": 0.75, "blue": 0.41},   # orange
    "due_yellow":    {"red": 1.00, "green": 0.93, "blue": 0.55},   # yellow
    "due_green":     {"red": 0.78, "green": 0.92, "blue": 0.78},   # mild green
    "win_text":      {"red": 0.06, "green": 0.45, "blue": 0.13},   # dark green
    "loss_text":     {"red": 0.70, "green": 0.10, "blue": 0.10},   # dark red
    "pending_gray":  {"red": 0.50, "green": 0.50, "blue": 0.50},   # gray
    "white_text":    {"red": 1, "green": 1, "blue": 1},
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply-sort", action="store_true",
                    help="Also re-sort the sheet so active bids are on top")
    args = ap.parse_args()

    from crm_lib import get_sheet, workbook, _retry

    sh = get_sheet("Bid Log")
    hdrs = sh.row_values(1)
    n_rows = len(sh.get_all_values()) - 1
    sheet_id = sh.id
    wb = workbook()

    # 1. Ensure 'Sort Priority' column exists, then ALWAYS (re)fill its formula
    # across EVERY data row. Critical: crm_writeback appends new rows without
    # this formula, so we must re-extend it on every run or appended rows get
    # a blank Sort Priority and sort to the very bottom (the bug the user hit).
    if "Sort Priority" not in hdrs:
        new_col_idx = len(hdrs) + 1
        new_letter = ""
        n = new_col_idx
        while n > 0:
            n, r = divmod(n - 1, 26)
            new_letter = chr(65 + r) + new_letter
        if sh.col_count < new_col_idx:
            _retry(sh.resize, rows=sh.row_count, cols=new_col_idx)
        _retry(sh.update_cell, 1, new_col_idx, "Sort Priority")
        hdrs.append("Sort Priority")
        print(f"Added 'Sort Priority' column at {new_letter}")

    sp_idx = hdrs.index("Sort Priority") + 1
    sp_letter = chr(64 + sp_idx) if sp_idx <= 26 else "A" + chr(64 + (sp_idx - 1) // 26 - 1) + chr(65 + (sp_idx - 1) % 26)
    # robust A1 for sp
    sp_letter = ""
    _n = sp_idx
    while _n > 0:
        _n, _r = divmod(_n - 1, 26)
        sp_letter = chr(65 + _r) + sp_letter
    status_idx = hdrs.index("Status") + 1
    status_letter = ""
    _n = status_idx
    while _n > 0:
        _n, _r = divmod(_n - 1, 26)
        status_letter = chr(65 + _r) + status_letter
    # Sort priority buckets — CRM contains submitted bids only.
    #   0 = Bid Submitted / Awaiting Decision (active pipeline)
    #   1 = anything else (blank Status, oddities)
    #   2 = Won / Lost / Withdrawn / No Bid / No Decision (dead)
    formulas = []
    for r_row in range(2, n_rows + 2):
        f = (f'=IF(OR({status_letter}{r_row}="Bid Submitted",'
             f'{status_letter}{r_row}="Awaiting Decision"),0,'
             f'IF(OR({status_letter}{r_row}="Won",{status_letter}{r_row}="Lost",'
             f'{status_letter}{r_row}="Withdrawn",{status_letter}{r_row}="No Bid",'
             f'{status_letter}{r_row}="No Decision"),2,1))')
        formulas.append([f])
    _retry(sh.update, f"{sp_letter}2:{sp_letter}{n_rows+1}", formulas,
           value_input_option="USER_ENTERED")
    print(f"Sort Priority formula (re)applied to all {n_rows} data rows (col {sp_letter})")

    # Re-read headers
    hdrs = sh.row_values(1)
    n_cols = len(hdrs)
    last_col_letter = ""
    n = n_cols
    while n > 0:
        n, r = divmod(n - 1, 26)
        last_col_letter = chr(65 + r) + last_col_letter

    print(f"Bid Log dimensions: {n_rows} data rows × {n_cols} cols (A:{last_col_letter})")

    # 2. Clear ALL existing conditional format rules (clean slate)
    print("Clearing existing conditional formatting...")
    meta = wb.fetch_sheet_metadata({})
    sheets_meta = meta.get("sheets", [])
    existing_rule_count = 0
    for sm in sheets_meta:
        if sm.get("properties", {}).get("sheetId") == sheet_id:
            existing_rule_count = len(sm.get("conditionalFormats", []))
            break
    print(f"  Found {existing_rule_count} existing rules — clearing")
    if existing_rule_count > 0:
        # Delete from index 0 repeatedly — each delete shifts remaining rules
        # down, so always delete index 0 N times.
        clear_requests = [{"deleteConditionalFormatRule": {"sheetId": sheet_id, "index": 0}}
                          for _ in range(existing_rule_count)]
        wb.batch_update({"requests": clear_requests})

    # 3. Build new conditional format rules
    status_col = hdrs.index("Status")    # 0-based for API
    days_due_col = hdrs.index("Days Until Due") if "Days Until Due" in hdrs else None
    winloss_col = hdrs.index("Win/Loss") if "Win/Loss" in hdrs else None

    full_row_range = {
        "sheetId": sheet_id,
        "startRowIndex": 1,   # row 2 (skip header)
        "endRowIndex": n_rows + 1,
        "startColumnIndex": 0,
        "endColumnIndex": n_cols,
    }
    status_letter = chr(65 + status_col)
    # A1 style requires capital letter; status_col 15 (P) for "Status"
    if status_col >= 26:
        status_letter = chr(65 + (status_col // 26) - 1) + chr(65 + (status_col % 26))

    def custom_formula_rule(formula, bg_color, text_color=None):
        cf = {
            "ranges": [full_row_range],
            "booleanRule": {
                "condition": {
                    "type": "CUSTOM_FORMULA",
                    "values": [{"userEnteredValue": formula}],
                },
                "format": {"backgroundColor": bg_color},
            },
        }
        if text_color:
            cf["booleanRule"]["format"]["textFormat"] = {"foregroundColor": text_color}
        return {"addConditionalFormatRule": {"rule": cf, "index": 0}}

    # Row-background rules (applied in order — first match wins for each cell,
    # but since we use whole-row ranges, order matters — put Won/Lost FIRST so
    # they override Awaiting/Submitted which only apply if rows are still active)
    rules = []
    # Won
    rules.append(custom_formula_rule(
        f'=$P2="Won"',  # P is Status col (16th col = index 15)
        COLORS["won"]
    ))
    # Lost
    rules.append(custom_formula_rule(
        f'=$P2="Lost"',
        COLORS["lost"]
    ))
    # Withdrawn / No Bid / No Decision
    rules.append(custom_formula_rule(
        f'=OR($P2="Withdrawn",$P2="No Bid",$P2="No Decision")',
        COLORS["dead_gray"]
    ))
    # Awaiting Decision
    rules.append(custom_formula_rule(
        f'=$P2="Awaiting Decision"',
        COLORS["awaiting"]
    ))
    # Bid Submitted (most common active state — applied last so closed states override)
    rules.append(custom_formula_rule(
        f'=$P2="Bid Submitted"',
        COLORS["submitted"]
    ))

    # Days Until Due column-specific coloring (AF is days due col if it's at index 31)
    if days_due_col is not None:
        # AF is the 32nd col (index 31). Compute A1 letter:
        d_col_letter = ""
        n = days_due_col + 1
        while n > 0:
            n, r = divmod(n - 1, 26)
            d_col_letter = chr(65 + r) + d_col_letter
        days_range = {
            "sheetId": sheet_id,
            "startRowIndex": 1,
            "endRowIndex": n_rows + 1,
            "startColumnIndex": days_due_col,
            "endColumnIndex": days_due_col + 1,
        }
        # Overdue (<=0)
        rules.append({
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [days_range],
                    "booleanRule": {
                        "condition": {"type": "NUMBER_LESS_THAN_EQ",
                                       "values": [{"userEnteredValue": "0"}]},
                        "format": {"backgroundColor": COLORS["overdue_red"],
                                    "textFormat": {"foregroundColor": COLORS["white_text"],
                                                   "bold": True}},
                    },
                },
                "index": 0,
            }
        })
        # 1-2 days
        rules.append({
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [days_range],
                    "booleanRule": {
                        "condition": {"type": "NUMBER_BETWEEN",
                                       "values": [{"userEnteredValue": "1"},
                                                  {"userEnteredValue": "2"}]},
                        "format": {"backgroundColor": COLORS["due_orange"],
                                    "textFormat": {"bold": True}},
                    },
                },
                "index": 0,
            }
        })
        # 3-7 days
        rules.append({
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [days_range],
                    "booleanRule": {
                        "condition": {"type": "NUMBER_BETWEEN",
                                       "values": [{"userEnteredValue": "3"},
                                                  {"userEnteredValue": "7"}]},
                        "format": {"backgroundColor": COLORS["due_yellow"]},
                    },
                },
                "index": 0,
            }
        })
        # 8+ days
        rules.append({
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [days_range],
                    "booleanRule": {
                        "condition": {"type": "NUMBER_GREATER_THAN_EQ",
                                       "values": [{"userEnteredValue": "8"}]},
                        "format": {"backgroundColor": COLORS["due_green"]},
                    },
                },
                "index": 0,
            }
        })

    # Win/Loss column-specific
    if winloss_col is not None:
        wl_range = {
            "sheetId": sheet_id,
            "startRowIndex": 1,
            "endRowIndex": n_rows + 1,
            "startColumnIndex": winloss_col,
            "endColumnIndex": winloss_col + 1,
        }
        rules.append({
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [wl_range],
                    "booleanRule": {
                        "condition": {"type": "TEXT_EQ",
                                       "values": [{"userEnteredValue": "WIN"}]},
                        "format": {"textFormat": {"foregroundColor": COLORS["win_text"],
                                                  "bold": True}},
                    },
                },
                "index": 0,
            }
        })
        rules.append({
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [wl_range],
                    "booleanRule": {
                        "condition": {"type": "TEXT_EQ",
                                       "values": [{"userEnteredValue": "LOSS"}]},
                        "format": {"textFormat": {"foregroundColor": COLORS["loss_text"],
                                                  "bold": True}},
                    },
                },
                "index": 0,
            }
        })
        rules.append({
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [wl_range],
                    "booleanRule": {
                        "condition": {"type": "TEXT_EQ",
                                       "values": [{"userEnteredValue": "PENDING"}]},
                        "format": {"textFormat": {"foregroundColor": COLORS["pending_gray"],
                                                  "italic": True}},
                    },
                },
                "index": 0,
            }
        })

    print(f"Applying {len(rules)} conditional formatting rules...")
    wb.batch_update({"requests": rules})
    print("Conditional formatting applied.")

    # 3.5. Normalize any RFC-2822 / freeform date text in date columns to
    # proper MM/DD/YYYY so the Sheets sort treats them as real dates and the
    # display is clean. Runs every time formatting is applied → idempotent.
    import re as _re
    from datetime import datetime as _dt
    try:
        from email.utils import parsedate_to_datetime as _pdt
    except Exception:
        _pdt = None
    _MMDDYY = _re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4}$")
    def _parse_loose(s):
        s = (s or "").strip()
        if not s: return None
        for fmt in ("%m/%d/%Y","%m/%d/%y","%Y-%m-%d","%Y-%m-%dT%H:%M:%S"):
            try: return _dt.strptime(s[:len(fmt)+2 if "T" in fmt else len(fmt)], fmt).date()
            except Exception: pass
        if _pdt:
            try:
                d = _pdt(s)
                return d.date() if d else None
            except Exception:
                return None
        return None
    all_vals = sh.get_all_values()
    date_norm_updates = []
    for col_name in ("ITB Received Date", "Bid Due Date", "Bid Submitted Date", "Award Date"):
        if col_name not in hdrs:
            continue
        ci = hdrs.index(col_name)
        col_letter_n = ""
        _n = ci + 1
        while _n > 0:
            _n, _r = divmod(_n - 1, 26)
            col_letter_n = chr(65 + _r) + col_letter_n
        for r_idx, row in enumerate(all_vals[1:], start=2):
            v = row[ci].strip() if len(row) > ci else ""
            if not v or _MMDDYY.match(v):
                continue
            d = _parse_loose(v)
            if d:
                date_norm_updates.append({
                    "range": f"{col_letter_n}{r_idx}",
                    "values": [[d.strftime("%m/%d/%Y")]],
                })
    if date_norm_updates:
        BATCH = 50
        for i in range(0, len(date_norm_updates), BATCH):
            _retry(sh.batch_update, date_norm_updates[i:i+BATCH],
                   value_input_option="USER_ENTERED")
        print(f"Normalized {len(date_norm_updates)} date cells → MM/DD/YYYY")

    # 3.6. Apply MM/dd/yyyy display format to date columns (so even native
    # date serials render cleanly in the UI).
    date_format_reqs = []
    for col_name in ("ITB Received Date", "Bid Due Date", "Bid Submitted Date", "Award Date"):
        if col_name not in hdrs: continue
        ci = hdrs.index(col_name)
        date_format_reqs.append({
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 1,
                    "endRowIndex": n_rows + 1,
                    "startColumnIndex": ci,
                    "endColumnIndex": ci + 1,
                },
                "cell": {"userEnteredFormat": {"numberFormat":
                    {"type": "DATE", "pattern": "MM/dd/yyyy"}}},
                "fields": "userEnteredFormat.numberFormat",
            }
        })
    if date_format_reqs:
        wb.batch_update({"requests": date_format_reqs})
        print(f"Date display format MM/dd/yyyy enforced on {len(date_format_reqs)} columns")

    # 4. Optional: sort the sheet now.
    # Sort key: [Sort Priority ASC, Bid Submitted Date DESC, ITB Received Date DESC]
    # → Active rows on top, MOST-RECENTLY-SUBMITTED bids at the very top
    # (those are the fresh actions needing follow-up). New invitations without
    # a submission yet fall in behind by ITB date.
    if args.apply_sort:
        sort_pri_col = hdrs.index("Sort Priority")
        sub_date_col = hdrs.index("Bid Submitted Date") if "Bid Submitted Date" in hdrs else None
        due_date_col = hdrs.index("Bid Due Date") if "Bid Due Date" in hdrs else None
        itb_col = hdrs.index("ITB Received Date") if "ITB Received Date" in hdrs else None
        # Sort: priority asc, then submitted DESC (most recently sent on top),
        # then due ASC (closest deadline first for pipeline rows w/o submission),
        # then ITB DESC (newest invitations).
        sort_specs = [{"dimensionIndex": sort_pri_col, "sortOrder": "ASCENDING"}]
        if sub_date_col is not None:
            sort_specs.append({"dimensionIndex": sub_date_col, "sortOrder": "DESCENDING"})
        if due_date_col is not None:
            sort_specs.append({"dimensionIndex": due_date_col, "sortOrder": "ASCENDING"})
        if itb_col is not None:
            sort_specs.append({"dimensionIndex": itb_col, "sortOrder": "DESCENDING"})
        sort_request = {
            "sortRange": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 1,
                    "endRowIndex": n_rows + 1,
                    "startColumnIndex": 0,
                    "endColumnIndex": n_cols,
                },
                "sortSpecs": sort_specs,
            }
        }
        wb.batch_update({"requests": [sort_request]})
        print("Sheet sorted: Active rows on top, newest invitations first within each group.")

    print("\nDone.")


if __name__ == "__main__":
    main()
