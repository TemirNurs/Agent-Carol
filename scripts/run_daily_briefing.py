#!/usr/bin/env python3
"""Full daily briefing with Top 3 deep dive and Carol's recommendations."""
import sys, json
sys.stdout.reconfigure(encoding='utf-8')
from pathlib import Path
from datetime import datetime

# Load data
bids = json.load(open(r"C:\Users\Nursm\Agent Carol\data\memory\active_bids.json"))
gc_dir = Path(r"C:\Users\Nursm\Agent Carol\data\memory\gc")
ft_dir = Path(r"C:\Users\Nursm\Agent Carol\data\memory\facility_types")

gcs = {}
for f in gc_dir.glob("*.json"):
    data = json.load(open(f))
    gcs[data["name"].lower()] = data

fts = {}
for f in ft_dir.glob("*.json"):
    data = json.load(open(f))
    fts[data["type"]] = data

# Categorize - dynamic dates based on today
today = datetime.now()
today_str = f"{today.month}/{today.day}/{today.year}"
today_bids = [b for b in bids if b.get("due_date", "") == today_str]
today_bids.sort(key=lambda b: b.get("distance_miles") or 999)

week_dates = []
for i in range(1, 7):
    d = today + __import__("datetime").timedelta(days=i)
    week_dates.append(f"{d.month}/{d.day}/{d.year}")
week_bids = [b for b in bids if b.get("due_date", "") in week_dates]
week_bids.sort(key=lambda b: b.get("distance_miles") or 999)

upcoming = [b for b in bids if b not in today_bids and b not in week_bids]
upcoming.sort(key=lambda b: b.get("distance_miles") or 999)

W = 90

def fmt_dist(b):
    d = b.get("distance_miles")
    if d and isinstance(d, (int, float)) and d < 900:
        return f"{d:.0f} mi"
    return "? mi"

def fmt_loc(b):
    return f"{b.get('city','')}, {b.get('state','')[:2]}"

def lookup_gc(gc_name):
    for key, gc in gcs.items():
        if gc_name.lower().startswith(key[:10]) or key.startswith(gc_name.lower()[:10]):
            return gc
    return None

def classify_facility(name):
    nl = name.lower()
    if "food lion" in nl: return "grocery_food_lion"
    if "dental" in nl or "heartland" in nl: return "dental"
    if "vamc" in nl or "va " in nl or "ehrm" in nl: return "government_military"
    if "camp butner" in nl or "fort bragg" in nl or "sof " in nl or "f-35" in nl: return "government_military"
    if "savers" in nl: return "retail"
    if "government" in nl or "elevator" in nl: return "government_military"
    if "hotel" in nl or "suites" in nl or "marriott" in nl or "hyatt" in nl or "home 2" in nl: return "hotel"
    if "bojangles" in nl or "eggs up" in nl or "dutch bros" in nl or "fuzzy" in nl: return "restaurant"
    if "victoria" in nl or "cvs" in nl or "chase bank" in nl or "boot barn" in nl: return "retail"
    if "church" in nl or "fellowship" in nl: return "religious"
    if "school" in nl or "elementary" in nl: return "education"
    if "usps" in nl or "postal" in nl: return "government_military"
    if "hvac" in nl: return "unknown"
    return "commercial"

def recommend(b):
    d = b.get("distance_miles") or 999
    gc_name = b.get("gc", "")
    gc_data = lookup_gc(gc_name)
    ftype = classify_facility(b["project_name"])

    # Hotel warning
    if ftype == "hotel":
        return "SKIP", "AGGRESSIVE", "Hotel/hospitality - we lose at 2x market. Recalibrate before bidding."

    # Very close
    if d < 20:
        tier = "TARGET"
        if gc_data and gc_data.get("win_rate", 0) == 0 and gc_data["projects_bid"] > 2:
            tier = "AGGRESSIVE"
        return "BID", tier, f"Only {d:.0f} mi from home. Low overhead, good schedule fill."

    # Known GC with good win rate + reasonable distance
    if gc_data:
        wr = gc_data.get("win_rate", 0)
        if wr >= 0.3 and d < 60:
            return "BID", "TARGET", f"{gc_data['name']}: {gc_data['projects_won']}/{gc_data['projects_bid']} wins. Known relationship."
        if wr >= 0.3 and d < 120:
            return "CONSIDER", "TARGET", f"{gc_data['name']}: {gc_data['projects_won']}/{gc_data['projects_bid']} wins but {d:.0f} mi."
        if wr == 0 and gc_data["projects_bid"] > 2:
            return "CONSIDER", "AGGRESSIVE", f"{gc_data['name']}: 0 wins in {gc_data['projects_bid']} bids. Tighten pricing."
        if gc_data.get("loss_patterns","") and "gc los" in gc_data.get("loss_patterns","").lower():
            return "BID", "TARGET", f"{gc_data['name']}: losses were GC losing, not our price. Keep building."

    # Government/military
    if ftype == "government_military":
        if d < 150:
            return "CONSIDER", "STANDARD", "Government work - steady pipeline. Prevailing wage."
        return "SKIP", "STANDARD", f"Government but {d:.0f} mi is far."

    # Unknown GC, far
    if d > 100:
        return "SKIP", "STANDARD", f"Unknown GC, {d:.0f} mi. Low win probability."

    # Unknown GC, medium distance
    if d > 60:
        return "CONSIDER", "STANDARD", f"Unknown GC but {d:.0f} mi is manageable for right project."

    return "CONSIDER", "TARGET", "Moderate distance, worth reviewing scope."


# === PRINT BRIEFING ===
print("=" * W)
print("  DAILY BID BRIEFING - Carolina Commercial Finishes")
print(f"  {today.strftime('%A, %B %#d, %Y')}")
print("=" * W)

# DUE TODAY table
print(f"\n  DUE TODAY: {len(today_bids)} projects (sorted nearest first)")
print("  " + "-" * (W-4))
print(f"  {'#':>2s}  {'Dist':>6s}  {'Project':<42s}  {'Location':<18s}  {'GC':<20s}")
print("  " + "-" * (W-4))
for i, b in enumerate(today_bids, 1):
    print(f"  {i:>2d}  {fmt_dist(b):>6s}  {b['project_name'][:42]:<42s}  {fmt_loc(b)[:18]:<18s}  {b.get('gc','')[:20]}")

# DUE THIS WEEK
print(f"\n  DUE THIS WEEK: {len(week_bids)} projects")
print("  " + "-" * (W-4))
print(f"  {'#':>2s}  {'Dist':>6s}  {'Project':<42s}  {'Location':<18s}  {'Due':<12s}  {'GC':<16s}")
print("  " + "-" * (W-4))
for i, b in enumerate(week_bids, 1):
    print(f"  {i:>2d}  {fmt_dist(b):>6s}  {b['project_name'][:42]:<42s}  {fmt_loc(b)[:18]:<18s}  {b.get('due_date',''):<12s}  {b.get('gc','')[:16]}")

# UPCOMING
print(f"\n  UPCOMING: {len(upcoming)} projects")
print("  " + "-" * (W-4))
print(f"  {'#':>2s}  {'Dist':>6s}  {'Project':<42s}  {'Location':<18s}  {'Due':<12s}  {'GC':<16s}")
print("  " + "-" * (W-4))
for i, b in enumerate(upcoming, 1):
    print(f"  {i:>2d}  {fmt_dist(b):>6s}  {b['project_name'][:42]:<42s}  {fmt_loc(b)[:18]:<18s}  {b.get('due_date',''):<12s}  {b.get('gc','')[:16]}")

# === TOP 3 DEEP DIVE ===
print(f"\n{'='*W}")
print("  TOP 3 NEAREST DUE TODAY - FULL PROJECT BRIEFS")
print("=" * W)

for i, b in enumerate(today_bids[:3], 1):
    d = b.get("distance_miles")
    dist_str = f"{d:.0f} mi" if d and d < 900 else "? mi"
    gc_name = b.get("gc", "Unknown")
    gc_data = lookup_gc(gc_name)
    ftype = classify_facility(b["project_name"])
    ft_data = fts.get(ftype)
    rec, tier, reason = recommend(b)

    print(f"\n  {i}. {b['project_name'].upper()} - {b.get('city','')}, {b.get('state','')[:2]} ({dist_str})")
    print("  " + "-" * (W-4))
    print(f"  GC: {gc_name} | Contact: {b.get('gc_contact','TBD')}")
    print(f"  Due: {b.get('due_date','')} | Size: {b.get('size_sf','TBD')} SF | Source: {b.get('source','')}")
    print(f"  Facility Type: {ftype.replace('_',' ').title()}")

    # Scope info
    print(f"\n  SCOPE (Painting & Wallcovering):")
    if ftype == "dental":
        print("  - Interior walls: standard latex eggshell")
        print("  - Restrooms: semi-gloss latex")
        print("  - Trim & doors: semi-gloss")
        print("  - Small upfit, likely 2-3 day job")
    elif ftype == "government_military":
        print("  - Interior/exterior painting per UFGS specifications")
        print("  - Likely includes: walls, ceilings, doors/frames")
        print("  - May require prevailing wage rates")
        print("  - Check for HAZMAT/lead paint conditions")
    elif "retail" in ftype or "grocery" in ftype:
        print("  - Interior walls: sales floor + back of house")
        print("  - Doors & frames, columns, trim")
        print("  - Possible exterior touch-up")
        print("  - Wallcovering per finish schedule")
    else:
        print("  - Scope details pending document download")
        print("  - Review bid docs on portal for full scope")

    # Documents
    print(f"\n  DOCUMENTS: Check {b.get('source','')} portal for bid docs")

    # GC History
    if gc_data:
        print(f"\n  GC HISTORY: {gc_data['name']} - {gc_data['projects_won']}/{gc_data['projects_bid']} wins ({gc_data.get('win_rate',0)*100:.0f}% win rate)")
        print(f"  {gc_data.get('pricing_notes','')[:120]}")
        if gc_data.get('key_wins'):
            print(f"  Key wins: {', '.join(gc_data['key_wins'][:2])}")
        if gc_data.get('loss_patterns'):
            print(f"  Loss pattern: {gc_data['loss_patterns'][:100]}")
    else:
        print(f"\n  GC HISTORY: {gc_name} - NEW GC, no prior history")

    # Facility pattern
    if ft_data:
        avg = ft_data.get("avg_contract_value", 0)
        cnt = ft_data.get("completed_count", 0)
        print(f"\n  FACILITY PATTERN: {ftype.replace('_',' ').title()} - {cnt} completed, avg ${avg:,}")
        if ft_data.get("pricing_notes"):
            print(f"  {ft_data['pricing_notes'][:120]}")

    # Recommendation
    print(f"\n  CAROL'S TAKE: {rec} ({tier} tier)")
    print(f"  {reason}")
    print("  " + "-" * (W-4))

# === RECOMMENDATIONS FOR ALL ===
print(f"\n{'='*W}")
print("  CAROL'S RECOMMENDATIONS - ALL DUE TODAY")
print("=" * W)

for i, b in enumerate(today_bids, 1):
    d = b.get("distance_miles")
    dist_str = f"{d:.0f} mi" if d and isinstance(d, (int, float)) and d < 900 else "? mi"
    rec, tier, reason = recommend(b)
    arrow = {
        "BID": ">>>  BID   ",
        "CONSIDER": ">>  CONSIDER",
        "SKIP": ">   SKIP   "
    }.get(rec, rec)
    short_name = b["project_name"][:35]
    print(f"  {i}. {short_name:<35s} ({dist_str:>6s}) {arrow} - {reason[:55]}")

print(f"\n  SOURCES: BC = {sum(1 for b in bids if b.get('source')=='buildingconnected')} | CC = {sum(1 for b in bids if b.get('source')=='constructconnect')} | Email = 0")
print(f"  TOTAL PIPELINE: {len(bids)} projects")
print("=" * W)
print("\n  Ask me about any project for full details. All data cached and ready.")
