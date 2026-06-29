#!/usr/bin/env python3
"""
recap.py - Print the activity log for today, yesterday, or a specific date.

Carol runs this when asked "what have we done today / yesterday / etc."

Usage:
  python scripts/recap.py --today
  python scripts/recap.py --yesterday
  python scripts/recap.py --date 2026-05-09
"""
from __future__ import annotations
import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def find_log_for(d: date) -> Path | None:
    """Return path to the activity log file for date d, or None."""
    if d == date.today():
        p = ROOT / "data" / "memory" / "activity_log_today.md"
        return p if p.exists() else None
    p = ROOT / "data" / "memory" / f"activity_log_{d.strftime('%Y-%m-%d')}.md"
    return p if p.exists() else None


def main():
    ap = argparse.ArgumentParser()
    # 6/2 fix — NOT required. Bare `recap.py` now defaults to today, so the
    # caller never gets a "must pass a date" error that tempts them to
    # compute and pass a WRONG --date by hand (Carol kept passing 2026-05-31
    # because she thought today was 6/1). Prefer --today/--yesterday, which
    # resolve off the SYSTEM clock — never compute a date manually.
    g = ap.add_mutually_exclusive_group(required=False)
    g.add_argument("--today", action="store_true")
    g.add_argument("--yesterday", action="store_true")
    g.add_argument("--date", help="YYYY-MM-DD")
    args = ap.parse_args()

    if args.yesterday:
        target = date.today() - timedelta(days=1)
        label = "Yesterday"
    elif args.date:
        from datetime import datetime
        target = datetime.strptime(args.date, "%Y-%m-%d").date()
        label = args.date
    else:  # --today OR no flag at all → today (system clock)
        target = date.today()
        label = "Today"
        label = target.strftime("%A %B %d")

    p = find_log_for(target)
    if not p:
        # Check if today's log might exist but for a different date marker
        if args.today:
            today_p = ROOT / "data" / "memory" / "activity_log_today.md"
            if today_p.exists():
                # Verify it's actually for today
                first_line = today_p.read_text(encoding="utf-8").split("\n", 1)[0]
                if target.isoformat() in first_line:
                    p = today_p
        if not p:
            print(f"No activity logged for {label} ({target.isoformat()}).")
            print(f"Looked for: data/memory/activity_log_{'today' if args.today else target.isoformat()}.md")
            return 1

    content = p.read_text(encoding="utf-8")
    if not content.strip():
        print(f"Activity log for {label} exists but is empty.")
        return 0

    # Print as-is — Carol should quote the headlines + bullets
    print(content)
    return 0


if __name__ == "__main__":
    sys.exit(main())
