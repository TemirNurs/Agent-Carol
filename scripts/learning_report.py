#!/usr/bin/env python3
"""
learning_report.py — Monthly learning analysis for CCF bid performance.

Analyzes outcomes to find:
- Win rate by GC, facility type, pricing tier
- Average $/SF by facility type
- Recommendations for pricing and GC prioritization

Usage:
  python scripts/learning_report.py
  python scripts/learning_report.py --json
  python scripts/learning_report.py --save
"""

import argparse
import json
from collections import defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTCOMES_DIR = ROOT / "data" / "memory" / "outcomes"
GC_DIR = ROOT / "data" / "memory" / "gc"
PIPELINE_FILE = ROOT / "data" / "memory" / "pipeline.json"
REPORTS_DIR = ROOT / "data" / "reports"


def load_outcomes() -> list[dict]:
    """Load all outcome records."""
    outcomes = []
    if OUTCOMES_DIR.exists():
        for f in OUTCOMES_DIR.glob("*.json"):
            try:
                outcomes.append(json.loads(f.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                continue
    return outcomes


def load_pipeline_outcomes() -> list[dict]:
    """Fallback: load outcomes from pipeline.json."""
    if not PIPELINE_FILE.exists():
        return []
    pipeline = json.loads(PIPELINE_FILE.read_text(encoding="utf-8"))
    return [
        proj for proj in pipeline.get("projects", {}).values()
        if proj.get("outcome") in ("won", "lost", "no_award")
    ]


def analyze_by_gc(outcomes: list[dict]) -> dict:
    """Win rate and avg bid by GC."""
    gc_stats = defaultdict(lambda: {"bids": 0, "wins": 0, "total_bid": 0, "total_won": 0})

    for o in outcomes:
        gc = o.get("gc", "Unknown")
        gc_stats[gc]["bids"] += 1
        gc_stats[gc]["total_bid"] += o.get("bid_amount", 0) or 0
        if o.get("outcome") == "won":
            gc_stats[gc]["wins"] += 1
            gc_stats[gc]["total_won"] += o.get("contract_amount") or o.get("bid_amount", 0) or 0

    # Calculate rates
    for gc, stats in gc_stats.items():
        stats["win_rate"] = round(stats["wins"] / stats["bids"] * 100, 0) if stats["bids"] else 0
        stats["avg_bid"] = round(stats["total_bid"] / stats["bids"], 0) if stats["bids"] else 0

    return dict(gc_stats)


def analyze_by_tier(outcomes: list[dict]) -> dict:
    """Win rate by pricing tier."""
    tier_stats = defaultdict(lambda: {"bids": 0, "wins": 0})

    for o in outcomes:
        tier = o.get("pricing_tier", "TARGET")
        tier_stats[tier]["bids"] += 1
        if o.get("outcome") == "won":
            tier_stats[tier]["wins"] += 1

    for tier, stats in tier_stats.items():
        stats["win_rate"] = round(stats["wins"] / stats["bids"] * 100, 0) if stats["bids"] else 0

    return dict(tier_stats)


def generate_recommendations(gc_stats: dict, tier_stats: dict) -> list[str]:
    """Generate actionable recommendations."""
    recs = []

    # Recommend best GCs
    top_gcs = sorted(gc_stats.items(),
                     key=lambda x: (x[1]["win_rate"], x[1]["bids"]),
                     reverse=True)
    for gc, stats in top_gcs[:3]:
        if stats["bids"] >= 2 and stats["win_rate"] >= 50:
            recs.append(f"Prioritize {gc} — {stats['win_rate']:.0f}% win rate over {stats['bids']} bids")

    # Flag low-performing GCs
    for gc, stats in gc_stats.items():
        if stats["bids"] >= 3 and stats["win_rate"] < 20:
            recs.append(f"Review pricing for {gc} — only {stats['win_rate']:.0f}% win rate over {stats['bids']} bids")

    # Tier recommendations
    for tier, stats in tier_stats.items():
        if stats["bids"] >= 3 and stats["win_rate"] < 25:
            recs.append(f"{tier} tier may be priced too high — {stats['win_rate']:.0f}% win rate")
        elif stats["bids"] >= 3 and stats["win_rate"] > 75:
            recs.append(f"{tier} tier may be underpriced — {stats['win_rate']:.0f}% win rate (leaving money on table)")

    if not recs:
        recs.append("Need more completed bids to generate meaningful recommendations")

    return recs


def build_report() -> dict:
    """Build full learning report."""
    outcomes = load_outcomes()
    if not outcomes:
        outcomes = load_pipeline_outcomes()

    if not outcomes:
        return {
            "date": date.today().isoformat(),
            "total_outcomes": 0,
            "message": "No completed bids to analyze. Win some, lose some, then come back.",
        }

    total = len(outcomes)
    wins = sum(1 for o in outcomes if o.get("outcome") == "won")
    losses = sum(1 for o in outcomes if o.get("outcome") == "lost")

    gc_stats = analyze_by_gc(outcomes)
    tier_stats = analyze_by_tier(outcomes)
    recommendations = generate_recommendations(gc_stats, tier_stats)

    total_bid_value = sum(o.get("bid_amount", 0) or 0 for o in outcomes)
    total_won_value = sum(
        (o.get("contract_amount") or o.get("bid_amount", 0) or 0)
        for o in outcomes if o.get("outcome") == "won"
    )

    return {
        "date": date.today().isoformat(),
        "total_outcomes": total,
        "wins": wins,
        "losses": losses,
        "overall_win_rate": round(wins / total * 100, 0) if total else 0,
        "total_bid_value": round(total_bid_value, 0),
        "total_won_value": round(total_won_value, 0),
        "by_gc": gc_stats,
        "by_tier": tier_stats,
        "recommendations": recommendations,
    }


def format_report(report: dict) -> str:
    """Format report for human reading."""
    if report.get("total_outcomes", 0) == 0:
        return report.get("message", "No data.")

    lines = [
        "CCF LEARNING REPORT",
        f"Date: {report['date']}",
        "=" * 50,
        f"Total bids tracked: {report['total_outcomes']}",
        f"Won: {report['wins']} | Lost: {report['losses']} | Win rate: {report['overall_win_rate']}%",
        f"Total bid value: ${report['total_bid_value']:,.0f}",
        f"Total won value: ${report['total_won_value']:,.0f}",
        "",
        "BY GC:",
        "-" * 40,
    ]

    for gc, stats in sorted(report["by_gc"].items(),
                            key=lambda x: -x[1]["bids"]):
        lines.append(
            f"  {gc}: {stats['bids']} bids, {stats['wins']} won "
            f"({stats['win_rate']}%), avg ${stats['avg_bid']:,.0f}"
        )

    lines.extend(["", "BY TIER:", "-" * 40])
    for tier, stats in report["by_tier"].items():
        lines.append(f"  {tier}: {stats['bids']} bids, {stats['wins']} won ({stats['win_rate']}%)")

    lines.extend(["", "RECOMMENDATIONS:", "-" * 40])
    for rec in report["recommendations"]:
        lines.append(f"  * {rec}")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="CCF Learning Report")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--save", action="store_true", help="Save to data/reports/")
    args = parser.parse_args()

    report = build_report()

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(format_report(report))

    if args.save:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        out = REPORTS_DIR / f"learning_report_{date.today().isoformat()}.json"
        out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nSaved: {out}")


if __name__ == "__main__":
    main()
