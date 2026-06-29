#!/usr/bin/env python3
r"""
bids_today.py — Answer "what do we have for today" with the ACTUAL bids,
not aggregate counts.

bid_stats.py --day today prints "3 due today" with zero project names — useless
for an estimator deciding what to work on. This lists the real projects due
today / tomorrow / rest of this week: name, GC, location, due date, source.
Output is already human-readable so Carol presents it as-is (no summarizing,
no raw-count dumping, no re-running identical output when re-asked).

Usage:
  python scripts/bids_today.py                # today + tomorrow + this week
  python scripts/bids_today.py --scope today  # today only
  python scripts/bids_today.py --scope week   # through Saturday
"""
from __future__ import annotations
import argparse, json, sys
from datetime import date, datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
BIDS = BASE / "data" / "memory" / "active_bids.json"
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass


_ST = {"north carolina":"NC","south carolina":"SC","virginia":"VA",
       "west virginia":"WV","georgia":"GA","tennessee":"TN","kentucky":"KY",
       "alabama":"AL","florida":"FL","maryland":"MD","ohio":"OH",
       "pennsylvania":"PA","texas":"TX","new york":"NY"}
def _st(v):
    v = (v or "").strip()
    return _ST.get(v.lower(), v[:2].upper() if v else "")


def _pd(s):
    if not s: return None
    for f in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d-%b-%Y"):
        try: return datetime.strptime(str(s).strip()[:10], f).date()
        except Exception: pass
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scope", choices=["today", "tomorrow", "week", "all"],
                    default="week")
    args = ap.parse_args()

    bids = json.load(open(BIDS, encoding="utf-8"))
    today = date.today()
    tomorrow = today + timedelta(days=1)
    sat = today + timedelta(days=(5 - today.weekday()) % 7)  # this Sat

    buckets = {"today": [], "tomorrow": [], "week": []}
    for b in bids:
        d = _pd(b.get("due_date") or b.get("bid_due_date") or "")
        if not d:
            continue
        rec = {
            "proj": (b.get("project_name") or "")[:55],
            "gc": (b.get("gc_name") or b.get("gc") or "")[:28],
            "city": (b.get("city") or "")[:18],
            "st": _st(b.get("state") or ""),
            "src": (b.get("source") or "")[:6],
            "mi": (b.get("distance_miles")
                   if isinstance(b.get("distance_miles"), (int, float))
                   else b.get("distance_mi")),
            "due": d,
        }
        if d == today:
            buckets["today"].append(rec)
        elif d == tomorrow:
            buckets["tomorrow"].append(rec)
        elif today < d <= sat:
            buckets["week"].append(rec)

    def line(r):
        mi = f"{r['mi']:.0f}mi" if isinstance(r["mi"], (int, float)) else "—"
        # LOCAL star = NC/SC AND within ~2hr drive (≤120mi).
        d = r["mi"]
        # Known distance required — missing geocode must NOT earn the star.
        is_local = r["st"] in ("NC", "SC") and (
            isinstance(d, (int, float)) and d <= 120)
        star = " *" if is_local else ""
        # ALWAYS show source code as prefix (user 2026-05-26: source + distance
        # must be in every bid listing by default, never gated behind a flag).
        # Map by the truncated-to-6 prefix that the rec dict actually carries.
        src_raw = (r.get("src") or "").lower()
        src = ("BC" if src_raw.startswith("buildi") else
               "CC" if src_raw.startswith("constr") else
               "EM" if src_raw.startswith(("gmail", "email")) else
               "PC" if src_raw.startswith("procor") else
               "PK" if src_raw.startswith("parkwa") else
               "TG" if src_raw.startswith("togal") else
               (src_raw.upper()[:2] if src_raw else "??"))
        return (f"  [{src}] {mi:>6}  • {r['proj']} — {r['gc'] or '(GC ?)'} — "
                f"{r['city']}, {r['st']}{star}".rstrip())

    out = [f"*Bids due — {today.strftime('%a %b %-d' if hasattr(today,'strftime') else '%a %b %d')}*"
           if False else f"*Bids due (as of {today.isoformat()})*", ""]

    show = []
    if args.scope in ("today", "week", "all"):
        show.append(("DUE TODAY", buckets["today"]))
    if args.scope in ("tomorrow", "week", "all"):
        show.append(("DUE TOMORROW", buckets["tomorrow"]))
    if args.scope in ("week", "all"):
        show.append(("REST OF THIS WEEK (thru Sat)", buckets["week"]))

    for title, rows in show:
        out.append(f"*{title}* ({len(rows)})")
        if not rows:
            out.append("  — none —")
        for r in sorted(rows, key=lambda x: (x["due"], -(x["mi"] or 0 if isinstance(x["mi"], (int, float)) else 0))):
            out.append(line(r))
        out.append("")

    out.append("* = NC/SC (local). GC '?' = not captured in invite.")
    print("\n".join(out).rstrip())


if __name__ == "__main__":
    main()
