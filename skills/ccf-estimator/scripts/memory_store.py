#!/usr/bin/env python3
"""
CCF Memory & Learning System
File-based knowledge base that grows with every project.
Tracks: GC relationships, facility type patterns, bid history, user feedback.

Usage:
  python memory_store.py gc get --name "WED Construction"
  python memory_store.py gc update --name "WED Construction" --field win_rate --value 0.375
  python memory_store.py facility get --type retail_food_lion
  python memory_store.py history add --project "Food Lion 1513" --gc "WED" --bid 20213
  python memory_store.py history update --project "Food Lion 1513" --result won
  python memory_store.py feedback add --action "Adjusted markup" --reason "User said too high for new GC"
  python memory_store.py brief --due-date today
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, date
from pathlib import Path

MEMORY_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data" / "memory"
GC_DIR = MEMORY_DIR / "gc"
FACILITY_DIR = MEMORY_DIR / "facility_types"
HISTORY_FILE = MEMORY_DIR / "bid_history.json"
FEEDBACK_FILE = MEMORY_DIR / "feedback.json"


def slugify(name):
    s = name.lower().strip()
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"[\s]+", "-", s)
    return s.strip("-")


# --- GC Knowledge ---

def gc_get(name):
    path = GC_DIR / f"{slugify(name)}.json"
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return None


def gc_create(name, **fields):
    gc = {
        "name": name,
        "slug": slugify(name),
        "contacts": [],
        "relationship": "new",
        "projects_bid": 0,
        "projects_won": 0,
        "win_rate": 0.0,
        "avg_bid_amount": 0,
        "pricing_notes": "",
        "preferred_markup": "existing-competitive",
        "last_feedback": "",
        "communication_style": "",
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
    }
    gc.update({k: v for k, v in fields.items() if v is not None})

    GC_DIR.mkdir(parents=True, exist_ok=True)
    path = GC_DIR / f"{gc['slug']}.json"
    with open(path, "w") as f:
        json.dump(gc, f, indent=2)
    return gc


def gc_update(name, **fields):
    gc = gc_get(name)
    if gc is None:
        gc = gc_create(name)

    for k, v in fields.items():
        if v is not None:
            if k == "contacts" and isinstance(v, str):
                v = json.loads(v)
            gc[k] = v

    # Auto-calculate win rate
    if gc["projects_bid"] > 0:
        gc["win_rate"] = round(gc["projects_won"] / gc["projects_bid"], 3)

    gc["updated_at"] = datetime.now().isoformat()

    path = GC_DIR / f"{gc['slug']}.json"
    with open(path, "w") as f:
        json.dump(gc, f, indent=2)
    return gc


def gc_list():
    if not GC_DIR.exists():
        return []
    gcs = []
    for f in sorted(GC_DIR.glob("*.json")):
        with open(f) as fh:
            gcs.append(json.load(fh))
    return gcs


def gc_record_bid(name, project_name, bid_amount, result=None):
    """Record a bid with a GC and update stats."""
    gc = gc_get(name)
    if gc is None:
        gc = gc_create(name)

    gc["projects_bid"] = gc.get("projects_bid", 0) + 1
    if result == "won":
        gc["projects_won"] = gc.get("projects_won", 0) + 1

    # Update average bid amount
    prev_total = gc.get("avg_bid_amount", 0) * (gc["projects_bid"] - 1)
    gc["avg_bid_amount"] = round((prev_total + bid_amount) / gc["projects_bid"], 2)

    return gc_update(name, **gc)


# --- Facility Type Patterns ---

def facility_get(facility_type):
    path = FACILITY_DIR / f"{slugify(facility_type)}.json"
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return None


def facility_create(facility_type, **fields):
    facility = {
        "type": facility_type,
        "slug": slugify(facility_type),
        "project_count": 0,
        "avg_project_size_sf": 0,
        "typical_scope": [],
        "common_exclusions": [],
        "typical_paint_systems": {},
        "typical_conditions": [],
        "avg_bid_price": 0,
        "avg_bid_price_per_sf": 0,
        "avg_labor_hours_per_sf": 0,
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
    }
    facility.update({k: v for k, v in fields.items() if v is not None})

    FACILITY_DIR.mkdir(parents=True, exist_ok=True)
    path = FACILITY_DIR / f"{facility['slug']}.json"
    with open(path, "w") as f:
        json.dump(facility, f, indent=2)
    return facility


def facility_update(facility_type, **fields):
    facility = facility_get(facility_type)
    if facility is None:
        facility = facility_create(facility_type)

    for k, v in fields.items():
        if v is not None:
            facility[k] = v
    facility["updated_at"] = datetime.now().isoformat()

    path = FACILITY_DIR / f"{facility['slug']}.json"
    with open(path, "w") as f:
        json.dump(facility, f, indent=2)
    return facility


def facility_learn_from_project(facility_type, project_sf, bid_price, labor_hours, scope_items=None, exclusions=None, paint_systems=None, conditions=None):
    """Update facility patterns from a completed project."""
    facility = facility_get(facility_type)
    if facility is None:
        facility = facility_create(facility_type)

    n = facility.get("project_count", 0)

    # Running averages
    facility["project_count"] = n + 1
    facility["avg_project_size_sf"] = round(((facility.get("avg_project_size_sf", 0) * n) + project_sf) / (n + 1))
    facility["avg_bid_price"] = round(((facility.get("avg_bid_price", 0) * n) + bid_price) / (n + 1), 2)

    if project_sf > 0:
        new_price_per_sf = bid_price / project_sf
        new_hours_per_sf = labor_hours / project_sf
        facility["avg_bid_price_per_sf"] = round(((facility.get("avg_bid_price_per_sf", 0) * n) + new_price_per_sf) / (n + 1), 4)
        facility["avg_labor_hours_per_sf"] = round(((facility.get("avg_labor_hours_per_sf", 0) * n) + new_hours_per_sf) / (n + 1), 6)

    # Merge scope items, exclusions, systems, conditions (accumulate unique)
    if scope_items:
        existing = set(facility.get("typical_scope", []))
        existing.update(scope_items)
        facility["typical_scope"] = sorted(existing)

    if exclusions:
        existing = set(facility.get("common_exclusions", []))
        existing.update(exclusions)
        facility["common_exclusions"] = sorted(existing)

    if paint_systems:
        facility.setdefault("typical_paint_systems", {}).update(paint_systems)

    if conditions:
        existing = set(facility.get("typical_conditions", []))
        existing.update(conditions)
        facility["typical_conditions"] = sorted(existing)

    return facility_update(facility_type, **facility)


def facility_list():
    if not FACILITY_DIR.exists():
        return []
    facilities = []
    for f in sorted(FACILITY_DIR.glob("*.json")):
        with open(f) as fh:
            facilities.append(json.load(fh))
    return facilities


# --- Bid History ---

def _load_history():
    if HISTORY_FILE.exists():
        with open(HISTORY_FILE) as f:
            return json.load(f)
    return []


def _save_history(history):
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)


def history_add(project, gc, bid_amount, bid_date=None, facility_type=None, source=None):
    history = _load_history()
    entry = {
        "project": project,
        "gc": gc,
        "bid_date": bid_date or datetime.now().strftime("%Y-%m-%d"),
        "our_bid": bid_amount,
        "facility_type": facility_type,
        "source": source,
        "result": "pending",
        "competitor_info": None,
        "gc_feedback": None,
        "lessons": None,
        "created_at": datetime.now().isoformat(),
    }
    history.append(entry)
    _save_history(history)
    return entry


def history_update(project, **fields):
    history = _load_history()
    for entry in history:
        if entry["project"] == project:
            for k, v in fields.items():
                if v is not None:
                    entry[k] = v
            entry["updated_at"] = datetime.now().isoformat()
            _save_history(history)
            return entry
    return None


def history_get_by_gc(gc_name):
    return [e for e in _load_history() if gc_name.lower() in e.get("gc", "").lower()]


def history_get_by_type(facility_type):
    return [e for e in _load_history() if e.get("facility_type") == facility_type]


def history_stats():
    history = _load_history()
    total = len(history)
    won = sum(1 for e in history if e.get("result") == "won")
    lost = sum(1 for e in history if e.get("result") == "lost")
    pending = sum(1 for e in history if e.get("result") == "pending")
    total_bid_value = sum(e.get("our_bid", 0) for e in history)
    won_value = sum(e.get("our_bid", 0) for e in history if e.get("result") == "won")

    return {
        "total_bids": total,
        "won": won,
        "lost": lost,
        "pending": pending,
        "win_rate": round(won / max(total - pending, 1), 3),
        "total_bid_value": total_bid_value,
        "won_value": won_value,
    }


# --- User Feedback ---

def _load_feedback():
    if FEEDBACK_FILE.exists():
        with open(FEEDBACK_FILE) as f:
            return json.load(f)
    return []


def _save_feedback(feedback):
    FEEDBACK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(FEEDBACK_FILE, "w") as f:
        json.dump(feedback, f, indent=2)


def feedback_add(project, phase, carol_action, user_correction, reason=None):
    """Log a user correction/feedback for learning."""
    feedback = _load_feedback()
    entry = {
        "project": project,
        "phase": phase,
        "carol_action": carol_action,
        "user_correction": user_correction,
        "reason": reason,
        "timestamp": datetime.now().isoformat(),
    }
    feedback.append(entry)
    _save_feedback(feedback)
    return entry


def feedback_get_by_phase(phase):
    return [e for e in _load_feedback() if e.get("phase") == phase]


def feedback_get_recent(n=10):
    return _load_feedback()[-n:]


# --- CLI ---

def main():
    parser = argparse.ArgumentParser(description="CCF Memory Store")
    sub = parser.add_subparsers(dest="command")

    # GC commands
    gc_p = sub.add_parser("gc")
    gc_sub = gc_p.add_subparsers(dest="gc_action")

    gc_get_p = gc_sub.add_parser("get")
    gc_get_p.add_argument("--name", required=True)

    gc_update_p = gc_sub.add_parser("update")
    gc_update_p.add_argument("--name", required=True)
    gc_update_p.add_argument("--relationship", default=None)
    gc_update_p.add_argument("--pricing-notes", default=None)
    gc_update_p.add_argument("--preferred-markup", default=None)
    gc_update_p.add_argument("--last-feedback", default=None)

    gc_sub.add_parser("list")

    gc_bid_p = gc_sub.add_parser("record-bid")
    gc_bid_p.add_argument("--name", required=True)
    gc_bid_p.add_argument("--project", required=True)
    gc_bid_p.add_argument("--bid", type=float, required=True)
    gc_bid_p.add_argument("--result", default=None)

    # Facility commands
    fac_p = sub.add_parser("facility")
    fac_sub = fac_p.add_subparsers(dest="fac_action")

    fac_get_p = fac_sub.add_parser("get")
    fac_get_p.add_argument("--type", required=True)

    fac_sub.add_parser("list")

    fac_learn_p = fac_sub.add_parser("learn")
    fac_learn_p.add_argument("--type", required=True)
    fac_learn_p.add_argument("--sf", type=float, required=True)
    fac_learn_p.add_argument("--bid", type=float, required=True)
    fac_learn_p.add_argument("--hours", type=float, required=True)

    # History commands
    hist_p = sub.add_parser("history")
    hist_sub = hist_p.add_subparsers(dest="hist_action")

    hist_add_p = hist_sub.add_parser("add")
    hist_add_p.add_argument("--project", required=True)
    hist_add_p.add_argument("--gc", required=True)
    hist_add_p.add_argument("--bid", type=float, required=True)
    hist_add_p.add_argument("--date", default=None)
    hist_add_p.add_argument("--type", default=None)

    hist_upd_p = hist_sub.add_parser("update")
    hist_upd_p.add_argument("--project", required=True)
    hist_upd_p.add_argument("--result", default=None)
    hist_upd_p.add_argument("--feedback", default=None)
    hist_upd_p.add_argument("--lessons", default=None)

    hist_sub.add_parser("stats")

    # Feedback commands
    fb_p = sub.add_parser("feedback")
    fb_sub = fb_p.add_subparsers(dest="fb_action")

    fb_add_p = fb_sub.add_parser("add")
    fb_add_p.add_argument("--project", required=True)
    fb_add_p.add_argument("--phase", required=True)
    fb_add_p.add_argument("--action", required=True)
    fb_add_p.add_argument("--correction", required=True)
    fb_add_p.add_argument("--reason", default=None)

    fb_sub.add_parser("recent")

    args = parser.parse_args()

    result = None

    if args.command == "gc":
        if args.gc_action == "get":
            result = gc_get(args.name) or {"error": "GC not found"}
        elif args.gc_action == "update":
            result = gc_update(args.name, relationship=args.relationship,
                             pricing_notes=args.pricing_notes,
                             preferred_markup=args.preferred_markup,
                             last_feedback=args.last_feedback)
        elif args.gc_action == "list":
            result = gc_list()
        elif args.gc_action == "record-bid":
            result = gc_record_bid(args.name, args.project, args.bid, args.result)

    elif args.command == "facility":
        if args.fac_action == "get":
            result = facility_get(args.type) or {"error": "Facility type not found"}
        elif args.fac_action == "list":
            result = facility_list()
        elif args.fac_action == "learn":
            result = facility_learn_from_project(args.type, args.sf, args.bid, args.hours)

    elif args.command == "history":
        if args.hist_action == "add":
            result = history_add(args.project, args.gc, args.bid, args.date, args.type)
        elif args.hist_action == "update":
            result = history_update(args.project, result=args.result, gc_feedback=args.feedback, lessons=args.lessons)
        elif args.hist_action == "stats":
            result = history_stats()

    elif args.command == "feedback":
        if args.fb_action == "add":
            result = feedback_add(args.project, args.phase, args.action, args.correction, args.reason)
        elif args.fb_action == "recent":
            result = feedback_get_recent()

    if result is not None:
        print(json.dumps(result, indent=2, default=str))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
