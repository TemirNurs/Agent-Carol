#!/usr/bin/env python3
"""Build structured SOW for 7-Eleven #42834 from actual scope_extract data."""
import json, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sow = {
    "interior_painting": {
        "walls": [
            {"area": "Manager Office Walls", "description": "GWB walls, P-8 Pearly White SW7009", "finish": "Semi-Gloss DTM", "method": "brush_roll", "coats": "1P+2F", "estimated_sf": 350},
            {"area": "Restroom Walls (above FRP)", "description": "GWB walls above 42in FRP wainscot", "finish": "Semi-Gloss", "method": "roll", "coats": "1P+2F", "estimated_sf": 250},
        ],
        "ceilings": [
            {"area": "Sales Floor Ceiling (GWB)", "type": "GWB", "description": "CLG-2 White USG - per RCP", "method": "spray", "coats": "2", "estimated_sf": 1800},
        ],
        "doors": {"count": 6, "type": "hollow_core", "description": "P-5 Restroom doors Mega Greige SW7031, P-8 Manager office Pearly White SW7009", "includes_frames": True, "coats": "2"},
        "frames": {"count": 6, "type": "HM", "coats": "2"},
        "trim": [],
        "misc": [
            {"item": "Roof Structure/Bracing/Metal Deck", "description": "P-7 Porpoise SW7047 DTM Semi-Gloss", "quantity": 1800, "unit": "SF"},
        ]
    },
    "exterior_painting": {
        "surfaces": [
            {"area": "Exterior HM Doors/Frames & Utilities", "substrate": "metal", "description": "P-1 Pure White SW7005 DTM", "method": "brush_roll", "coats": "1P+2F", "estimated_sf": 200},
            {"area": "Trash Enclosure Gate", "substrate": "metal", "description": "P-3 Seal Skin SW7675 DTM", "method": "brush_roll", "coats": "1P+2F", "estimated_sf": 120},
            {"area": "Gas Canopy Underside", "substrate": "metal", "description": "P-17 Pure White SW7005 DTM", "method": "spray", "coats": "1P+2F", "estimated_sf": 1200},
            {"area": "Canopy Columns", "substrate": "metal", "description": "P-18 Pure White SW7005 DTM", "method": "brush_roll", "coats": "1P+2F", "estimated_sf": 300},
            {"area": "Bollards in Fueling Area", "substrate": "metal", "description": "P-19 Tricorn SW6258 DTM", "method": "brush", "coats": "2", "estimated_sf": 80},
            {"area": "Fuel Island Curbs", "substrate": "concrete", "description": "P-20 Tricorn SW6258 DTM", "method": "roll", "coats": "2", "estimated_sf": 150},
            {"area": "Vent Pipe", "substrate": "metal", "description": "P-22 Extra White SW7006", "method": "brush", "coats": "2", "estimated_sf": 30},
        ],
        "fuel_equipment_painting": [
            {"item": "Gas Fill Lids & Manhole Crosses", "description": "Color-coded per fuel type (P-23 thru P-34)", "quantity": 15, "unit": "EA"},
        ],
        "prep": [
            {"item": "pressure_wash", "estimated_sf": 2000, "notes": "Exterior surfaces prior to painting"},
        ]
    },
    "wallcovering": {
        "locations": [
            {"area": "Sales Area - WC-1", "type": "Type II vinyl", "description": "54in Momentum Structured NA-19-711-03 Glass", "estimated_sf": 800},
            {"area": "Sales Area - WC-2", "type": "Type II vinyl", "description": "54in Momentum Bronze", "estimated_sf": 400},
        ]
    },
    "prep_work": {
        "items": [
            {"task": "priming", "estimated_sf_or_lf": 4600, "notes": "All new GWB and metal surfaces"},
            {"task": "masking", "estimated_sf_or_lf": 3000, "notes": "Protect equipment, glass, flooring"},
            {"task": "caulking", "estimated_sf_or_lf": 500, "notes": "Seal exterior joints"},
        ]
    },
    "exclusions": [
        "FRP installation (GC scope per A6.1)",
        "Pre-finished metal panels, coping, shutters (factory-finished)",
        "Floor painting / epoxy",
        "Storefront glazing",
        "Signage installation",
        "Stucco finish system (by stucco sub)",
        "Beer cave / cooler / freezer pre-fab finish",
    ],
    "special_conditions": {
        "prevailing_wage": False,
        "night_work": False,
        "phased": False,
        "occupied": False,
        "high_work": True,
        "containment": False,
        "notes": "Ground-up new construction. All SW per 7-Eleven spec. Canopy requires lift."
    },
    "summary": {
        "total_sf_walls": 600,
        "total_sf_ceilings": 1800,
        "total_doors": 6,
        "total_lf_trim": 0,
        "total_sf_exterior": 2080,
        "total_sf_wallcovering": 1200,
        "complexity": "moderate",
        "key_scope_items": [
            "Interior: GWB ceiling spray, manager office & restroom walls, doors/frames",
            "Exterior: Gas canopy underside, columns, bollards, fuel curbs, HM doors",
            "Fuel equipment: Color-coded fill lids & manhole crosses",
            "Wallcovering: Type II vinyl (WC-1, WC-2) in sales areas",
            "Roof structure/bracing painting (P-7 Porpoise)",
            "All Sherwin-Williams products per 7-Eleven national account"
        ],
        "estimated_duration_days": 5
    },
    "paint_schedule": {
        "P-1": {"application": "Exterior Utilities, HM", "color": "Pure White SW7005", "product": "Sher-Cryl HPA Semi-Gloss DTM"},
        "P-3": {"application": "Trash Enclosure Gate", "color": "Seal Skin SW7675", "product": "Sher-Cryl HPA Semi-Gloss DTM"},
        "P-5": {"application": "Restroom Doors & Frames", "color": "Mega Greige SW7031", "product": "Pro Industrial Pre-Cat DTM"},
        "P-7": {"application": "Roof Structure/Bracing", "color": "Porpoise SW7047", "product": "Pro Industrial Pre-Cat DTM"},
        "P-8": {"application": "Manager Office Door/Frame", "color": "Pearly White SW7009", "product": "Pro Industrial Pre-Cat DTM"},
        "P-17": {"application": "Gas Canopy Underside", "color": "Pure White SW7005", "product": "Sher-Cryl HPA Semi-Gloss DTM"},
        "P-18": {"application": "Canopy Columns", "color": "Pure White SW7005", "product": "Sher-Cryl HPA Semi-Gloss DTM"},
        "P-19": {"application": "Bollards", "color": "Tricorn SW6258", "product": "Sher-Cryl HPA Semi-Gloss DTM"},
        "P-20": {"application": "Fuel Island Curbs", "color": "Tricorn SW6258", "product": "Sher-Cryl HPA Semi-Gloss DTM"},
    },
    "_source": "scope_extract + A6.1 Materials Schedules paint schedule",
    "_generated_at": "2026-04-09"
}

with open("data/projects/7_eleven_42834_n_myrtle_beach_sc/sow.json", "w", encoding="utf-8") as f:
    json.dump(sow, f, indent=2, ensure_ascii=False)

print("SOW built from actual 7-Eleven bid docs (A6.1 Paint Schedule):")
print(f"  Interior walls: {sow['summary']['total_sf_walls']:,} SF")
print(f"  Ceilings: {sow['summary']['total_sf_ceilings']:,} SF")
print(f"  Doors: {sow['summary']['total_doors']}")
print(f"  Exterior: {sow['summary']['total_sf_exterior']:,} SF")
print(f"  Wallcovering: {sow['summary']['total_sf_wallcovering']:,} SF")
print(f"  Roof structure: 1,800 SF")
print(f"  Fuel equip: 15 EA")
print(f"  Duration: {sow['summary']['estimated_duration_days']} days")
print()
for item in sow["summary"]["key_scope_items"]:
    print(f"  - {item}")
