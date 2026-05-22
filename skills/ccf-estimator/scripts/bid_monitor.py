#!/usr/bin/env python3
"""
CCF Unified Bid Monitor
Checks all sources (email, BuildingConnected, ConstructConnect) for bid opportunities.
Returns a unified, categorized bid list.

Usage:
  python bid_monitor.py --check-all
  python bid_monitor.py --check email
  python bid_monitor.py --check buildingconnected
  python bid_monitor.py --check constructconnect
  python bid_monitor.py --daily-brief
  python bid_monitor.py --due-today
  python bid_monitor.py --due-this-week
"""

import argparse
import json
import sys
from datetime import datetime, date, timedelta
from pathlib import Path

# Import sibling modules
sys.path.insert(0, str(Path(__file__).parent))
from facility_classifier import classify, classify_batch
from buildingconnected_client import list_opportunities as bc_list, check_status as bc_status
from constructconnect_client import api_search_projects as cc_search, check_status as cc_status
from email_scanner import get_search_queries, classify_facility
from trade_filter import filter_bids, is_our_trade
from distance_calc import add_distance_to_bids

BIDS_CACHE_FILE = Path(__file__).resolve().parent.parent.parent.parent / "data" / "memory" / "active_bids.json"


def _load_bids_cache():
    if BIDS_CACHE_FILE.exists():
        with open(BIDS_CACHE_FILE) as f:
            return json.load(f)
    return []


def _save_bids_cache(bids):
    BIDS_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(BIDS_CACHE_FILE, "w") as f:
        json.dump(bids, f, indent=2, default=str)


def check_buildingconnected():
    """Check BuildingConnected for open opportunities."""
    status = bc_status()
    if not status.get("configured"):
        return {"source": "buildingconnected", "status": "not_configured", "bids": []}

    result = bc_list(status="open")
    if "error" in result:
        return {"source": "buildingconnected", "status": "error", "error": result["error"], "bids": []}

    bids = []
    for opp in result.get("opportunities", []):
        # Classify facility type
        fc = classify(opp.get("project_name", ""))
        opp["facility_type"] = fc["type"]
        opp["facility_label"] = fc["label"]
        bids.append(opp)

    return {"source": "buildingconnected", "status": "ok", "bids": bids, "count": len(bids)}


def check_constructconnect():
    """Check ConstructConnect for bidding projects."""
    status = cc_status()
    if not status.get("configured"):
        return {"source": "constructconnect", "status": "not_configured", "bids": []}

    # Search for painting projects in NC and SC
    bids = []
    for state in ["NC", "SC"]:
        result = cc_search(trade="painting", state=state, status="bidding")
        if "error" not in result:
            for proj in result.get("projects", []):
                fc = classify(proj.get("project_name", ""))
                proj["facility_type"] = fc["type"]
                proj["facility_label"] = fc["label"]
                bids.append(proj)

    return {"source": "constructconnect", "status": "ok", "bids": bids, "count": len(bids)}


def check_email():
    """Return search queries for the agent to run via Gmail MCP.
    The agent will execute these queries and pass results back."""
    queries = get_search_queries()
    return {
        "source": "email",
        "status": "queries_ready",
        "search_queries": queries,
        "instructions": "Run each query with gmail_search_messages, then pass results to email_scanner.py --action parse-email for each email found.",
        "bids": [],  # Agent fills this after running queries
    }


def check_all():
    """Check all sources."""
    results = {
        "timestamp": datetime.now().isoformat(),
        "sources": {},
        "all_bids": [],
    }

    # BuildingConnected
    bc = check_buildingconnected()
    results["sources"]["buildingconnected"] = {"status": bc["status"], "count": bc.get("count", 0)}
    results["all_bids"].extend(bc.get("bids", []))

    # ConstructConnect
    cc = check_constructconnect()
    results["sources"]["constructconnect"] = {"status": cc["status"], "count": cc.get("count", 0)}
    results["all_bids"].extend(cc.get("bids", []))

    # Email (returns queries for agent to execute)
    email = check_email()
    results["sources"]["email"] = {"status": email["status"], "search_queries": email["search_queries"]}

    # Filter to only Painting & Wallcovering trades
    results["all_bids"], filtered_count = filter_bids(results["all_bids"])
    results["filtered_out"] = filtered_count

    # Deduplicate by project name similarity
    results["all_bids"] = _deduplicate(results["all_bids"])

    # Add distance from Monroe, NC and sort closest first
    results["all_bids"] = add_distance_to_bids(results["all_bids"])

    # Categorize
    results["summary"] = _summarize(results["all_bids"])

    # Cache
    _save_bids_cache(results["all_bids"])

    return results


def _deduplicate(bids):
    """Deduplicate bids across sources, merging GCs for the same project.
    Same project from multiple GCs or sources → one row with all GCs listed."""
    import re

    def _normalize(name):
        """Normalize project name for matching."""
        n = name.lower().strip()
        # Remove common suffixes
        for suffix in [" - all trades", " - main trades", " - all", " - main"]:
            n = n.replace(suffix, "")
        # Remove punctuation and extra spaces
        n = re.sub(r'[^a-z0-9\s]', '', n)
        n = re.sub(r'\s+', ' ', n).strip()
        return n

    groups = {}
    for bid in bids:
        key = _normalize(bid.get("project_name", ""))
        if not key:
            continue

        # Try to match against existing keys (fuzzy: first 25 chars)
        matched_key = None
        for existing_key in groups:
            if key[:25] == existing_key[:25] or existing_key[:25] in key or key[:25] in existing_key:
                matched_key = existing_key
                break

        if matched_key:
            groups[matched_key].append(bid)
        else:
            groups[key] = [bid]

    # Merge each group into one unified bid
    unique = []
    for key, group in groups.items():
        # Use the bid with the most info as the base
        base = max(group, key=lambda b: len(str(b.get("size_sf", ""))) + len(str(b.get("gc", ""))))

        # Collect all GCs and contacts
        gcs = []
        contacts = []
        sources = set()
        for b in group:
            gc = b.get("gc", "").strip()
            contact = b.get("gc_contact", "").strip()
            source = b.get("source", "")
            if gc and gc not in gcs:
                gcs.append(gc)
            if contact and contact not in contacts:
                contacts.append(contact)
            if source:
                sources.add(source)

        base["gc"] = " + ".join(gcs) if gcs else base.get("gc", "")
        base["gc_contact"] = ", ".join(contacts) if contacts else base.get("gc_contact", "")
        base["sources"] = sorted(sources)
        base["gc_count"] = len(gcs)

        # Use best size_sf available
        for b in group:
            sf = b.get("size_sf", "")
            if sf and not base.get("size_sf"):
                base["size_sf"] = sf

        unique.append(base)

    # Sort by due date
    def _sort_key(bid):
        d = bid.get("due_date", "")
        try:
            return datetime.strptime(d, "%m/%d/%Y") if "/" in d else datetime.strptime(d, "%b %d, %Y") if "," in d else datetime.max
        except (ValueError, TypeError):
            return datetime.max

    unique.sort(key=_sort_key)

    return unique


def _summarize(bids):
    """Generate summary categorization."""
    categories = {}
    for bid in bids:
        label = bid.get("facility_label", bid.get("facility_type", "Other"))
        if label not in categories:
            categories[label] = []
        categories[label].append(bid.get("project_name", "Unknown"))

    parts = []
    for label, projects in sorted(categories.items(), key=lambda x: -len(x[1])):
        parts.append(f"{len(projects)} {label}")

    return {
        "total_bids": len(bids),
        "categories": {k: len(v) for k, v in categories.items()},
        "text": ", ".join(parts) if parts else "No bids found",
        "detail": categories,
    }


def daily_brief():
    """Generate daily briefing of bids."""
    result = check_all()
    bids = result.get("all_bids", [])

    today = date.today().isoformat()
    this_week_end = (date.today() + timedelta(days=7)).isoformat()

    due_today = [b for b in bids if today in str(b.get("bid_due", ""))]
    due_this_week = [b for b in bids if _is_within_week(b.get("bid_due", ""))]
    upcoming = [b for b in bids if b not in due_today and b not in due_this_week]

    brief = {
        "date": today,
        "greeting": f"Good morning! Here's your bid pipeline for {datetime.now().strftime('%A, %B %d')}:",
        "due_today": {
            "count": len(due_today),
            "bids": due_today,
        },
        "due_this_week": {
            "count": len(due_this_week),
            "bids": due_this_week,
        },
        "upcoming": {
            "count": len(upcoming),
            "bids": upcoming,
        },
        "summary": result.get("summary", {}),
        "sources_status": result.get("sources", {}),
    }

    return brief


def _is_within_week(date_str):
    """Check if a date string is within the next 7 days."""
    if not date_str:
        return False
    try:
        today = date.today()
        week_end = today + timedelta(days=7)
        # Try multiple date formats
        for fmt in ["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%m/%d/%Y", "%B %d, %Y"]:
            try:
                d = datetime.strptime(str(date_str)[:10], fmt[:10 if "T" not in fmt else None]).date()
                return today <= d <= week_end
            except (ValueError, TypeError):
                continue
    except Exception:
        pass
    return False


def get_due_today():
    """Get bids due today only."""
    bids = _load_bids_cache()
    today = date.today().isoformat()
    return [b for b in bids if today in str(b.get("bid_due", ""))]


def main():
    parser = argparse.ArgumentParser(description="CCF Bid Monitor")
    parser.add_argument("--check-all", action="store_true")
    parser.add_argument("--check", choices=["email", "buildingconnected", "constructconnect"])
    parser.add_argument("--daily-brief", action="store_true")
    parser.add_argument("--due-today", action="store_true")
    parser.add_argument("--due-this-week", action="store_true")
    args = parser.parse_args()

    if args.check_all:
        result = check_all()
    elif args.check == "email":
        result = check_email()
    elif args.check == "buildingconnected":
        result = check_buildingconnected()
    elif args.check == "constructconnect":
        result = check_constructconnect()
    elif args.daily_brief:
        result = daily_brief()
    elif args.due_today:
        result = get_due_today()
    elif args.due_this_week:
        bids = _load_bids_cache()
        result = [b for b in bids if _is_within_week(b.get("bid_due", ""))]
    else:
        parser.print_help()
        sys.exit(1)

    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
