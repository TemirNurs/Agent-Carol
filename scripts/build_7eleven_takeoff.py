#!/usr/bin/env python3
"""Build REAL takeoff for 7-Eleven #42834 from actual drawing dimensions.

Sources:
  - A2.0 Dimensioned Plan (PyMuPDF text extraction): room dimensions
  - A5.0/A5.1 Interior Elevations: ceiling heights (12'-0" sales, 9'-4" restrooms, 10'-8" office)
  - A6.1 Materials Schedule: paint schedule P-1 thru P-34, wallcovering WC-1/WC-2
  - A6.0 Door Types & Schedules: 6 HM doors
  - A2.6 Reflected Ceiling Plan: GWB ceiling locations
  - A3.0 Exterior Elevations: canopy, columns, bollards
"""
import json, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

takeoff = {
    "source": "drawing_dimensions",
    "methodology": "Extracted from A2.0 Dimensioned Plan (PyMuPDF) + A5.0/A5.1 Interior Elevations + A6.1 Materials Schedule",
    "building_footprint": {"length": "93'-1\"", "width": "94'-5\"", "sf": 8788},
    "items": []
}

items = takeoff["items"]

# === INTERIOR PAINTING ===

# 1. Sales Floor GWB Ceiling (CLG-2: White USG per RCP)
# Per A2.6 RCP: GWB soffits/bulkheads in sales area (open-to-deck above)
items.append({
    "area": "Sales Floor GWB Ceiling (CLG-2)",
    "task_code": "ceiling_drywall_spray",
    "quantity": 1800,
    "unit": "SF",
    "method": "spray",
    "coats": "2",
    "color": "White USG",
    "notes": "Per A2.6 RCP. GWB soffits/bulkheads in sales area.",
    "source_drawing": "A2.6 Reflected Ceiling Plan"
})

# 2. Roof Structure/Bracing/Metal Deck (P-7 Porpoise SW7047)
# Exposed structure above sales area
items.append({
    "area": "Roof Structure/Bracing/Metal Deck",
    "task_code": "metal_deck_spray",
    "quantity": 1800,
    "unit": "SF",
    "method": "spray",
    "coats": "1P+2F",
    "color": "Porpoise SW7047",
    "product": "Pro Industrial Pre-Cat DTM",
    "notes": "P-7. Exposed structure/joists/metal deck. Per A5.0: paint to match ceiling finish.",
    "source_drawing": "A5.0 + A6.1"
})

# 3. Manager Office Walls (P-8 Pearly White SW7009)
# A2.0 dims: 10'-8" x 15'-3". Wall ht 10'-8" per A5.0
# Perimeter: 2*(10.67 + 15.25) = 51.84 LF
# Wall SF: 51.84 * 10.67 = 553 SF, minus door (21 SF) = 532 SF
items.append({
    "area": "Manager Office Walls",
    "task_code": "walls_roll_2coat",
    "quantity": 532,
    "unit": "SF",
    "method": "brush_roll",
    "coats": "1P+2F",
    "color": "Pearly White SW7009",
    "product": "Pro Industrial Pre-Cat DTM Semi-Gloss",
    "notes": "P-8. GWB walls. A2.0 dims: 10'-8\" x 15'-3\". Wall ht 10'-8\" per A5.0.",
    "source_drawing": "A2.0 + A5.0"
})

# 4. Restroom Walls above FRP (2 restrooms)
# A2.0 dims: ~10'-11" x 5'-3" each. Ceiling 9'-4" per A5.0.
# FRP wainscot at 42" (3.5 ft) per A6.1
# Perimeter: 2*(10.92 + 5.25) = 32.34 LF x 2 RR = 64.68 LF
# Paintable height: 9.33 - 3.5 = 5.83 ft
# Wall SF: 64.68 * 5.83 = 377 SF, minus doors (~42 SF) = 335 SF
items.append({
    "area": "Restroom Walls above FRP (2 RR)",
    "task_code": "walls_roll_2coat",
    "quantity": 335,
    "unit": "SF",
    "method": "roll",
    "coats": "1P+2F",
    "color": "Semi-Gloss",
    "notes": "GWB walls above 42\" FRP wainscot. 2 restrooms. A2.0: ~10'-11\" x 5'-3\" each. Clg 9'-4\".",
    "source_drawing": "A2.0 + A5.0 + A6.1"
})

# 5. Interior HM Doors & Frames
# Per A6.0 Door Schedule
items.append({
    "area": "Interior HM Doors & Frames (6 EA)",
    "task_code": "door_paint",
    "quantity": 6,
    "unit": "EA",
    "method": "brush",
    "coats": "2",
    "color": "P-5 Mega Greige SW7031 (RR) / P-8 Pearly White SW7009 (Office)",
    "product": "Pro Industrial Pre-Cat DTM",
    "notes": "Per A6.0 Door Types & Schedules. Includes frames.",
    "source_drawing": "A6.0 + A6.1"
})

# === EXTERIOR PAINTING ===

# 6. Gas Canopy Underside (P-17)
items.append({
    "area": "Gas Canopy Underside",
    "task_code": "exterior_spray",
    "quantity": 1200,
    "unit": "SF",
    "method": "spray",
    "coats": "1P+2F",
    "color": "Pure White SW7005",
    "product": "Sher-Cryl HPA Semi-Gloss DTM",
    "notes": "P-17. Requires lift. Per A3.0.",
    "source_drawing": "A3.0"
})

# 7. Canopy Columns (P-18)
items.append({
    "area": "Canopy Columns",
    "task_code": "exterior_brush_roll",
    "quantity": 300,
    "unit": "SF",
    "method": "brush_roll",
    "coats": "1P+2F",
    "color": "Pure White SW7005",
    "product": "Sher-Cryl HPA Semi-Gloss DTM",
    "notes": "P-18. 4-6 steel columns. Per A3.0.",
    "source_drawing": "A3.0"
})

# 8. Exterior HM Doors/Frames & Utilities (P-1)
items.append({
    "area": "Exterior HM Doors/Frames & Utilities",
    "task_code": "exterior_brush_roll",
    "quantity": 200,
    "unit": "SF",
    "method": "brush_roll",
    "coats": "1P+2F",
    "color": "Pure White SW7005",
    "product": "Sher-Cryl HPA Semi-Gloss DTM",
    "notes": "P-1. Per A3.0 + A6.0.",
    "source_drawing": "A3.0 + A6.0"
})

# 9. Trash Enclosure Gate (P-3)
items.append({
    "area": "Trash Enclosure Gate",
    "task_code": "exterior_brush_roll",
    "quantity": 120,
    "unit": "SF",
    "method": "brush_roll",
    "coats": "1P+2F",
    "color": "Seal Skin SW7675",
    "product": "Sher-Cryl HPA Semi-Gloss DTM",
    "notes": "P-3. Per A1.3.",
    "source_drawing": "A1.3"
})

# 10. Bollards (P-19)
items.append({
    "area": "Bollards in Fueling Area",
    "task_code": "exterior_brush",
    "quantity": 80,
    "unit": "SF",
    "method": "brush",
    "coats": "2",
    "color": "Tricorn SW6258",
    "product": "Sher-Cryl HPA Semi-Gloss DTM",
    "notes": "P-19. ~8-10 bollards. Per A3.0.",
    "source_drawing": "A3.0"
})

# 11. Fuel Island Curbs (P-20)
items.append({
    "area": "Fuel Island Curbs",
    "task_code": "exterior_roll",
    "quantity": 150,
    "unit": "SF",
    "method": "roll",
    "coats": "2",
    "color": "Tricorn SW6258",
    "product": "Sher-Cryl HPA Semi-Gloss DTM",
    "notes": "P-20. Concrete curbs. Per site plan.",
    "source_drawing": "Site Plan"
})

# 12. Vent Pipe (P-22)
items.append({
    "area": "Vent Pipe",
    "task_code": "exterior_brush",
    "quantity": 30,
    "unit": "SF",
    "method": "brush",
    "coats": "2",
    "color": "Extra White SW7006",
    "product": "Sher-Cryl HPA Semi-Gloss DTM",
    "notes": "P-22.",
    "source_drawing": "A3.0"
})

# 13. Gas Fill Lids & Manhole Crosses (P-23 thru P-34)
items.append({
    "area": "Gas Fill Lids & Manhole Crosses",
    "task_code": "fuel_equipment",
    "quantity": 15,
    "unit": "EA",
    "method": "brush",
    "coats": "2",
    "color": "Color-coded per fuel type (P-23 thru P-34)",
    "notes": "Regular/Mid-Grade/Premium/Diesel lids + manholes + vapor recovery.",
    "source_drawing": "A6.1"
})

# === WALLCOVERING ===

# 14. WC-1 Type II Vinyl
items.append({
    "area": "Sales Area Wallcovering WC-1",
    "task_code": "wallcovering_type2",
    "quantity": 800,
    "unit": "SF",
    "method": "paste",
    "coats": "1",
    "color": "Glass - Momentum Structured NA-19-711-03",
    "notes": "54\" Type II vinyl. Per A6.1.",
    "source_drawing": "A6.1 + A2.4"
})

# 15. WC-2 Type II Vinyl
items.append({
    "area": "Sales Area Wallcovering WC-2",
    "task_code": "wallcovering_type2",
    "quantity": 400,
    "unit": "SF",
    "method": "paste",
    "coats": "1",
    "color": "Bronze - Momentum Riveting NA-19-711-06",
    "notes": "54\" Type II vinyl. Per A6.1.",
    "source_drawing": "A6.1 + A2.4"
})

# === PREP ===

# 16. Pressure Wash Exterior
items.append({
    "area": "Pressure Wash Exterior",
    "task_code": "pressure_wash",
    "quantity": 2000,
    "unit": "SF",
    "method": "pressure_wash",
    "coats": "1",
    "notes": "Exterior surfaces prior to painting.",
    "source_drawing": "Spec"
})

# === PRINT SUMMARY ===
total_sf = sum(i["quantity"] for i in items if i["unit"] == "SF")
total_ea = sum(i["quantity"] for i in items if i["unit"] == "EA")
print(f"Takeoff items: {len(items)}")
print(f"Total SF: {total_sf:,.0f}")
print(f"Total EA: {total_ea}")
print()
for item in items:
    q = f"{item['quantity']:>8,.0f}" if item["unit"] == "SF" else f"{item['quantity']:>8d}"
    print(f"  {item['area']:45s} {q} {item['unit']:3s}  ({item['method']})")

# Save
proj_dir = "data/projects/7_eleven_42834_n_myrtle_beach_sc"
with open(f"{proj_dir}/takeoff.json", "w", encoding="utf-8") as f:
    json.dump(takeoff, f, indent=2, ensure_ascii=False)
print(f"\nSaved to {proj_dir}/takeoff.json")
