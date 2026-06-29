#!/usr/bin/env python3
"""
Bid brief formatter — produces the mobile-friendly rich brief Carol pastes to chat.
Use this instead of hand-formatting. Consistent emoji tags, source marks, distance,
estimated value, known-GC markers.

Usage:
  python scripts/bid_brief.py                      # this week (default)
  python scripts/bid_brief.py --day monday         # just Monday's bids
  python scripts/bid_brief.py --day today
  python scripts/bid_brief.py --day tomorrow
  python scripts/bid_brief.py --date 4/21/2026     # specific date
  python scripts/bid_brief.py --week              # whole calendar week
  python scripts/bid_brief.py --all                # everything (no filter)
  python scripts/bid_brief.py --max 5              # max items shown explicitly
"""

import json
import re
import sys
import argparse
from datetime import date, datetime, timedelta
from pathlib import Path

# Force UTF-8 on Windows cp1252 consoles so emoji output doesn't crash
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = Path(__file__).resolve().parent.parent
BIDS_FILE = BASE / "data" / "memory" / "active_bids.json"
GC_DIR    = BASE / "data" / "memory" / "gc"

FACILITY_RATES = {
    "Hotel": 2.75, "Restaurant": 2.25, "Retail": 2.00, "School": 2.00,
    "Gov/Mil": 1.75, "Civic": 1.50, "Multifamily": 2.00, "Office": 1.50,
    "Healthcare": 2.00, "Other": 1.75,
}
DEFAULT_SF = {
    "Retail": 4000, "Hotel": 60000, "Restaurant": 3500, "Gov/Mil": 15000,
    "School": 50000, "Civic": 25000, "Multifamily": 150000, "Office": 20000,
    "Healthcare": 25000, "Other": 20000,
}


def parse_date(s):
    if not s:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            pass
    return None


def load_known_gcs():
    known = {}
    if GC_DIR.exists():
        for f in GC_DIR.glob("*.json"):
            try:
                d = json.load(open(f, encoding="utf-8"))
                name = (d.get("name") or f.stem).lower()
                known[re.sub(r"[^a-z]", "", name)] = d
            except Exception:
                pass
    return known


def match_gc(gc_name, known):
    key = re.sub(r"[^a-z]", "", (gc_name or "").lower())
    if not key:
        return None
    for k in known:
        if k and (k in key or (len(key) >= 8 and k.startswith(key[:8]))):
            return known[k]
    return None


def facility_of(name):
    n = (name or "").lower()
    if any(k in n for k in ["school", "elementary", "classroom", "university", "college", "academic", "community college"]):
        return "School"
    if any(k in n for k in ["hotel", "suites", "marriott", "hilton", "home 2", "hyatt"]):
        return "Hotel"
    if any(k in n for k in ["bojangles", "chase", "sheetz", "dollar", "cvs", "ulta", "american eagle", "burberry",
                            "autozone", "bank", "first horizon", "abercrombie", "harris teeter", "circle k", "buckle",
                            "pb express"]):
        return "Retail"
    if any(k in n for k in ["brewery", "restaurant", "pure green", "dutch bros", "tyson corner"]):
        return "Restaurant"
    if any(k in n for k in ["va ", "vamc", "veteran", "ems", "fire", "police", "guard", "mcas", "military",
                            "gatehouse", "cherry point", "f-35", "uscg", "sampson ci", "postal", "federal"]):
        return "Gov/Mil"
    if any(k in n for k in ["community center", "park", "rec", "civic", "parks and recreation", "town hall"]):
        return "Civic"
    if any(k in n for k in ["residence hall", "apartment", "village", "dorm"]):
        return "Multifamily"
    if any(k in n for k in ["hospital", "medical", "clinic", "pediatrics", "health", "wellness"]):
        return "Healthcare"
    if any(k in n for k in ["warehouse", "distribution", "amazon", "storage", "parking garage"]):
        return "Other"
    return "Other"


def est_value(name, fac):
    n = (name or "").lower()
    m = re.search(r"([0-9]{3,6})\s*sf", n)
    sf = int(m.group(1)) if m else DEFAULT_SF.get(fac, 20000)
    if "campus" in n or "multiple" in n:
        sf *= 2
    if "renovation" in n or "reno" in n or "remodel" in n:
        sf = int(sf * 0.7)
    if "ground up" in n or "new construction" in n or "new build" in n:
        sf = int(sf * 1.3)
    return sf * FACILITY_RATES.get(fac, 1.75)


def src_tag(s):
    return {"buildingconnected": "BC", "constructconnect": "CC", "email": "EM"}.get((s or "").lower(), "?")


def fmt_bid(b, known, today):
    gc = match_gc(b.get("gc", ""), known)
    fac = facility_of(b.get("project_name", ""))
    val_k = est_value(b.get("project_name", ""), fac) / 1000
    dist = b.get("distance_miles")
    dist_s = f"{dist:.0f}mi" if isinstance(dist, (int, float)) else "?mi"
    due = parse_date(b.get("due_date", ""))

    # Priority emojis
    tags = []
    if val_k >= 100:
        tags.append("🎯")
    elif val_k >= 50:
        tags.append("✅")
    else:
        tags.append("⚠️")
    if gc:
        tags.append("⭐")
    if isinstance(dist, (int, float)) and dist < 30:
        tags.append("📍")

    # Day abbreviation
    if due:
        day_abbr = due.strftime("%a %m/%d")
    else:
        day_abbr = "?"

    name = (b.get("project_name") or "?")[:42]
    gc_name = (b.get("gc") or "?")[:20]

    return {
        "tags": "".join(tags),
        "day": day_abbr,
        "name": name,
        "gc": gc_name,
        "src": src_tag(b.get("source", "")),
        "dist": dist_s,
        "val_k": val_k,
        "date": due,
        "known": bool(gc),
        "raw": b,
    }


def select(bids, mode, today):
    """Filter bids per user-requested scope."""
    days_since_sun = (today.weekday() + 1) % 7
    week_start = today - timedelta(days=days_since_sun)
    this_week_end = week_start + timedelta(days=6)

    kept = []
    for b in bids:
        d = parse_date(b.get("due_date", ""))
        if not d:
            continue
        if d < today:
            continue
        if mode == "today" and d != today: continue
        if mode == "tomorrow" and d != today + timedelta(days=1): continue
        if mode.endswith("day") and mode != "today" and mode != "tomorrow":
            # day-of-week mode like 'monday' — match ONLY the NEXT occurrence
            wanted = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
                      "friday": 4, "saturday": 5, "sunday": 6}.get(mode)
            if wanted is None:
                continue
            days_ahead = (wanted - today.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7 if today.weekday() != wanted else 0
            target = today + timedelta(days=days_ahead)
            if d != target:
                continue
        if mode == "week" and not (today <= d <= this_week_end):
            continue
        if mode == "all":
            pass
        kept.append(b)
    return kept


def render(rows, mode, max_items, bids_total):
    if not rows:
        return f"No bids matching '{mode}'.\n(Total active bids: {bids_total})"

    # Sort priority: highest $ first. Known-GC elevates below-$50K from "skip" to "show".
    # Order: $100K+ -> $50-100K -> known-GC-any-value -> the rest (by date).
    def priority_key(r):
        if r["val_k"] >= 100: tier = 0
        elif r["val_k"] >= 50: tier = 1
        elif r["known"]:      tier = 2   # known-GC bumps small bids into visible tier
        else:                 tier = 3
        return (tier, -r["val_k"], r["date"] or date.max)
    rows.sort(key=priority_key)

    header = {
        "today": "TODAY",
        "tomorrow": "TOMORROW",
        "week": "THIS WEEK",
        "all": "ALL ACTIVE",
    }.get(mode, mode.upper())

    # For specific-day modes, show all. For week/all, cap at max_items.
    specific_day = mode in ("today", "tomorrow", "monday", "tuesday", "wednesday",
                            "thursday", "friday", "saturday", "sunday")
    shown = rows if specific_day else rows[:max_items]
    out = [f"**{header}** ({len(rows)} bid{'s' if len(rows)!=1 else ''}):"]
    for r in shown:
        val_str = f"~${r['val_k']:.0f}K"
        out.append(f"{r['tags']} {r['day']}  [{r['src']}]  {r['name']}  ({r['gc']})  {r['dist']}  {val_str}")
    if not specific_day and len(rows) > max_items:
        out.append(f"\n+ {len(rows) - max_items} more. Say 'show all' for full list.")
    out.append("")
    out.append("Tags: 🎯 $100K+  ✅ $50-100K sweet-spot  ⚠️ below $50K  ⭐ known GC  📍 local (<30mi)")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default=None, help="today|tomorrow|monday|tuesday|...")
    ap.add_argument("--date", default=None, help="m/d/yyyy specific date")
    ap.add_argument("--week", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--max", type=int, default=6, help="max bids shown explicitly (default 6)")
    args = ap.parse_args()

    today = date.today()
    bids = json.load(open(BIDS_FILE, encoding="utf-8"))
    known = load_known_gcs()

    if args.all:
        mode = "all"
    elif args.date:
        target = parse_date(args.date)
        if not target:
            print(f"Bad date: {args.date}"); sys.exit(1)
        mode = target.strftime("%A").lower()
        filt = [b for b in bids if parse_date(b.get("due_date")) == target]
    elif args.day:
        mode = args.day.lower()
    else:
        mode = "week"

    if not args.date:
        filt = select(bids, mode, today)

    rows = [fmt_bid(b, known, today) for b in filt]
    mtime = datetime.fromtimestamp(BIDS_FILE.stat().st_mtime)
    age = int((datetime.now() - mtime).total_seconds() / 60)

    print(render(rows, mode, args.max, len(bids)))
    print(f"\n(cache: {age} min old)")


if __name__ == "__main__":
    main()
