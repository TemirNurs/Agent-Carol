#!/usr/bin/env python3
"""
Authoritative bid stats — Carol MUST use this instead of counting by hand.
Prints total + breakdown by source, urgency bucket, and cache age.

Usage:
  python scripts/bid_stats.py               # full stats
  python scripts/bid_stats.py --count       # just the total (one number)
  python scripts/bid_stats.py --json        # machine-readable
"""

import json
import sys
import os
from datetime import date, datetime, timedelta
from collections import defaultdict
from pathlib import Path

BIDS_FILE = Path(__file__).resolve().parent.parent / "data" / "memory" / "active_bids.json"


def _parse_date(s):
    if not s:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            pass
    return None


def compute_stats():
    if not BIDS_FILE.exists():
        return {"error": "active_bids.json not found", "total": 0}

    mtime = datetime.fromtimestamp(BIDS_FILE.stat().st_mtime)
    age_min = int((datetime.now() - mtime).total_seconds() / 60)

    bids = json.load(open(BIDS_FILE, encoding="utf-8"))
    today = date.today()
    # Calendar-week buckets (US convention: Sunday = start of week)
    # weekday(): Mon=0 .. Sun=6. Days since last Sunday:
    days_since_sun = (today.weekday() + 1) % 7
    week_start = today - timedelta(days=days_since_sun)       # most recent Sunday
    this_week_end = week_start + timedelta(days=6)            # next Saturday
    next_week_start = this_week_end + timedelta(days=1)       # next Sunday
    next_week_end = next_week_start + timedelta(days=6)       # Saturday after

    by_source = defaultdict(int)
    urgency = {"today": 0, "tomorrow": 0, "rest_of_this_week": 0, "next_week": 0, "later": 0, "past": 0, "no_date": 0}

    for b in bids:
        by_source[b.get("source", "unknown")] += 1
        d = _parse_date(b.get("due_date", ""))
        if not d:
            urgency["no_date"] += 1
        elif d < today:
            urgency["past"] += 1
        elif d == today:
            urgency["today"] += 1
        elif d == today + timedelta(days=1):
            urgency["tomorrow"] += 1
        elif d <= this_week_end:
            urgency["rest_of_this_week"] += 1
        elif d <= next_week_end:
            urgency["next_week"] += 1
        else:
            urgency["later"] += 1

    return {
        "total": len(bids),
        "by_source": dict(by_source),
        "urgency": urgency,
        "cache_age_minutes": age_min,
        "cache_mtime": mtime.isoformat(timespec="seconds"),
    }


def main():
    s = compute_stats()
    if "--count" in sys.argv:
        print(s.get("total", 0))
        return
    if "--json" in sys.argv:
        print(json.dumps(s, indent=2))
        return

    if "--brief" in sys.argv:
        # Telegram-ready Markdown. Carol should quote this VERBATIM.
        u = s["urgency"]
        age = s["cache_age_minutes"]
        fresh = "fresh" if age < 60 else (f"⚠️ STALE {age}min" if age > 180 else f"{age}min old")
        lines = [
            f"*{s['total']} active invitations* · cache {fresh}",
            "",
            "*By source:*",
        ]
        for src, n in s["by_source"].items():
            label = {"buildingconnected": "BuildingConnected",
                     "constructconnect": "ConstructConnect",
                     "email": "Email"}.get(src, src)
            lines.append(f"  • {label}: {n}")
        lines.extend([
            "",
            "*By urgency:*",
            f"  • Due today: {u['today']}",
            f"  • Due tomorrow: {u['tomorrow']}",
            f"  • Rest of this week: {u['rest_of_this_week']}",
            f"  • Next week: {u['next_week']}",
            f"  • Later: {u['later']}",
            f"  • Past due: {u['past']}",
            f"  • No due date: {u['no_date']}",
        ])
        print("\n".join(lines))
        return

    # Human-readable
    age = s["cache_age_minutes"]
    fresh = "fresh" if age < 60 else ("STALE - daemon may be down" if age > 180 else f"{age} min old")
    print(f"ACTIVE BIDS: {s['total']}  (cache {fresh}, updated {s['cache_mtime']})")
    print()
    print("By source:")
    for src, n in s["by_source"].items():
        print(f"  {n:3}  {src}")
    print()
    print("By urgency:")
    u = s["urgency"]
    print(f"  {u['today']:3}  due today")
    print(f"  {u['tomorrow']:3}  due tomorrow")
    print(f"  {u['rest_of_this_week']:3}  rest of this calendar week (through Saturday)")
    print(f"  {u['next_week']:3}  next calendar week (Sun-Sat)")
    print(f"  {u['later']:3}  later")
    print(f"  {u['past']:3}  past due (stale?)")
    print(f"  {u['no_date']:3}  no date")


if __name__ == "__main__":
    main()
