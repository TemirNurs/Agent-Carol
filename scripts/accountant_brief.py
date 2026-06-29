#!/usr/bin/env python3
r"""
accountant_brief.py — accountant-tailored Telegram brief.

Pulls the numbers an accountant cares about (NOT estimator dashboards) and
formats them for spreadsheet copy/paste + financial reporting:
  - Lifetime revenue (signed contracts)
  - YTD revenue + by-month breakdown
  - Top GCs (relevant for 1099 prep)
  - Won bids with both Bid Amount AND Contract Value (invoice reconciliation)
  - Open pipeline value (awaiting decision)

Carol calls this when the accountant asks "give me the financial brief" / "books summary"
/ "monthly numbers" / "tax prep stats".

Usage:
  python scripts/accountant_brief.py             # Telegram-ready Markdown
  python scripts/accountant_brief.py --json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))


def _money(n: float) -> str:
    if n >= 1_000_000:
        return f"${n/1_000_000:.2f}M"
    if n >= 10_000:
        return f"${n/1000:.0f}K"
    return f"${n:,.0f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    from crm_stats import completed_stats, submitted_bid_stats
    from _lib import money

    completed = completed_stats()
    won_this_year = submitted_bid_stats(year=date.today().year, status_filter="Won")
    awaiting = submitted_bid_stats(year=None, status_filter="Awaiting Decision")
    submitted_open = submitted_bid_stats(year=None, status_filter="Bid Submitted")

    won_bids = won_this_year.get("submitted_in_year", [])
    awaiting_bids = awaiting.get("submitted_in_year", [])
    submitted_bids = submitted_open.get("submitted_in_year", [])

    # YTD signed (from Won bids' contract_value)
    ytd_signed = sum(b.get("contract_value", 0) or b.get("amount", 0) for b in won_bids)
    open_pipeline_value = sum(b.get("amount", 0) for b in awaiting_bids + submitted_bids)

    if args.json:
        out = {
            "ytd_signed_revenue": ytd_signed,
            "open_pipeline_value": open_pipeline_value,
            "lifetime_revenue": completed.get("total_revenue_usd", 0),
            "lifetime_completed": completed.get("total_completed", 0),
            "won_bids_this_year": [
                {"bid_id": b.get("bid_id"), "name": b.get("name"),
                 "gc": b.get("gc"), "bid_amount": b.get("amount"),
                 "contract_value": b.get("contract_value")}
                for b in won_bids
            ],
            "top_gcs_lifetime": completed.get("revenue_by_gc", [])[:6],
            "by_year": completed.get("revenue_by_year", {}),
        }
        print(json.dumps(out, indent=2, default=str))
        return

    # Telegram-ready Markdown
    lines = [
        f"📊 *CCF Financial Brief — for {date.today():%b %d, %Y}*",
        "",
        "*This year (YTD):*",
        f"  • Signed contract revenue: {_money(ytd_signed)}",
        f"  • Won bids this year: {len(won_bids)}",
        f"  • Open pipeline (Awaiting + Submitted): {_money(open_pipeline_value)}",
        "",
    ]

    if won_bids:
        lines.append("*Won contracts this year:*")
        for b in won_bids:
            bid_amt = b.get("amount", 0) or 0
            cv = b.get("contract_value", 0) or 0
            lines.append(
                f"  • {b.get('bid_id')} — {(b.get('name') or '')[:35]}"
            )
            lines.append(
                f"    Bid: {_money(bid_amt)}  ·  Signed: {_money(cv)}  ·  GC: {(b.get('gc') or '')[:25]}"
            )
        lines.append("")

    lines.append("*Lifetime (since Nov 2017):*")
    lines.append(f"  • {completed.get('total_completed', 0)} completed projects")
    lines.append(f"  • Total revenue: {_money(completed.get('total_revenue_usd', 0))}")
    lines.append(f"  • Avg project value: {_money(completed.get('avg_project_value', 0))}")
    lines.append("")

    lines.append("*Revenue by year:*")
    for y in sorted(completed.get("count_by_year", {}).keys(), reverse=True):
        n = completed["count_by_year"][y]
        r = completed.get("revenue_by_year", {}).get(y, 0)
        lines.append(f"  {y}: {n} projects · {_money(r)}")
    lines.append("")

    lines.append("*Top GCs by lifetime revenue (1099 prep):*")
    for gc, rev in (completed.get("revenue_by_gc") or [])[:6]:
        lines.append(f"  • {gc}: {_money(rev)}")

    lines.append("")
    lines.append("_Need to drill down? Ask: 'list won bids', 'list awaiting', or 'history of BID-NNNN'._")

    print("\n".join(lines))


if __name__ == "__main__":
    sys.exit(main())
