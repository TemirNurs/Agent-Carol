#!/usr/bin/env python3
"""
Authoritative CRM counts — Carol MUST use this for any number question
about completed projects, GCs, or pipeline. No more hallucinations.

Reads LIVE from Google Sheets via gspread on every invocation. No data cache.

Usage:
  python scripts/crm_stats.py                    # full report
  python scripts/crm_stats.py --gcs              # GC counts only
  python scripts/crm_stats.py --completed        # completed-project counts
  python scripts/crm_stats.py --pipeline         # active bid counts
  python scripts/crm_stats.py --json             # machine-readable
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = Path(__file__).resolve().parent.parent
GC_FILE        = BASE / "data" / "memory" / "gc_crm.json"
COMPLETED_FILE = BASE / "data" / "memory" / "completed_projects.json"
ACTIVE_BIDS    = BASE / "data" / "memory" / "active_bids.json"


def load(path, default=None):
    if not path.exists():
        return default if default is not None else []
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return default if default is not None else []


def gc_stats():
    gcs = load(GC_FILE, default={})
    completed = load(COMPLETED_FILE, default=[])

    # All GCs in directory
    total_gcs = len(gcs)

    # GCs by relationship status
    by_status = Counter(g.get("relationship_status", "Unknown") for g in gcs.values())

    # GCs we've actually completed projects with (from Completed Projects sheet)
    gcs_with_completed = set()
    for c in completed:
        gc = (c.get("gc") or "").strip()
        if gc:
            gcs_with_completed.add(gc.lower().rstrip())

    # Top GCs by completed project count
    completed_by_gc = Counter()
    for c in completed:
        gc = (c.get("gc") or "").strip()
        if gc:
            completed_by_gc[gc] += 1

    return {
        "total_in_directory": total_gcs,
        "gcs_with_completed_work": len(gcs_with_completed),
        "by_relationship_status": dict(by_status),
        "top_gcs_by_completed_count": completed_by_gc.most_common(10),
    }


def completed_stats():
    completed = load(COMPLETED_FILE, default=[])
    total = len(completed)
    by_year = Counter(str(c.get("year", "?")).split(".")[0] for c in completed)
    by_facility = Counter((c.get("facility_type") or "Unknown").strip() for c in completed)
    by_state = Counter((c.get("state") or "?").strip() for c in completed)

    def cv(c):
        """Parse contract_value — it can be int, float, or string like '$74,000'."""
        v = c.get("contract_value", 0)
        if isinstance(v, (int, float)):
            return v
        if isinstance(v, str):
            import re
            m = re.search(r"[\d,]+(?:\.\d+)?", v.replace(",", "").replace("$", ""))
            if m:
                try:
                    return float(m.group())
                except ValueError:
                    pass
            # Try original with comma stripped
            cleaned = re.sub(r"[^\d.]", "", v)
            try:
                return float(cleaned) if cleaned else 0
            except ValueError:
                return 0
        return 0

    total_revenue = sum(cv(c) for c in completed)

    # Revenue by year
    revenue_by_year = {}
    count_by_year = {}
    for c in completed:
        y = str(c.get("year", "?")).split(".")[0]
        revenue_by_year[y] = revenue_by_year.get(y, 0) + cv(c)
        count_by_year[y] = count_by_year.get(y, 0) + 1
    revenue_by_year_sorted = dict(sorted(revenue_by_year.items()))

    # Revenue by GC
    revenue_by_gc = {}
    for c in completed:
        gc = (c.get("gc") or "?").strip()
        revenue_by_gc[gc] = revenue_by_gc.get(gc, 0) + cv(c)
    revenue_by_gc_sorted = sorted(revenue_by_gc.items(), key=lambda x: -x[1])

    # Revenue by facility type
    revenue_by_facility = {}
    for c in completed:
        fac = (c.get("facility_type") or "Unknown").strip()
        revenue_by_facility[fac] = revenue_by_facility.get(fac, 0) + cv(c)
    revenue_by_facility_sorted = sorted(revenue_by_facility.items(), key=lambda x: -x[1])

    # Best year (by revenue)
    best_year = max(revenue_by_year.items(), key=lambda x: x[1]) if revenue_by_year else (None, 0)
    best_gc   = revenue_by_gc_sorted[0] if revenue_by_gc_sorted else (None, 0)

    return {
        "total_completed": total,
        "by_year": dict(by_year.most_common()),
        "by_facility_type": dict(by_facility.most_common()),
        "by_state": dict(by_state.most_common(10)),
        "total_revenue_usd": total_revenue,
        "revenue_by_year": revenue_by_year_sorted,
        "count_by_year": count_by_year,
        "revenue_by_gc": revenue_by_gc_sorted[:10],
        "revenue_by_facility_type": revenue_by_facility_sorted[:10],
        "best_year_by_revenue": best_year,
        "best_gc_by_revenue": best_gc,
        "avg_project_value": total_revenue / total if total else 0,
    }


def pipeline_stats():
    bids = load(ACTIVE_BIDS, default=[])
    by_source = Counter(b.get("source", "?") for b in bids)
    return {
        "total_active_bids": len(bids),
        "by_source": dict(by_source),
    }


def submitted_bid_stats(year=None, status_filter=None):
    """Aggregate Bid Log statuses from the live CRM Sheet.

    Returns counts of bids by status, optionally filtered to a year (using
    Bid Submitted Date) and/or a specific status. Status options: ITB Received,
    Estimating, Pending Review, Bid Submitted, Awaiting Decision, Won, Lost,
    No Bid, Withdrawn.
    """
    sys.path.insert(0, str(BASE / "scripts"))
    try:
        from crm_lib import all_records
        rows = all_records("Bid Log")
    except Exception as e:
        return {"error": f"could not read Bid Log: {e}"}

    from datetime import datetime as _dt

    def parse_year(s):
        if not s:
            return None
        s = str(s).strip()
        for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%Y/%m/%d"):
            try:
                return _dt.strptime(s, fmt).year
            except ValueError:
                pass
        # Last resort — first 4-digit number is the year
        import re as _re
        m = _re.search(r"(20\d{2})", s)
        return int(m.group(1)) if m else None

    by_status = Counter()
    by_year_status = {}
    submitted_total = 0
    submitted_revenue = 0
    won_revenue = 0
    lost_count = 0
    won_count = 0
    awaiting_count = 0
    submitted_in_year = []

    def parse_amount(v):
        if not v:
            return 0
        if isinstance(v, (int, float)):
            return v
        import re as _re
        cleaned = _re.sub(r"[^\d.]", "", str(v))
        try: return float(cleaned) if cleaned else 0
        except ValueError: return 0

    for r in rows:
        status = (r.get("Status") or "").strip()
        if not status:
            continue
        by_status[status] += 1
        sub_year = parse_year(r.get("Bid Submitted Date"))
        if sub_year:
            yk = (sub_year, status)
            by_year_status[yk] = by_year_status.get(yk, 0) + 1

        # Anything that's been submitted (incl. Won/Lost/Awaiting)
        is_submitted = status in ("Bid Submitted", "Awaiting Decision", "Won", "Lost")
        if is_submitted:
            submitted_total += 1
            amt = parse_amount(r.get("Bid Amount ($)"))
            submitted_revenue += amt
            # Apply year + status filters
            year_match = (year is None) or (sub_year == year)
            status_match = (status_filter is None) or (status.lower() == status_filter.lower())
            if year_match and status_match:
                submitted_in_year.append({
                    "bid_id": r.get("Bid #"),
                    "internal_id": r.get("Internal ID", ""),  # stable UUID
                    "name": r.get("Project Name"),
                    "gc": r.get("GC / Client"),
                    "amount": amt,                                          # Bid Amount (what we proposed)
                    "contract_value": parse_amount(r.get("Contract Value ($)")),  # signed contract $
                    "status": status,
                    "submitted": r.get("Bid Submitted Date"),
                    "loss_reason": (r.get("Loss Reason") or "").strip(),
                    "notes": (r.get("Notes") or "").strip(),
                })
        if status == "Won":
            won_count += 1
            won_revenue += parse_amount(r.get("Contract Value ($)") or r.get("Bid Amount ($)"))
        if status == "Lost":
            lost_count += 1
        if status == "Awaiting Decision":
            awaiting_count += 1

    # Compute submission counts by year (any status that implies submitted)
    submitted_by_year = Counter()
    for (y, st), n in by_year_status.items():
        if st in ("Bid Submitted", "Awaiting Decision", "Won", "Lost"):
            submitted_by_year[y] += n

    out = {
        "total_with_status": sum(by_status.values()),
        "by_status": dict(by_status.most_common()),
        "submitted_total_alltime": submitted_total,
        "submitted_revenue_alltime": submitted_revenue,
        "won_count": won_count,
        "won_revenue": won_revenue,
        "lost_count": lost_count,
        "awaiting_count": awaiting_count,
        "submitted_by_year": dict(sorted(submitted_by_year.items(), reverse=True)),
        "win_rate": (won_count / (won_count + lost_count) * 100) if (won_count + lost_count) else 0,
    }
    if year or status_filter:
        out["filter_year"] = year
        out["filter_status"] = status_filter
        out["submitted_in_year"] = submitted_in_year
        out["submitted_count_in_year"] = len(submitted_in_year)
        out["submitted_revenue_in_year"] = sum(b["amount"] for b in submitted_in_year)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gcs", action="store_true")
    ap.add_argument("--completed", action="store_true")
    ap.add_argument("--pipeline", action="store_true")
    ap.add_argument("--submitted", action="store_true",
                    help="Bids submitted (incl. Won/Lost/Awaiting) by year + win rate")
    ap.add_argument("--year", type=int, help="Filter --submitted to a specific year")
    ap.add_argument("--status", type=str, default=None,
                    help="Filter --submitted to a specific status (e.g. 'Awaiting Decision', 'Lost', 'Won')")
    ap.add_argument("--status-breakdown", action="store_true",
                    help="One-line authoritative status counts — quote verbatim, do NOT paraphrase")
    ap.add_argument("--list-status", type=str, default=None,
                    help="List individual bids matching this status (e.g. 'Lost', 'Won', 'Awaiting Decision'). Clean output, no preamble. For Lost: includes Loss Reason column.")
    ap.add_argument("--history-brief", action="store_true",
                    help="Telegram-ready Markdown summary of CCF lifetime history. Carol must QUOTE VERBATIM.")
    ap.add_argument("--loss-analysis", action="store_true",
                    help="Aggregate Loss Reason column across all Lost bids — count + dollar value per reason.")
    ap.add_argument("--loss-trends", action="store_true",
                    help="Loss patterns: by GC, by month, by reason×GC. Surfaces 'we always lose to X on Y'.")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    # History-brief mode: lifetime CCF stats in a Telegram-ready Markdown block.
    # Used when the user asks "how many years operated", "lifetime revenue",
    # "how much money have we earned", "won projects history", etc.
    if getattr(args, 'history_brief', False):
        c = completed_stats()
        from datetime import date as _date
        founded = _date(2017, 11, 16)
        years = round((_date.today() - founded).days / 365.25, 1)

        def _money(n):
            if n >= 1_000_000: return f"${n/1_000_000:.2f}M"
            if n >= 1000:       return f"${n/1000:.0f}K"
            return f"${n:,.0f}"

        lines = [
            f"*CCF lifetime — {years} years operating* (since Nov 2017)",
            "",
            f"📊 *{c['total_completed']} completed projects · {_money(c['total_revenue_usd'])} total revenue*",
            f"  Avg project: {_money(c.get('avg_project_value', 0))}",
            "",
            "*Revenue by year:*",
        ]
        for y in sorted(c.get("count_by_year", {}).keys(), reverse=True):
            n = c["count_by_year"][y]
            r = c.get("revenue_by_year", {}).get(y, 0)
            lines.append(f"  {y}: {n} projects · {_money(r)}")
        if c.get("best_year_by_revenue"):
            by, br = c["best_year_by_revenue"]
            if by: lines.append(f"\n_Best year: {by} ({_money(br)})_")
        lines.append("")
        lines.append("*Top GCs by lifetime revenue:*")
        for gc, rev in (c.get("revenue_by_gc") or [])[:6]:
            lines.append(f"  • {gc}: {_money(rev)}")
        # Facility type — show count + revenue together
        rev_by_ft = dict(c.get("revenue_by_facility_type") or [])
        if c.get("by_facility_type"):
            lines.append("")
            lines.append("*By facility type (top 6):*")
            facilities = sorted(c["by_facility_type"].items(),
                                key=lambda kv: -rev_by_ft.get(kv[0], 0))[:6]
            for ft, cnt in facilities:
                rev = rev_by_ft.get(ft, 0)
                lines.append(f"  • {ft}: {cnt} projects · {_money(rev)}")
        print("\n".join(lines))
        return

    # List-by-status mode: print clean list of bids matching a status — no
    # statistical preamble, no "lifetime" footnotes that confuse the LLM.
    # Used when the user asks "give me list of lost projects", "show me won bids",
    # "what's awaiting decision", etc.
    if getattr(args, 'list_status', None):
        s = submitted_bid_stats(year=args.year, status_filter=args.list_status)
        bids = s.get("submitted_in_year", [])
        year_label = f" in {args.year}" if args.year else ""
        print(f"{len(bids)} bids with status '{args.list_status}'{year_label}:")
        if not bids:
            return
        total_bid = 0
        total_contract = 0
        is_lost = args.list_status.lower() == "lost"
        is_won  = args.list_status.lower() == "won"
        for b in bids:
            amt = b.get("amount", 0) or 0
            cv = b.get("contract_value", 0) or 0
            total_bid += amt
            total_contract += cv
            if is_won:
                # For Won bids show BOTH proposed (Bid Amount) and signed (Contract Value)
                cv_str = f"${cv:>10,.0f}" if cv else "    (no CV)"
                print(f"  {b.get('bid_id','?'):10}  bid=${amt:>10,.0f}  contract={cv_str}  "
                      f"{(b.get('name') or '?')[:40]:40}  ({(b.get('gc') or '?')[:22]})")
            else:
                base = f"  {b.get('bid_id','?'):10}  ${amt:>10,.0f}  {(b.get('name') or '?')[:45]:45}  ({(b.get('gc') or '?')[:22]})"
                if is_lost:
                    rea = b.get("loss_reason", "") or "(no reason recorded)"
                    print(f"{base}  — {rea[:80]}")
                else:
                    print(base)
        if is_won:
            print(f"\nTotal proposed (Bid Amount):  ${total_bid:,.0f}")
            print(f"Total signed (Contract Value): ${total_contract:,.0f}")
            print(f"  ** When asked 'what was the contract amount?', use Contract Value, NOT Bid Amount **")
        else:
            print(f"\nTotal value: ${total_bid:,.0f}")
        return

    # Loss trends: break out losses by GC, by month, by reason — find patterns
    if getattr(args, 'loss_trends', False):
        from collections import Counter, defaultdict
        s = submitted_bid_stats(year=args.year, status_filter="Lost")
        bids = s.get("submitted_in_year", [])
        if not bids:
            print("No lost bids found.")
            return
        year_label = f" in {args.year}" if args.year else ""
        print(f"Loss trends{year_label} — {len(bids)} lost bids, ${sum(b.get('amount',0) for b in bids):,.0f} total\n")

        # By GC
        by_gc = Counter()
        gc_value = defaultdict(float)
        gc_reasons = defaultdict(Counter)
        for b in bids:
            gc = (b.get("gc") or "Unknown").strip()
            by_gc[gc] += 1
            gc_value[gc] += b.get("amount", 0) or 0
            gc_reasons[gc][(b.get("loss_reason") or "(no reason)").strip()] += 1

        print("=== By GC (most lost first) ===")
        for gc, n in by_gc.most_common():
            print(f"  {n:2}  ${gc_value[gc]:>10,.0f}   {gc[:35]}")
            top_reasons = gc_reasons[gc].most_common(3)
            for rea, rn in top_reasons:
                print(f"        └─ {rn}× {rea[:60]}")
        print()

        # By month
        import re as _re
        from datetime import datetime as _dt
        def parse_month(date_str):
            if not date_str: return None
            for fmt in ("%a, %d %b %Y", "%m/%d/%Y", "%Y-%m-%d"):
                try:
                    return _dt.strptime(str(date_str).strip(), fmt).strftime("%Y-%m")
                except ValueError:
                    continue
            m = _re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", str(date_str))
            if m:
                return f"{m.group(3)}-{int(m.group(1)):02d}"
            return None
        by_month = Counter()
        month_value = defaultdict(float)
        for b in bids:
            mon = parse_month(b.get("submitted"))
            if mon:
                by_month[mon] += 1
                month_value[mon] += b.get("amount", 0) or 0
        if by_month:
            print("=== By month (submission month) ===")
            for mon in sorted(by_month.keys()):
                print(f"  {mon}   {by_month[mon]:2} losses   ${month_value[mon]:,.0f}")
            print()

        # By reason × GC matrix (top reasons only)
        print("=== Reason × GC concentration ===")
        all_reasons = Counter()
        for b in bids:
            all_reasons[(b.get("loss_reason") or "(no reason)").strip()] += 1
        for rea, total_n in all_reasons.most_common():
            gcs = Counter()
            val = 0
            for b in bids:
                if (b.get("loss_reason") or "(no reason)").strip() == rea:
                    gcs[(b.get("gc") or "Unknown").strip()] += 1
                    val += b.get("amount", 0) or 0
            top_gc = gcs.most_common(1)[0] if gcs else ("?", 0)
            print(f"  {total_n:2}× ${val:>9,.0f}  {rea[:55]}")
            for gc, n in gcs.most_common():
                print(f"        └─ {n}× {gc[:35]}")
        return

    # Loss analysis: aggregate Loss Reason column for all Lost bids
    if getattr(args, 'loss_analysis', False):
        from collections import Counter
        s = submitted_bid_stats(year=args.year, status_filter="Lost")
        bids = s.get("submitted_in_year", [])
        if not bids:
            print("No lost bids found.")
            return
        reasons = Counter()
        reason_value = {}
        for b in bids:
            rea = (b.get("loss_reason") or "").strip() or "(no reason recorded)"
            reasons[rea] += 1
            amt = b.get("amount", 0) or 0
            reason_value[rea] = reason_value.get(rea, 0) + amt
        year_label = f" in {args.year}" if args.year else ""
        total_value = sum(reason_value.values())
        print(f"Loss reason analysis{year_label}: {len(bids)} lost bids, ${total_value:,.0f} total value")
        print()
        for rea, n in reasons.most_common():
            val = reason_value.get(rea, 0)
            print(f"  {n:3}  ${val:>10,.0f}   {rea[:80]}")
        print()
        print(f"TOTAL: {len(bids)} bids, ${total_value:,.0f}  (do NOT recompute — quote this number)")
        return

    # Status breakdown mode: print canonical counts and exit. Used by Carol when
    # the user asks "what are the statuses" / "how many awaiting" — no LLM
    # paraphrasing, just facts.
    if args.status_breakdown:
        s = submitted_bid_stats(year=args.year)
        by_status = s.get("by_status", {})
        # Stable order
        order = ["Bid Submitted", "Awaiting Decision", "Won", "Lost", "On Hold",
                 "ITB Received", "Estimating", "Pending Review"]
        ordered = [(k, by_status[k]) for k in order if k in by_status]
        for k, v in by_status.items():
            if k not in order:
                ordered.append((k, v))
        total_with_status = sum(by_status.values())
        year_label = f" in {args.year}" if args.year else ""
        print(f"Bid Log status breakdown{year_label}: {total_with_status} bids total")
        for k, v in ordered:
            print(f"  {v:3}  {k}")
        return

    out = {}
    show_all = not (args.gcs or args.completed or args.pipeline or args.submitted)
    if args.gcs or show_all:       out["gcs"] = gc_stats()
    if args.completed or show_all: out["completed"] = completed_stats()
    if args.pipeline or show_all:  out["pipeline"] = pipeline_stats()
    if args.submitted or show_all:
        out["submitted"] = submitted_bid_stats(year=args.year, status_filter=args.status)

    if args.json:
        print(json.dumps(out, indent=2))
        return

    if "gcs" in out:
        g = out["gcs"]
        print(f"=== GC Directory ===")
        print(f"  Total GCs in directory:        {g['total_in_directory']}")
        print(f"  GCs we've completed work with: {g['gcs_with_completed_work']}")
        print(f"  By relationship status:")
        for s, n in g["by_relationship_status"].items():
            print(f"    {s:<20} {n}")
        print(f"  Top GCs by completed count:")
        for name, n in g["top_gcs_by_completed_count"]:
            print(f"    {n:>3}  {name}")
        print()

    if "completed" in out:
        c = out["completed"]
        print(f"=== Completed Projects ===")
        print(f"  Total completed projects:      {c['total_completed']}")
        rev = c["total_revenue_usd"]
        if rev:
            print(f"  Total recorded revenue:        ${rev:,.0f}")
            print(f"  Avg project value:             ${c['avg_project_value']:,.0f}")
            best_y, best_y_rev = c["best_year_by_revenue"]
            if best_y:
                print(f"  Best year by revenue:          {best_y} (${best_y_rev:,.0f})")
            best_gc_name, best_gc_rev = c["best_gc_by_revenue"]
            if best_gc_name:
                print(f"  Top GC by revenue:             {best_gc_name} (${best_gc_rev:,.0f})")
        print(f"\n  By YEAR (count + revenue):")
        for y, count in c["count_by_year"].items():
            r = c["revenue_by_year"].get(y, 0)
            print(f"    {y}: {count:>2} projects  ${r:>10,.0f}")
        print(f"\n  Top GCs by revenue:")
        for gc, r in c["revenue_by_gc"][:8]:
            print(f"    ${r:>10,.0f}  {gc}")
        print(f"\n  By facility type (count):")
        for fac, n in list(c["by_facility_type"].items())[:6]:
            r = dict(c["revenue_by_facility_type"]).get(fac, 0)
            print(f"    {n:>2} projects  ${r:>10,.0f}  {fac}")
        print(f"\n  By state (count):")
        for st, n in list(c["by_state"].items())[:6]:
            print(f"    {n:>2}  {st}")
        print()

    if "pipeline" in out:
        p = out["pipeline"]
        print(f"=== Active Pipeline ===")
        print(f"  Total active bids: {p['total_active_bids']}")
        for src, n in p["by_source"].items():
            print(f"    {n:>3}  {src}")
        print()

    if "submitted" in out:
        s = out["submitted"]
        if "error" in s:
            print(f"=== Submitted Bids ===\n  ERROR: {s['error']}")
        else:
            print(f"=== Bids Submitted (Bid Log statuses) ===")
            print(f"  Total bids tracked with status: {s['total_with_status']}")
            print(f"  By status:")
            for st, n in s["by_status"].items():
                print(f"    {n:>3}  {st}")
            print(f"\n  Lifetime submitted (any post-submission status): {s['submitted_total_alltime']}")
            if s.get("submitted_revenue_alltime"):
                print(f"  Lifetime submitted bid value: ${s['submitted_revenue_alltime']:,.0f}")
            print(f"  Won: {s['won_count']}  |  Lost: {s['lost_count']}  |  Awaiting: {s['awaiting_count']}")
            if (s["won_count"] + s["lost_count"]) > 0:
                print(f"  Win rate (Won / (Won+Lost)): {s['win_rate']:.1f}%")
            if s["submitted_by_year"]:
                print(f"\n  Submitted by year:")
                for y, n in s["submitted_by_year"].items():
                    print(f"    {y}: {n}")
            if "filter_year" in s:
                f_y = s.get("filter_year") or "all years"
                f_s = s.get("filter_status") or "all submitted statuses"
                print(f"\n  === Filtered to year={f_y}, status={f_s} ===")
                print(f"  Matched: {s['submitted_count_in_year']} bids")
                print(f"  Bid value: ${s['submitted_revenue_in_year']:,.0f}")
                for b in s["submitted_in_year"]:
                    print(f"    {b['bid_id']:<10} {b['status']:<18} ${b['amount']:>9,.0f}  {b['name'][:48]}  ({b['gc'][:25]})")


if __name__ == "__main__":
    main()
