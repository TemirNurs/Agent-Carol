#!/usr/bin/env python3
r"""
deadline_alerts.py - Ping Telegram when a bid's deadline is approaching.

Tiers:
  0 days  → 🚨 DUE TODAY — submit ASAP or pull out
  1 day   → ⏰ Due TOMORROW — last day to estimate
  2 days  → ⚠️  Due in 2 days
  -1 day  → ❌ OVERDUE by 1 day — flag for status update or extension request
            (only fire OVERDUE alert once per bid via state file dedup)

Reads from Bid Log sheet → finds rows with Bid Due Date matching tier_days.
Skips rows already in terminal Status (Won/Lost/Withdrawn/Bid Submitted).
Sends one bundled Telegram message to the user.

State: data/memory/deadline_alerts_sent.json
  { internal_id: { tier: "due-1", sent_at: "..." } }
This prevents re-pinging the same bid twice for the same tier.

Usage:
  python scripts/deadline_alerts.py             # check + send
  python scripts/deadline_alerts.py --dry-run   # show what would send
  python scripts/deadline_alerts.py --quiet     # CLI silent (only Telegram)
"""
from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / "data" / "memory" / "deadline_alerts_sent.json"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def parse_date(s):
    if not s: return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%d-%b-%Y"):
        try: return datetime.strptime(str(s).strip()[:30], fmt).date()
        except Exception: pass
    return None


def load_state():
    if STATE_FILE.exists():
        try: return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception: pass
    return {}


def save_state(s):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(s, indent=2), encoding="utf-8")


TIER_DEFS = [
    # (days_until_due, key, prefix)
    (0,  "due-0",  "🚨 DUE TODAY"),
    (1,  "due-1",  "⏰ Due TOMORROW"),
    (2,  "due-2",  "⚠️  Due in 2 days"),
    (-1, "over-1", "❌ OVERDUE by 1 day"),
    (-3, "over-3", "❌ OVERDUE by 3+ days (consider withdrawing)"),
]

# Statuses where deadline doesn't matter (already submitted or closed)
SKIP_STATUSES = {"Bid Submitted", "Awaiting Decision", "Won", "Lost",
                 "Withdrawn", "No Decision", "No Bid"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    sys.path.insert(0, str(ROOT / "scripts"))
    from crm_lib import get_sheet

    sh = get_sheet("Bid Log")
    hdrs = sh.row_values(1)
    rows = sh.get_all_values()
    iid_idx = hdrs.index("Internal ID") if "Internal ID" in hdrs else None
    proj_idx = hdrs.index("Project Name") if "Project Name" in hdrs else None
    due_idx = hdrs.index("Bid Due Date") if "Bid Due Date" in hdrs else None
    status_idx = hdrs.index("Status") if "Status" in hdrs else None
    gc_idx = hdrs.index("GC / Client") if "GC / Client" in hdrs else None
    bid_idx = hdrs.index("Bid #") if "Bid #" in hdrs else None
    tags_idx = hdrs.index("Tags") if "Tags" in hdrs else None

    if any(i is None for i in (iid_idx, due_idx, status_idx, proj_idx)):
        print("ERROR: missing required column in Bid Log (Internal ID / Bid Due Date / Status / Project Name)")
        return

    today = date.today()
    state = load_state()

    # Group buckets by tier
    buckets = {t[1]: [] for t in TIER_DEFS}
    for r in rows[1:]:
        d = {h: (r[i] if i < len(r) else "") for i, h in enumerate(hdrs)}
        iid = (d.get("Internal ID") or "").strip()
        if not iid: continue
        due = parse_date(d.get("Bid Due Date", ""))
        if not due: continue
        status = (d.get("Status") or "").strip()
        if status in SKIP_STATUSES: continue
        days = (due - today).days
        for tier_days, tier_key, tier_prefix in TIER_DEFS:
            if tier_key.startswith("over"):
                # Overdue: matches if days <= tier_days (negative bucket)
                if days <= tier_days:
                    # Only the most-overdue tier counts (skip if already in -1 and -3 both qualify)
                    if tier_key == "over-3":
                        buckets[tier_key].append(d)
                    elif tier_key == "over-1" and days > -3:
                        buckets[tier_key].append(d)
                    break
            elif days == tier_days:
                buckets[tier_key].append(d)
                break

    # Filter out bids already alerted at this tier
    fresh = {}
    for tier_key, lst in buckets.items():
        fresh[tier_key] = []
        for d in lst:
            iid = d.get("Internal ID", "").strip()
            already = state.get(iid, {}).get(tier_key)
            if not already:
                fresh[tier_key].append(d)

    total = sum(len(v) for v in fresh.values())
    if total == 0:
        if not args.quiet:
            print(f"[deadlines] no new alerts today ({today})")
        return

    # Build Telegram message
    lines = [f"📋 *Deadline check — {today.strftime('%a %b %d')}*", ""]
    for tier_days, tier_key, tier_prefix in TIER_DEFS:
        lst = fresh[tier_key]
        if not lst: continue
        lines.append(f"*{tier_prefix}* ({len(lst)})")
        for d in lst:
            bid = d.get("Bid #", "")
            proj = (d.get("Project Name") or "")[:55]
            gc = (d.get("GC / Client") or "")[:30]
            tags = (d.get("Tags") or "")
            line = f"  • {bid} {proj}"
            if gc: line += f" — {gc}"
            if tags: line += f"  [{tags}]"
            lines.append(line)
        lines.append("")
    msg = "\n".join(lines).strip()
    if not args.quiet:
        print(msg)

    if args.dry_run:
        print(f"\n[deadlines] DRY-RUN: would alert on {total} bid(s)")
        return

    # Send via Telegram
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        from telegram_send import send as tg_send
        tg_send(msg)
    except Exception:
        # Fallback: shell out
        try:
            subprocess.run([sys.executable, str(ROOT / "scripts" / "telegram_send.py"),
                            "--text", msg], timeout=30, capture_output=True)
        except Exception as e:
            print(f"[deadlines] Telegram send failed: {e}")

    # Mark as sent in state
    for tier_key, lst in fresh.items():
        for d in lst:
            iid = d.get("Internal ID", "").strip()
            state.setdefault(iid, {})[tier_key] = datetime.now().isoformat(timespec="seconds")
    save_state(state)

    # Activity log
    try:
        from log_activity import log_activity
        log_activity("⏰ Deadline alerts",
                     f"Pinged {total} bid(s) via Telegram: "
                     + ", ".join(f"{t[1]}={len(fresh[t[1]])}" for t in TIER_DEFS if fresh[t[1]]))
    except Exception:
        pass


if __name__ == "__main__":
    main()
