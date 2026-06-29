#!/usr/bin/env python3
r"""verified_report.py — render the OPEN pipeline straight from verified_rank_state.json
(memory's SINGLE SOURCE OF TRUTH: deduped, garbage-filtered, doc-read scope/value).

So when Nursultan asks on Telegram, the COUNT and the LIST both come from the
verified store — never the raw scrape cache (which runs a few higher pre-dedup).

  verified_report.py            -> summary headline (count + soonest deadlines)
  verified_report.py --list     -> ranked verified open list ($ high->low, in-radius, due)
"""
import argparse
import datetime
import json
import os
import sys
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VRS = os.path.join(ROOT, "data", "memory", "verified_rank_state.json")
RADIUS_MI = 100


def _num(x):
    try:
        return float(x)
    except Exception:
        return 0.0


def _due(p):
    s = str(p.get("due") or p.get("due_date") or "")[:10]
    for f in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.datetime.strptime(s, f).date()
        except Exception:
            pass
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="show the ranked verified open list")
    a = ap.parse_args()
    try:
        d = json.load(open(VRS, encoding="utf-8"))
    except Exception as e:
        print(f"verified rank state unreadable ({e}) — run verified_intelligence.py first")
        return 1

    projs = d.get("projects") or {}
    rows = [p for p in (projs.values() if isinstance(projs, dict) else projs)
            if isinstance(p, dict) and p.get("_status") != "garbage" and not p.get("_garbage_reason")]
    when = str(d.get("updated_at") or "")[:16].replace("T", " ")
    open_count = d.get("open_count")
    active = d.get("active_count")
    today = datetime.date.today()

    if not a.list:
        wk = today + datetime.timedelta(days=7)
        dues = [x for x in (_due(p) for p in rows) if x]
        dt = sum(1 for x in dues if x == today)
        dwk = sum(1 for x in dues if today <= x <= wk)
        inr = sum(1 for p in rows if p.get("mi") not in (None, "") and _num(p.get("mi")) <= RADIUS_MI)
        print(f"OPEN PIPELINE (verified | refreshed {when})")
        print(f"  - OPEN: {open_count if open_count is not None else len(rows)}"
              f"   |   ACTIVELY BIDDABLE: {active if active is not None else '?'}")
        print(f"  - in-radius (<={RADIUS_MI}mi + Parkway): {inr}"
              f"   |   soonest: {dt} due today | {dwk} within 7 days")
        return 0

    def sort_key(p):
        dd = _due(p) or datetime.date(2999, 1, 1)
        in_rad = 0 if (p.get("mi") not in (None, "") and _num(p.get("mi")) <= RADIUS_MI) else 1
        return (-_num(p.get("value_high")), in_rad, dd)

    rows.sort(key=sort_key)
    print(f"OPEN (verified): {open_count if open_count is not None else len(rows)} projects "
          f"  refreshed {when} | $ high->low, in-radius first, due soonest\n")
    print(f"{'#':>3}  {'$ band (CCF)':21} {'mi':>5} {'due':10}  project")
    print("-" * 92)
    for i, p in enumerate(rows, 1):
        vl, vh = p.get("value_low"), p.get("value_high")
        band = f"${int(vl):,}-${int(vh):,}" if vl and vh else "(scope read, no $)"
        mi = p.get("mi")
        mis = f"{_num(mi):.0f}" if mi not in (None, "") else "?"
        dd = _due(p)
        dds = dd.strftime("%m/%d/%Y") if dd else "—"
        nm = str(p.get("project") or p.get("project_name") or "?")[:46]
        print(f"{i:>3}  {band:21} {mis:>5} {dds:10}  {nm}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
