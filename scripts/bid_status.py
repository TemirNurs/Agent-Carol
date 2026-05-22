#!/usr/bin/env python3
"""
Unified bid-status intelligence for Carol.

Single source of truth for where every bid is in its lifecycle. Derives status
from filesystem state (project files) + portal metadata (BC/CC scraper output)
+ explicit overrides written via this CLI.

Lifecycle states (in order):
  invited        — in BC/CC/email, no decision yet
  accepted       — we accepted the invitation (BC shows "Accepted")
  docs_pulled    — bid_docs/ has at least one real PDF
  sow_done       — sow.json or SOW_*.md exists
  takeoff_done   — togal_takeoff.json or takeoff.csv exists
  estimate_done  — estimate_output.json or estimate_input.json exists
  proposal_ready — proposal.md or proposal.json exists
  submitted      — explicit override (via 'update <slug> submitted') or BC shows "Submitted"
  won / lost     — explicit final outcomes
  no_bid         — explicit walk-away
  declined       — explicit decline of invite
  past_due       — due_date passed without submission

Usage:
  python scripts/bid_status.py list                       # all bids with current status
  python scripts/bid_status.py list --due-this-week
  python scripts/bid_status.py list --status submitted
  python scripts/bid_status.py summary                    # status counts + breakdown
  python scripts/bid_status.py show <project-name>        # detail one bid
  python scripts/bid_status.py update <slug> <status>     # manual override
  python scripts/bid_status.py update <slug> submitted --amount 87500
  python scripts/bid_status.py refresh                    # re-derive all statuses from disk
  python scripts/bid_status.py stale                      # bids stuck at a stage too long
"""

import argparse
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = Path(__file__).resolve().parent.parent
BIDS_FILE   = BASE / "data" / "memory" / "active_bids.json"
STATUS_FILE = BASE / "data" / "memory" / "bid_status.json"
PROJECTS    = BASE / "data" / "projects"

# Lifecycle order — index = "progress score" for sorting
LIFECYCLE = [
    "invited", "accepted", "docs_pulled", "sow_done", "takeoff_done",
    "estimate_done", "proposal_ready", "submitted", "won", "lost",
    "no_bid", "declined", "past_due",
]
STAGE_RANK = {s: i for i, s in enumerate(LIFECYCLE)}

# Visual indicators
STAGE_EMOJI = {
    "invited":         "📨",
    "accepted":        "✅",
    "docs_pulled":     "📂",
    "sow_done":        "📋",
    "takeoff_done":    "📐",
    "estimate_done":   "💵",
    "proposal_ready":  "📤",
    "submitted":       "🚀",
    "won":             "🏆",
    "lost":            "❌",
    "no_bid":          "🚫",
    "declined":        "👎",
    "past_due":        "⌛",
}


def slugify(name):
    """Match scout_agent's slugify. Collapses repeat dashes from special chars."""
    if not name:
        return ""
    s = re.sub(r"[^a-z0-9\s-]", "", name.lower())
    s = re.sub(r"\s+", "-", s).strip("-")
    s = re.sub(r"-+", "-", s)
    return s[:80]


def parse_date(s):
    if not s:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            pass
    return None


def find_project_dir(slug):
    """Locate a project dir even if slug uses underscores vs dashes."""
    for variant in [slug, slug.replace("-", "_"), slug.replace("_", "-")]:
        p = PROJECTS / variant
        if p.exists():
            return p
    # Fuzzy: clean slug → match clean dir name
    clean = re.sub(r"[^a-z0-9]", "", slug.lower())
    if PROJECTS.exists() and clean:
        for d in PROJECTS.iterdir():
            if d.is_dir() and re.sub(r"[^a-z0-9]", "", d.name.lower()) == clean:
                return d
    return None


def derive_status(bid, override=None):
    """Derive current lifecycle status from filesystem + bid metadata.

    Returns: (status_string, evidence_list)
    """
    evidence = []
    today = date.today()

    # Explicit override always wins (until cleared)
    if override and override.get("status"):
        return override["status"], [f"override: {override.get('reason', '(no reason given)')}"]

    # Past-due check (final outcome takes priority over past_due)
    due = parse_date(bid.get("due_date", ""))

    # Filesystem-derived status
    slug = slugify(bid.get("project_name", ""))
    pdir = find_project_dir(slug) if slug else None

    has_real_docs = False
    if pdir:
        bid_docs = pdir / "bid_docs"
        if bid_docs.exists():
            real_pdfs = [f for f in bid_docs.rglob("*.pdf")
                         if not f.name.startswith("_")]
            if real_pdfs:
                has_real_docs = True
                evidence.append(f"docs: {len(real_pdfs)} PDFs")

        # Check artifacts
        markers = {
            "sow.json":          "sow_done",
            "SOW.md":            "sow_done",
            "togal_takeoff.json":"takeoff_done",
            "takeoff.csv":       "takeoff_done",
            "estimate_output.json":"estimate_done",
            "estimate_input.json": "estimate_done",
            "proposal.md":       "proposal_ready",
            "proposal.json":     "proposal_ready",
        }
        max_status = "invited"
        for fname, st in markers.items():
            p = pdir / fname
            if not p.exists():
                # also check for SOW_*.md pattern
                if fname == "SOW.md":
                    found = list(pdir.glob("SOW*.md"))
                    if found:
                        p = found[0]
                    else:
                        continue
                else:
                    continue
            if p.exists():
                evidence.append(f"{fname}")
                if STAGE_RANK[st] > STAGE_RANK[max_status]:
                    max_status = st

        if max_status != "invited":
            # If we have advanced artifacts, use them
            if due and due < today and STAGE_RANK[max_status] < STAGE_RANK["submitted"]:
                # Past due without submission
                return "past_due", evidence + [f"due {bid.get('due_date')}, not submitted"]
            return max_status, evidence

        if has_real_docs:
            if due and due < today:
                return "past_due", evidence + [f"due {bid.get('due_date')}, no SOW"]
            return "docs_pulled", evidence

    # No project dir / no real docs — pre-fetch state
    if due and due < today:
        return "past_due", [f"due {bid.get('due_date')}, no docs"]

    # Use BC portal status if available
    bc_status = (bid.get("bc_status") or "").lower()
    if bc_status == "accepted":
        return "accepted", ["BC: Accepted"]
    if bc_status == "submitted":
        return "submitted", ["BC: Submitted"]
    if bc_status == "declined":
        return "declined", ["BC: Declined"]

    return "invited", ["no docs yet"]


def load_overrides():
    if STATUS_FILE.exists():
        try:
            return json.load(open(STATUS_FILE, encoding="utf-8"))
        except Exception:
            return {"overrides": {}, "history": []}
    return {"overrides": {}, "history": []}


def save_overrides(data):
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATUS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def all_bids_with_status():
    bids = json.load(open(BIDS_FILE, encoding="utf-8"))
    overrides_data = load_overrides()
    overrides = overrides_data.get("overrides", {})
    rows = []
    for b in bids:
        slug = slugify(b.get("project_name", ""))
        ovr = overrides.get(slug)
        status, evidence = derive_status(b, ovr)
        rows.append({
            "slug": slug,
            "name": b.get("project_name", "?"),
            "gc": b.get("gc", "?"),
            "source": b.get("source", "?"),
            "due_date": b.get("due_date", ""),
            "due": parse_date(b.get("due_date", "")),
            "status": status,
            "evidence": evidence,
            "distance": b.get("distance_miles"),
            "amount": ovr.get("amount") if ovr else None,
        })
    return rows


# ---------------- CLI commands ----------------

def cmd_list(args):
    rows = all_bids_with_status()
    today = date.today()

    if args.status:
        rows = [r for r in rows if r["status"] == args.status]
    if args.due_this_week:
        days_since_sun = (today.weekday() + 1) % 7
        week_start = today - timedelta(days=days_since_sun)
        week_end = week_start + timedelta(days=6)
        rows = [r for r in rows if r["due"] and week_start <= r["due"] <= week_end]
    if args.due_today:
        rows = [r for r in rows if r["due"] == today]

    # Sort: by status rank, then by due date
    rows.sort(key=lambda r: (STAGE_RANK.get(r["status"], 99), r["due"] or date.max))

    print(f"{'STATUS':<16} {'DUE':<10} {'NAME':<45} {'GC':<22}")
    print("-" * 100)
    for r in rows[:args.limit]:
        emoji = STAGE_EMOJI.get(r["status"], " ")
        print(f"{emoji} {r['status']:<13} {r['due_date']:<10} {r['name'][:43]:<45} {r['gc'][:20]}")
    if len(rows) > args.limit:
        print(f"... + {len(rows) - args.limit} more")
    print(f"\nTotal: {len(rows)} bids")


def cmd_summary(args):
    rows = all_bids_with_status()
    today = date.today()
    days_since_sun = (today.weekday() + 1) % 7
    week_start = today - timedelta(days=days_since_sun)
    week_end = week_start + timedelta(days=6)

    print(f"=== ALL ACTIVE BIDS (n={len(rows)}) ===\n")
    by_status = {}
    for r in rows:
        by_status.setdefault(r["status"], []).append(r)
    for status in LIFECYCLE:
        if status in by_status:
            count = len(by_status[status])
            print(f"  {STAGE_EMOJI.get(status,' ')} {status:<16} {count:>3}")
    print()

    # This-week breakdown
    this_week = [r for r in rows if r["due"] and week_start <= r["due"] <= week_end]
    if this_week:
        print(f"=== DUE THIS WEEK ({len(this_week)}) ===")
        wk_status = {}
        for r in this_week:
            wk_status.setdefault(r["status"], 0)
            wk_status[r["status"]] += 1
        for status in LIFECYCLE:
            if status in wk_status:
                print(f"  {STAGE_EMOJI.get(status,' ')} {status:<16} {wk_status[status]}")
        print()

    # Items needing action (invited but due within 3 days)
    urgent_invited = [r for r in rows
                      if r["status"] == "invited" and r["due"]
                      and 0 <= (r["due"] - today).days <= 3]
    if urgent_invited:
        print(f"=== URGENT — invited, due ≤3 days, no docs yet ({len(urgent_invited)}) ===")
        for r in urgent_invited:
            print(f"  {r['due_date']} {r['name'][:50]} ({r['gc'][:20]})")
        print()


def cmd_show(args):
    rows = all_bids_with_status()
    needle = args.name.lower()
    matches = [r for r in rows if needle in r["name"].lower() or needle in r["slug"]]
    if not matches:
        print(f"No bid matching '{args.name}'")
        return
    for r in matches[:5]:
        print(f"\n{STAGE_EMOJI.get(r['status'],' ')} {r['status'].upper()}: {r['name']}")
        print(f"  slug:     {r['slug']}")
        print(f"  GC:       {r['gc']}")
        print(f"  source:   {r['source']}")
        print(f"  due:      {r['due_date']}  ({(r['due'] - date.today()).days if r['due'] else '?'} days)")
        print(f"  distance: {r['distance']} mi")
        print(f"  evidence: {', '.join(r['evidence'])}")
        if r["amount"]:
            print(f"  amount:   ${r['amount']:,}")


def cmd_update(args):
    if args.status not in LIFECYCLE:
        print(f"Bad status. Choose from: {', '.join(LIFECYCLE)}")
        return
    data = load_overrides()
    overrides = data.setdefault("overrides", {})
    history = data.setdefault("history", [])

    # Resolve slug if user gave a project name
    slug = args.slug
    if not (PROJECTS / slug).exists():
        rows = all_bids_with_status()
        needle = args.slug.lower()
        matches = [r for r in rows if needle in r["name"].lower() or needle in r["slug"]]
        if matches:
            slug = matches[0]["slug"]
            print(f"Resolved '{args.slug}' → slug '{slug}'")

    prev = overrides.get(slug, {}).get("status", "(no override)")
    overrides[slug] = {
        "status": args.status,
        "amount": args.amount,
        "reason": args.reason or "manual update",
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    history.append({
        "slug": slug,
        "from": prev,
        "to": args.status,
        "amount": args.amount,
        "reason": args.reason,
        "at": datetime.now().isoformat(timespec="seconds"),
    })
    save_overrides(data)
    print(f"✓ {slug}: {prev} → {args.status}" + (f" (${args.amount:,})" if args.amount else ""))


def cmd_refresh(args):
    rows = all_bids_with_status()
    print(f"Refreshed status for {len(rows)} active bids.")
    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    for s in LIFECYCLE:
        if s in counts:
            print(f"  {STAGE_EMOJI.get(s,' ')} {s}: {counts[s]}")


def cmd_stale(args):
    """Find bids stuck at a stage longer than expected."""
    rows = all_bids_with_status()
    today = date.today()
    stale = []
    for r in rows:
        if not r["due"]:
            continue
        days_left = (r["due"] - today).days
        # Invited but bid is <2 days away
        if r["status"] == "invited" and 0 <= days_left <= 2:
            stale.append((r, "invited but due in ≤2 days"))
        # Docs pulled but no progress and bid is <3 days
        elif r["status"] == "docs_pulled" and 0 <= days_left <= 3:
            stale.append((r, "docs pulled but no SOW yet"))
        elif r["status"] == "estimate_done" and 0 <= days_left <= 1:
            stale.append((r, "estimate done but no proposal"))

    if not stale:
        print("No stale bids — pipeline is clean.")
        return
    print(f"=== STALE BIDS NEEDING ATTENTION ({len(stale)}) ===")
    for r, reason in stale:
        print(f"  {STAGE_EMOJI.get(r['status'],' ')} {r['status']:<14} due {r['due_date']}  {r['name'][:50]} — {reason}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")

    p_list = sub.add_parser("list")
    p_list.add_argument("--status", help="filter by status")
    p_list.add_argument("--due-this-week", action="store_true")
    p_list.add_argument("--due-today", action="store_true")
    p_list.add_argument("--limit", type=int, default=50)

    sub.add_parser("summary")
    sub.add_parser("refresh")
    sub.add_parser("stale")

    p_show = sub.add_parser("show")
    p_show.add_argument("name")

    p_upd = sub.add_parser("update")
    p_upd.add_argument("slug")
    p_upd.add_argument("status")
    p_upd.add_argument("--amount", type=int)
    p_upd.add_argument("--reason", default="")

    args = ap.parse_args()
    cmd = args.cmd or "summary"
    {"list": cmd_list, "summary": cmd_summary, "show": cmd_show,
     "update": cmd_update, "refresh": cmd_refresh, "stale": cmd_stale}[cmd](args)


if __name__ == "__main__":
    main()
