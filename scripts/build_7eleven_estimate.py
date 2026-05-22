#!/usr/bin/env python3
"""Build estimate for 7-Eleven #42834 from real takeoff data and CCF pricing."""
import json, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Load takeoff and pricing
takeoff = json.load(open("data/projects/7_eleven_42834_n_myrtle_beach_sc/takeoff.json", encoding="utf-8"))

# CCF burdened labor rate
LABOR_RATE = 28.00  # $/hr burdened (experienced crew for commercial)

# Pricing tier: TARGET (OH=12%, Profit=18%)
OH_PCT = 0.12
PROFIT_PCT = 0.18

# Unit prices from CCF config (TARGET tier mid-range)
unit_prices = {
    "ceiling_spray": 0.475,
    "metal_deck_spray": 0.50,
    "walls_brush_roll": 0.65,
    "walls_roll": 0.65,
    "exterior_spray": 0.60,
    "exterior_brush_roll": 0.75,
    "exterior_brush": 0.75,
    "exterior_roll": 0.75,
    "door_complete": 75.00,
    "fuel_equipment": 45.00,
    "wallcovering_type2": 2.50,
    "pressure_wash": 0.15,
}

estimate_items = []
total_labor = 0
total_material = 0

for item in takeoff["items"]:
    qty = item["quantity"]
    area = item["area"]
    method = item["method"]
    task = item.get("task_code", "")

    # Determine unit price
    if "ceiling" in task or "ceiling" in area.lower():
        price = unit_prices["ceiling_spray"]
    elif "metal_deck" in task or "Roof Structure" in area:
        price = unit_prices["metal_deck_spray"]
    elif "door" in task:
        price = unit_prices["door_complete"]
    elif "fuel_equipment" in task:
        price = unit_prices["fuel_equipment"]
    elif "wallcovering" in task:
        price = unit_prices["wallcovering_type2"]
    elif "pressure_wash" in task:
        price = unit_prices["pressure_wash"]
    elif "exterior" in task or "Canopy" in area or "Bollard" in area or "Trash" in area or "Vent" in area or "Fuel Island" in area:
        if "spray" in method:
            price = unit_prices["exterior_spray"]
        else:
            price = unit_prices["exterior_brush_roll"]
    elif "brush_roll" in method:
        price = unit_prices["walls_brush_roll"]
    elif "roll" in method:
        price = unit_prices["walls_roll"]
    else:
        price = unit_prices["walls_brush_roll"]

    line_total = qty * price

    # Labor/material split
    if "wallcovering" in task:
        labor_pct = 0.60
    elif "pressure_wash" in task:
        labor_pct = 0.85
    else:
        labor_pct = 0.70

    labor = line_total * labor_pct
    material = line_total * (1 - labor_pct)
    total_labor += labor
    total_material += material

    estimate_items.append({
        "area": area,
        "quantity": qty,
        "unit": item["unit"],
        "unit_price": price,
        "line_total": round(line_total, 2),
        "labor": round(labor, 2),
        "material": round(material, 2),
        "method": method,
        "color": item.get("color", ""),
    })

# Calculate totals
direct_cost = total_labor + total_material
overhead = direct_cost * OH_PCT
subtotal = direct_cost + overhead
profit = subtotal * PROFIT_PCT
equipment = 450.00  # Boom lift rental
mobilization = 350.00  # Crew travel to N. Myrtle Beach
total_bid = subtotal + profit + equipment + mobilization

print("=== 7-ELEVEN #42834 N. MYRTLE BEACH ESTIMATE ===")
print(f"Source: Drawing dimensions (A2.0, A5.0, A6.1)")
print()
print(f"{'Item':45s} {'Qty':>8s} {'Unit':>4s} {'$/Unit':>8s} {'Total':>10s}")
print("-" * 80)
for item in estimate_items:
    q = f"{item['quantity']:,.0f}" if item["unit"] == "SF" else f"{item['quantity']}"
    print(f"{item['area']:45s} {q:>8s} {item['unit']:>4s} {item['unit_price']:>8.2f} ${item['line_total']:>9,.2f}")

print("-" * 80)
print(f"{'Direct Labor':45s} {'':>22s} ${total_labor:>9,.2f}")
print(f"{'Materials':45s} {'':>22s} ${total_material:>9,.2f}")
print(f"{'Direct Cost':45s} {'':>22s} ${direct_cost:>9,.2f}")
print(f"{'Overhead (12%)':45s} {'':>22s} ${overhead:>9,.2f}")
print(f"{'Subtotal':45s} {'':>22s} ${subtotal:>9,.2f}")
print(f"{'Profit (18%)':45s} {'':>22s} ${profit:>9,.2f}")
print(f"{'Equipment (Lift Rental)':45s} {'':>22s} ${equipment:>9,.2f}")
print(f"{'Mobilization':45s} {'':>22s} ${mobilization:>9,.2f}")
print("=" * 80)
print(f"{'TOTAL BID (TARGET Tier)':45s} {'':>22s} ${total_bid:>9,.2f}")

# Duration
labor_hours = total_labor / LABOR_RATE
crew_size = 3
days = labor_hours / (crew_size * 8)
print(f"\nEstimated labor hours: {labor_hours:.0f}")
print(f"Crew size: {crew_size}")
print(f"Estimated duration: {days:.1f} days")

# Save estimate
estimate = {
    "project": "7-Eleven #42834 - N. Myrtle Beach, SC",
    "source": "drawing_dimensions",
    "methodology": takeoff["methodology"],
    "tier": "TARGET",
    "oh_pct": OH_PCT,
    "profit_pct": PROFIT_PCT,
    "items": estimate_items,
    "summary": {
        "direct_labor": round(total_labor, 2),
        "materials": round(total_material, 2),
        "direct_cost": round(direct_cost, 2),
        "overhead": round(overhead, 2),
        "profit": round(profit, 2),
        "equipment": equipment,
        "mobilization": mobilization,
        "total_bid": round(total_bid, 2),
        "labor_hours": round(labor_hours, 1),
        "crew_size": crew_size,
        "duration_days": round(days, 1),
    },
    "exclusions": [
        "FRP installation (GC scope per A6.1)",
        "Pre-finished metal panels, coping, shutters",
        "Floor painting / epoxy",
        "Storefront glazing",
        "Signage installation",
        "Stucco finish system",
        "Beer cave / cooler / freezer pre-fab finish",
    ],
}

with open("data/projects/7_eleven_42834_n_myrtle_beach_sc/estimate.json", "w", encoding="utf-8") as f:
    json.dump(estimate, f, indent=2, ensure_ascii=False)
print("\nEstimate saved to data/projects/7_eleven_42834_n_myrtle_beach_sc/estimate.json")
