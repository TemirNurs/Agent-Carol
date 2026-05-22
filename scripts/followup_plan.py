#!/usr/bin/env python3
"""
followup_plan.py - Generate a prioritized follow-up action list from the CRM.

Cadence (from Dashboard):
  FU1: Receipt Confirm     -> Bid Date + 2  days
  FU2: Award Status        -> Bid Date + 7  days
  FU3: Feedback Request    -> Bid Date + 30 days
  FU4: Relationship Check-in -> Bid Date + 90 days

For each active bid (Status = Bid Submitted or Awaiting Decision):
  - Compute current age in days
  - Determine which FU is DUE based on cadence
  - Check what's already been logged in FU1-FU4 Date columns
  - Skip bids flagged INACTIVE (BOUNCE, NOT BIDDING, Withdrawn, On Hold)

Output: 3 buckets:
  [OVERDUE]   - past target window, hasn't been done
  [DUE TODAY] - hits target today
  [UPCOMING]  - within the next 3 days
"""
from __future__ import annotations
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from crm_lib import get_sheet

CADENCE = [
    ("FU1: Receipt Confirm",     2,   "FU1 Date"),
    ("FU2: Award Status",        7,   "FU2 Date"),
    ("FU3: Feedback Request",    30,  "FU3 Date"),
    ("FU4: Relationship Check-in", 90, "FU4 Date"),
]

ACTIVE_STATUSES = {"Bid Submitted", "Awaiting Decision"}
INACTIVE_FLAGS = ["BOUNCE", "NOT BIDDING", "WITHDRAWN", "ON HOLD"]


def parse_date_safe(s):
    if not s: return None
    for fmt in ("%a, %d %b %Y", "%m/%d/%Y", "%Y-%m-%d", "%d-%b-%Y"):
        try: return datetime.strptime(str(s).strip()[:30], fmt).date()
        except Exception: pass
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(s).date()
    except Exception:
        return None


def main():
    sh = get_sheet("Bid Log")
    hdrs = sh.row_values(1)
    rows = sh.get_all_values()

    today = date.today()
    overdue, due_today, upcoming, skipped_inactive = [], [], [], []
    active_count = 0

    for r_idx, row in enumerate(rows[1:], start=2):
        d = {h: (row[i] if i < len(row) else "") for i, h in enumerate(hdrs)}
        status = (d.get("Status") or "").strip()
        if status not in ACTIVE_STATUSES:
            continue
        notes = (d.get("Notes") or "").upper()
        flag = next((f for f in INACTIVE_FLAGS if f"[{f}]" in notes), None)
        if flag:
            skipped_inactive.append({"row": r_idx, "data": d, "flag": flag})
            continue
        active_count += 1
        sub_date = parse_date_safe(d.get("Bid Submitted Date"))
        if not sub_date:
            continue
        age_days = (today - sub_date).days
        # Pick the FU stage APPROPRIATE FOR THE BID'S AGE — earlier stages that
        # we missed (e.g. FU1 day-2 on a 30-day-old bid) are SKIPPED, not flagged
        # as "overdue forever." Then check if that stage has been logged.
        # Stage windows:
        #   age 2-6   -> FU1
        #   age 7-29  -> FU2
        #   age 30-89 -> FU3
        #   age 90+   -> FU4
        if   age_days <  2: stage_idx = None     # too new, no FU needed yet
        elif age_days <  7: stage_idx = 0        # FU1
        elif age_days < 30: stage_idx = 1        # FU2
        elif age_days < 90: stage_idx = 2        # FU3
        else:               stage_idx = 3        # FU4

        if stage_idx is None:
            continue

        label, target_days, fu_col = CADENCE[stage_idx]
        already_logged = (d.get(fu_col) or "").strip()
        if already_logged:
            # Current stage already done — see if we're approaching the NEXT stage
            next_stage_idx = stage_idx + 1 if stage_idx < 3 else None
            if next_stage_idx is None:
                continue  # already at FU4, nothing more to do
            label, target_days, fu_col = CADENCE[next_stage_idx]
            already_logged = (d.get(fu_col) or "").strip()
            if already_logged:
                continue
        target_date = sub_date + timedelta(days=target_days)
        days_until = (target_date - today).days
        next_fu = {
            "label": label,
            "target_days": target_days,
            "target_date": target_date,
            "days_until": days_until,
            "fu_col": fu_col,
        }
        amount = (d.get("Bid Amount ($)") or "$0").replace("$", "").replace(",", "")
        try: amt = int(amount.split(".")[0])
        except: amt = 0
        entry = {
            "row": r_idx,
            "bid_id": d.get("Bid #", ""),
            "project": d.get("Project Name", ""),
            "gc": d.get("GC / Client", ""),
            "contact": d.get("Contact Name", ""),
            "email": d.get("Contact Email", ""),
            "phone": d.get("Contact Phone", ""),
            "sub_date": sub_date,
            "age_days": age_days,
            "amount": amt,
            "amount_str": d.get("Bid Amount ($)", ""),
            "next_fu": next_fu,
            "status": status,
        }
        if next_fu["days_until"] < 0:
            overdue.append(entry)
        elif next_fu["days_until"] == 0:
            due_today.append(entry)
        elif next_fu["days_until"] <= 3:
            upcoming.append(entry)

    # Sort each bucket by amount desc (high value first), then age
    overdue.sort(key=lambda x: (-x["amount"], -x["age_days"]))
    due_today.sort(key=lambda x: -x["amount"])
    upcoming.sort(key=lambda x: (x["next_fu"]["days_until"], -x["amount"]))

    print(f"=" * 80)
    print(f"FOLLOW-UP PLAN — {today.strftime('%A, %B %d, %Y')}")
    print(f"=" * 80)
    print(f"Active bids: {active_count} | Overdue: {len(overdue)} | "
          f"Due today: {len(due_today)} | Upcoming (3d): {len(upcoming)} | "
          f"Skipped inactive: {len(skipped_inactive)}")
    print()

    if overdue:
        print(f"[!!] OVERDUE ({len(overdue)}) — do these first")
        print("-" * 80)
        total_value = sum(e["amount"] for e in overdue)
        print(f"Combined pipeline value: ${total_value:,}")
        print()
        for e in overdue:
            fu = e["next_fu"]
            print(f"  {e['bid_id']}  ${e['amount']:>10,}  {e['project'][:38]:<38}")
            print(f"    GC: {e['gc'][:30]:<30}  Contact: {e['contact'][:20]:<20}  {e['email'][:30]}")
            print(f"    Sub: {e['sub_date']}  ({e['age_days']}d old)  ")
            print(f"    NEXT: {fu['label']} was due {-fu['days_until']}d ago ({fu['target_date']})")
            print()

    if due_today:
        print(f"[!] DUE TODAY ({len(due_today)})")
        print("-" * 80)
        for e in due_today:
            fu = e["next_fu"]
            print(f"  {e['bid_id']}  ${e['amount']:>10,}  {e['project'][:38]:<38}")
            print(f"    GC: {e['gc'][:30]:<30}  -> {e['email'][:35]}")
            print(f"    {fu['label']}")
            print()

    if upcoming:
        print(f"[i] UPCOMING NEXT 3 DAYS ({len(upcoming)})")
        print("-" * 80)
        for e in upcoming:
            fu = e["next_fu"]
            print(f"  {e['bid_id']}  ${e['amount']:>10,}  {e['project'][:38]:<38}  "
                  f"-> {fu['label']} in {fu['days_until']}d")
            print(f"    {e['email'][:40]}")
        print()

    if skipped_inactive:
        print(f"[x] SKIPPED — flagged inactive ({len(skipped_inactive)})")
        print("-" * 80)
        for s in skipped_inactive:
            d = s["data"]
            print(f"  {d.get('Bid #','')}  {d.get('Project Name','')[:35]:<35}  "
                  f"[{s['flag']}]")
        print()


if __name__ == "__main__":
    main()
