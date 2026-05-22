#!/usr/bin/env python3
"""Test daily briefing - compile BC + CC bids, sort by distance."""
import sys, json
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, r'C:\Users\Nursm\Agent Carol\skills\ccf-estimator\scripts')
from distance_calc import add_distance_to_bids
from pathlib import Path

# BC results (31 bids from live scrape)
bc_bids = [
    {"project_name": "Butner Facility Maintenance Shop 13 Repairs", "trade": "Finishes", "due_date": "4/2/2026", "size_sf": "13350", "city": "Butner", "state": "North Carolina", "gc": "CMC Building Inc.", "gc_contact": "Parin Bodiwala", "source": "buildingconnected"},
    {"project_name": "Fayetteville, NC VAMC EHRM Upgrade", "trade": "Painting", "due_date": "4/2/2026", "size_sf": "7216", "city": "Fayetteville", "state": "North Carolina", "gc": "VALIANT Construction, LLC", "gc_contact": "Dmitriy Redka", "source": "buildingconnected"},
    {"project_name": "Savers - Fayetteville, NC", "trade": "Painting", "due_date": "4/2/2026", "size_sf": "45183", "city": "Fayetteville", "state": "North Carolina", "gc": "DeLauter Development", "gc_contact": "Justin Hibbard", "source": "buildingconnected"},
    {"project_name": "Savers - Fayetteville, NC", "trade": "Painting", "due_date": "4/3/2026", "city": "Fayetteville", "state": "North Carolina", "gc": "Atlas Building Group", "source": "buildingconnected"},
    {"project_name": "Savers, Fayetteville NC", "trade": "Painting", "due_date": "4/3/2026", "size_sf": "44250", "city": "Fayetteville", "state": "North Carolina", "gc": "A. Eilers Construction", "source": "buildingconnected"},
    {"project_name": "Towneplace Suites by Marriott - NC", "trade": "Painting", "due_date": "4/3/2026", "size_sf": "62137", "city": "Elizabeth City", "state": "North Carolina", "gc": "Path Construction", "source": "buildingconnected"},
    {"project_name": "EHRM Infrastructure Upgrades (Fayetteville NC", "trade": "Painting", "due_date": "4/3/2026", "city": "Fayetteville", "state": "North Carolina", "gc": "Dawes Construction", "source": "buildingconnected"},
    {"project_name": "Eggs Up Grill - Wilmington, NC", "trade": "Painting", "due_date": "4/6/2026", "city": "Wilmington", "state": "North Carolina", "gc": "Benchmark Building Solutions", "source": "buildingconnected"},
    {"project_name": "Install Campus Generator", "trade": "Finishes", "due_date": "4/6/2026", "city": "Salisbury", "state": "North Carolina", "gc": "Pointer Construction Group", "source": "buildingconnected"},
    {"project_name": "Victorias Secret- Greensboro, NC", "trade": "Painting", "due_date": "4/6/2026", "city": "Greensboro", "state": "North Carolina", "gc": "Construction One Inc.", "source": "buildingconnected"},
    {"project_name": "Camp Butner Training Center Latrine Renovations", "trade": "Painting", "due_date": "4/7/2026", "city": "Stem", "state": "North Carolina", "gc": "CMC Building Inc.", "source": "buildingconnected"},
    {"project_name": "Salisbury, NC VAMC - Install Campus Generator", "trade": "Painting", "due_date": "4/7/2026", "city": "Salisbury", "state": "North Carolina", "gc": "VALIANT Construction, LLC", "source": "buildingconnected"},
    {"project_name": "Woodlawn Community Fellowship", "trade": "Painting", "due_date": "4/7/2026", "city": "Charlotte", "state": "North Carolina", "gc": "Metrolina Builders Inc.", "source": "buildingconnected"},
    {"project_name": "US Postal Service VMF - Hickory, NC", "trade": "Painting", "due_date": "4/7/2026", "city": "Hickory", "state": "North Carolina", "gc": "Rectenwald Brothers Construction", "source": "buildingconnected"},
    {"project_name": "Victorias Secret #183", "trade": "Painting", "due_date": "4/7/2026", "city": "Greensboro", "state": "North Carolina", "gc": "Fred Olivieri Construction", "source": "buildingconnected"},
    {"project_name": "USC Colonial Life Arena Elevator Lobby", "trade": "Painting", "due_date": "4/8/2026", "city": "Columbia", "state": "South Carolina", "gc": "Construction Services Group", "source": "buildingconnected"},
    {"project_name": "Bojangles - Weaverville, NC", "trade": "Painting", "due_date": "4/13/2026", "city": "Weaverville", "state": "North Carolina", "gc": "Benchmark Building Solutions", "source": "buildingconnected"},
    {"project_name": "SOF Mission Command Center, Ft. Bragg", "trade": "Painting", "due_date": "4/13/2026", "city": "Fort Bragg", "state": "North Carolina", "gc": "Sauer Construction", "source": "buildingconnected"},
    {"project_name": "Dutch Bros - Spartanburg", "trade": "Painting", "due_date": "4/14/2026", "city": "Spartanburg", "state": "South Carolina", "gc": "Integrity Construction", "source": "buildingconnected"},
    {"project_name": "Newington Elementary School Ceiling", "trade": "Painting", "due_date": "4/15/2026", "city": "Summerville", "state": "South Carolina", "gc": "Construction Services Group", "source": "buildingconnected"},
    {"project_name": "Chase Bank Ground Up - Conway, SC", "trade": "Painting", "due_date": "4/15/2026", "city": "Conway", "state": "South Carolina", "gc": "Singleton Construction", "source": "buildingconnected"},
    {"project_name": "Wilson County East EMS Station", "trade": "Painting", "due_date": "4/16/2026", "city": "Wilson", "state": "North Carolina", "gc": "CMC Building Inc.", "source": "buildingconnected"},
    {"project_name": "Home 2 Suites Carowinds, Ft. Mills, SC", "trade": "Painting", "due_date": "4/16/2026", "city": "Ft. Mills", "state": "South Carolina", "gc": "Path Construction", "source": "buildingconnected"},
    {"project_name": "Columbia VA Training & Admin Space", "trade": "Painting", "due_date": "4/16/2026", "city": "Columbia", "state": "South Carolina", "gc": "Construction Services Group", "source": "buildingconnected"},
    {"project_name": "F-35 Composite Repair Facility at MCAS", "trade": "Painting", "due_date": "4/17/2026", "city": "Havelock", "state": "North Carolina", "gc": "ECC", "source": "buildingconnected"},
    {"project_name": "SCSU New Residence Hall - GMP", "trade": "Painting", "due_date": "4/20/2026", "city": "Orangeburg", "state": "South Carolina", "gc": "Ajax", "source": "buildingconnected"},
    {"project_name": "USCG STA Oregon Inlet Gutter Replacement", "trade": "Painting", "due_date": "4/20/2026", "city": "Nags Head", "state": "North Carolina", "gc": "Capital Trades, LLC", "source": "buildingconnected"},
    {"project_name": "Midway Lake Norman", "trade": "Painting", "due_date": "4/21/2026", "city": "Lake Norman of Catawba", "state": "North Carolina", "gc": "Integrity Construction", "source": "buildingconnected"},
    {"project_name": "USC National Guard Road Field Improvements", "trade": "Painting", "due_date": "4/21/2026", "city": "Columbia", "state": "South Carolina", "gc": "Construction Services Group", "source": "buildingconnected"},
    {"project_name": "CMS Dilworth Latta Elementary School Renovation", "trade": "Painting", "due_date": "4/21/2026", "city": "Charlotte", "state": "North Carolina", "gc": "D.A. Everett Construction", "source": "buildingconnected"},
    {"project_name": "CVS #5531-Orangeburg, SC", "trade": "Painting", "due_date": "5/1/2026", "city": "Orangeburg", "state": "South Carolina", "gc": "UHC Construction Services", "source": "buildingconnected"},
]

# CC results (5 due today from live scrape)
cc_bids = [
    {"project_name": "Savannah Rapids Pavillion Renovation", "gc": "McKnight Construction Company", "city": "Martinez", "state": "Georgia", "due_date": "4/2/2026", "source": "constructconnect"},
    {"project_name": "Downtown Government Complex Elevator and Escalator", "gc": "H. M. Kern Corporation", "city": "Raleigh", "state": "North Carolina", "due_date": "4/2/2026", "source": "constructconnect"},
    {"project_name": "Camp Butner Facility Maintenance Shop 13 Repairs", "gc": "CMC Building, Inc", "city": "Butner", "state": "North Carolina", "due_date": "4/2/2026", "source": "constructconnect"},
    {"project_name": "Heartland Dental Upfit- Wesley Chapel, NC", "gc": "WIMCO Corp", "city": "Wesley Chapel", "state": "North Carolina", "due_date": "4/2/2026", "source": "constructconnect"},
    {"project_name": "HVAC Replacement", "gc": "Group III Management", "city": "", "state": "", "due_date": "4/2/2026", "source": "constructconnect"},
]

all_bids = bc_bids + cc_bids
all_bids = add_distance_to_bids(all_bids)

# Save cache
cache_file = Path(r"C:\Users\Nursm\Agent Carol\data\memory\active_bids.json")
cache_file.parent.mkdir(parents=True, exist_ok=True)
with open(cache_file, "w") as f:
    json.dump(all_bids, f, indent=2, default=str)

# === DAILY BRIEFING ===
print("=" * 90)
print("  DAILY BID BRIEFING - Carolina Commercial Finishes")
print(f"  Wednesday, April 2, 2026")
print("=" * 90)

# Due today
today_bids = [b for b in all_bids if "4/2/2026" in str(b.get("due_date", ""))]
today_bids.sort(key=lambda b: b.get("distance_miles") or 999)

print(f"\n  DUE TODAY: {len(today_bids)} projects")
print("  " + "-" * 86)
print(f"  {'#':>2s}  {'Dist':>6s}  {'Project':<42s}  {'Location':<18s}  {'GC':<25s}  {'Source':<5s}")
print("  " + "-" * 86)
for i, b in enumerate(today_bids, 1):
    d = b.get("distance_miles", 999)
    dist = f"{d:.0f} mi" if isinstance(d, (int, float)) and d < 900 else "? mi"
    loc = f"{b.get('city', '')}, {b.get('state', '')[:2]}"
    print(f"  {i:>2d}  {dist:>6s}  {b['project_name'][:42]:<42s}  {loc[:18]:<18s}  {b.get('gc', '')[:25]:<25s}  {b.get('source', '')[:5]}")

# This week (Apr 3-8)
week_dates = ["4/3/2026", "4/4/2026", "4/5/2026", "4/6/2026", "4/7/2026", "4/8/2026"]
week_bids = [b for b in all_bids if b.get("due_date", "") in week_dates]
week_bids.sort(key=lambda b: b.get("distance_miles") or 999)

print(f"\n  DUE THIS WEEK: {len(week_bids)} projects")
print("  " + "-" * 86)
print(f"  {'#':>2s}  {'Dist':>6s}  {'Project':<42s}  {'Location':<18s}  {'Due':<12s}  {'GC':<20s}")
print("  " + "-" * 86)
for i, b in enumerate(week_bids, 1):
    d = b.get("distance_miles", 999)
    dist = f"{d:.0f} mi" if isinstance(d, (int, float)) and d < 900 else "? mi"
    loc = f"{b.get('city', '')}, {b.get('state', '')[:2]}"
    print(f"  {i:>2d}  {dist:>6s}  {b['project_name'][:42]:<42s}  {loc[:18]:<18s}  {b.get('due_date', ''):<12s}  {b.get('gc', '')[:20]}")

# Upcoming
upcoming = [b for b in all_bids if b not in today_bids and b not in week_bids]
upcoming.sort(key=lambda b: b.get("distance_miles") or 999)

print(f"\n  UPCOMING: {len(upcoming)} projects")
print("  " + "-" * 86)
print(f"  {'#':>2s}  {'Dist':>6s}  {'Project':<42s}  {'Location':<18s}  {'Due':<12s}  {'GC':<20s}")
print("  " + "-" * 86)
for i, b in enumerate(upcoming, 1):
    d = b.get("distance_miles", 999)
    dist = f"{d:.0f} mi" if isinstance(d, (int, float)) and d < 900 else "? mi"
    loc = f"{b.get('city', '')}, {b.get('state', '')[:2]}"
    print(f"  {i:>2d}  {dist:>6s}  {b['project_name'][:42]:<42s}  {loc[:18]:<18s}  {b.get('due_date', ''):<12s}  {b.get('gc', '')[:20]}")

print(f"\n  SOURCES: BC = {len(bc_bids)} bids | CC = {len(cc_bids)} bids | Email = 0")
print(f"  TOTAL PIPELINE: {len(all_bids)} projects")
print("=" * 90)
