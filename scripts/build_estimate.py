"""
Build PHASE 3 TARGET tier estimate from Togal AI takeoff measurements.
Hopewell Elementary Phase 2 Gym Addition.
"""
import json
from pathlib import Path

PROJECT_DIR = Path(r"C:\Agent Carol\data\projects\hopewell_elementary_phase2_gym")

# Pricing constants
BURDENED_RATE = 28.00
OH_PCT = 0.12
MARKUP_PCT = 0.18
PAINT_GAL = 38.00
PRIMER_GAL = 32.00
COVERAGE = 350  # SF/gal/coat walls
COVERAGE_PRIMER = 400

line_items = []

def add_paint_item(desc, qty, unit, method, rate_sf_hr, coats, note=""):
    if qty == 0:
        return
    total_coat_sf = qty * coats
    labor_hrs = total_coat_sf / rate_sf_hr
    labor_cost = labor_hrs * BURDENED_RATE
    gals = total_coat_sf / COVERAGE
    material_cost = gals * PAINT_GAL
    # Primer (1 coat)
    primer_hrs = qty / (rate_sf_hr * 1.5)
    primer_cost = primer_hrs * BURDENED_RATE
    primer_mat = (qty / COVERAGE_PRIMER) * PRIMER_GAL
    total_labor = labor_cost + primer_cost
    total_material = material_cost + primer_mat
    line_items.append({
        "desc": desc, "qty": qty, "unit": unit, "method": method,
        "labor_hrs": round(labor_hrs + primer_hrs, 1),
        "labor_cost": round(total_labor, 0),
        "material_cost": round(total_material, 0),
        "direct_cost": round(total_labor + total_material, 0),
        "note": note,
    })

def add_fixed_item(desc, qty, unit, labor_hrs, labor_cost, material_cost, note=""):
    line_items.append({
        "desc": desc, "qty": qty, "unit": unit, "method": "fixed",
        "labor_hrs": labor_hrs,
        "labor_cost": round(labor_cost, 0),
        "material_cost": round(material_cost, 0),
        "direct_cost": round(labor_cost + material_cost, 0),
        "note": note,
    })


def main():
    print("=" * 70)
    print("HOPEWELL ELEMENTARY PHASE 2 GYM ADDITION")
    print("PAINTING ESTIMATE — TARGET TIER")
    print("Based on TOGAL AI TAKEOFF | CCF $28/hr burdened | 12% OH | 18% MU")
    print("=" * 70)

    # === LINE ITEMS FROM TOGAL AI TAKEOFF ===

    # 1. GYM WALLS — P-1 Spray+BR, 22ft
    # Togal: 429 LF perim x 22ft = 9,438 SF gross, 35% deduction = 6,135 SF
    add_paint_item("GYM WALLS - P-1 Spray+BR (22ft AFF)", 6135, "SF",
                   "spray+BR", 300, 2, "Togal 429 LF x 22ft x 0.65")

    # 2. GYM CEILING — PEMB spray, 22ft+
    # Togal: 9,762 SF floor x 1.3 = 12,691 SF
    add_paint_item("GYM CEILING - PEMB structure spray (22ft+)", 12691, "SF",
                   "spray", 250, 2, "Togal 9,762 SF x 1.3 steel mult")

    # 3. SUPPORT WALLS L1 — P-1 B&R, 10ft
    # Togal: 1,475 LF / 1.3 overlap x 10ft x 0.85 = 9,648 SF - 680 platform
    add_paint_item("SUPPORT WALLS L1 - P-1 B&R (10ft)", 8968, "SF",
                   "B&R", 150, 2, "Togal L1 rooms adj perim x 10ft")

    # 4. PLATFORM WALLS — P-3 Black accent
    add_paint_item("PLATFORM WALLS - P-3 Black B&R", 680, "SF",
                   "B&R", 125, 3, "Dark color extra coat")

    # 5. LOBBY WALLS — P-2
    add_paint_item("LOBBY WALLS - P-2 B&R (10ft)", 800, "SF",
                   "B&R", 150, 2, "Togal lobby region")

    # 6. GWB CEILINGS L1 — P-2 spray
    add_paint_item("GWB CEILINGS L1 - P-2 spray", 4200, "SF",
                   "spray", 300, 2, "P-2 rooms excl ACT & gym")

    # 7. SUPPORT WALLS L2 — P-1 B&R, 10ft
    add_paint_item("SUPPORT WALLS L2 - P-1 B&R (10ft)", 12740, "SF",
                   "B&R", 150, 2, "Togal L2 rooms adj perim x 10ft")

    # 8. TEACHER WORKROOM — P-7 Gray
    add_paint_item("TEACHER WORKROOM - P-7 Gray B&R", 595, "SF",
                   "B&R", 140, 2, "Room 224 accent")

    # 9. GWB CEILINGS L2 — P-2 spray
    add_paint_item("GWB CEILINGS L2 - P-2 spray", 3500, "SF",
                   "spray", 300, 2, "GWB rooms L2 excl ACT")

    # 10. HM DOORS — 20 doors, both sides
    add_fixed_item("HM DOORS - 1P+2F both sides (20 doors)", 20, "doors",
                   labor_hrs=20, labor_cost=20 * BURDENED_RATE,
                   material_cost=160, note="0.5 hr/side x 2 sides")

    # 11. HM FRAMES
    add_fixed_item("HM FRAMES - 1P+2F (20 frames)", 20, "frames",
                   labor_hrs=6.6, labor_cost=6.6 * BURDENED_RATE,
                   material_cost=80, note="0.33 hr/frame")

    # 12. EXTERIOR MISC STEEL
    add_fixed_item("EXTERIOR MISC - handrails, bollards, ladder", 1, "ALLOW",
                   labor_hrs=16, labor_cost=16 * BURDENED_RATE,
                   material_cost=200, note="16 hr allowance")

    # 13. EQUIPMENT
    add_fixed_item("EQUIPMENT - scissor lift + sprayer rental", 1, "ALLOW",
                   labor_hrs=0, labor_cost=0,
                   material_cost=1800, note="Scissor lift 2wk + airless")

    # 14. MOBILIZATION
    add_fixed_item("MOBILIZATION / DEMOBILIZATION", 1, "ALLOW",
                   labor_hrs=0, labor_cost=0,
                   material_cost=600, note="Travel, setup, protection")

    # === SUMMARY ===
    total_labor_hrs = sum(i["labor_hrs"] for i in line_items)
    total_labor = sum(i["labor_cost"] for i in line_items)
    total_material = sum(i["material_cost"] for i in line_items)
    total_direct = total_labor + total_material
    overhead = total_direct * OH_PCT
    subtotal = total_direct + overhead
    markup = subtotal * MARKUP_PCT
    bid_price = subtotal + markup
    total_paint_sf = sum(i["qty"] for i in line_items if i["unit"] == "SF")

    # Print table
    hdr = f"{'LINE ITEM':<55} {'QTY':>6} {'UNIT':>5} {'LABOR':>8} {'MAT':>7} {'TOTAL':>8}"
    print(f"\n{hdr}")
    print("-" * 95)
    for item in line_items:
        print(f"{item['desc']:<55} {item['qty']:>6,} {item['unit']:>5} "
              f"${item['labor_cost']:>7,.0f} ${item['material_cost']:>6,.0f} ${item['direct_cost']:>7,.0f}")
    print("-" * 95)

    print(f"\n{'COST SUMMARY':=^60}")
    print(f"  Total Paint SF:        {total_paint_sf:>10,} SF")
    print(f"  Total Labor Hours:     {total_labor_hrs:>10.1f} hrs ({total_labor_hrs/8:.1f} painter-days)")
    print(f"  Labor Cost:            ${total_labor:>10,.0f}")
    print(f"  Material Cost:         ${total_material:>10,.0f}")
    print(f"  Equipment + Mob:                ${1800+600:>6,}")
    print(f"  {'':->40}")
    print(f"  DIRECT COST:           ${total_direct:>10,.0f}")
    print(f"  Overhead (12%):        ${overhead:>10,.0f}")
    print(f"  {'':->40}")
    print(f"  SUBTOTAL:              ${subtotal:>10,.0f}")
    print(f"  Markup (18%):          ${markup:>10,.0f}")
    print(f"  {'':=<40}")
    print(f"  BID PRICE:             ${bid_price:>10,.0f}")
    print(f"  $/SF (paint area):     ${bid_price/total_paint_sf:>10.2f}")
    print(f"  $/SF (building ~20K):  ${bid_price/20000:>10.2f}")

    # Duration estimate
    crew_size = 3
    duration_days = total_labor_hrs / 8 / crew_size
    print(f"\n  Est. Duration: {duration_days:.0f} working days (3-man crew)")

    # Save
    estimate = {
        "project": "Hopewell Elementary Phase 2 Gym Addition",
        "date": "2026-04-11",
        "tier": "TARGET",
        "method": "Togal AI Takeoff + CCF Estimate Engine",
        "labor_rate": BURDENED_RATE,
        "oh_pct": OH_PCT,
        "markup_pct": MARKUP_PCT,
        "line_items": line_items,
        "summary": {
            "total_paint_sf": total_paint_sf,
            "total_labor_hrs": round(total_labor_hrs, 1),
            "painter_days": round(total_labor_hrs / 8, 1),
            "crew_size": crew_size,
            "duration_days": round(duration_days, 0),
            "labor_cost": round(total_labor, 0),
            "material_cost": round(total_material, 0),
            "equipment": 1800,
            "mobilization": 600,
            "direct_cost": round(total_direct, 0),
            "overhead": round(overhead, 0),
            "subtotal": round(subtotal, 0),
            "markup": round(markup, 0),
            "bid_price": round(bid_price, 0),
            "per_sf_paint": round(bid_price / total_paint_sf, 2),
            "per_sf_building": round(bid_price / 20000, 2),
        },
        "exclusions": [
            "ACT grid/tile (factory finish)",
            "EIFS exterior", "Brick veneer", "ACM panels",
            "Storefront glazing", "Athletic wall pads",
            "Acoustic wall panels (AWP-1 thru AWP-4)",
            "Floor paint/epoxy", "Wood staining (09 93 00)",
            "Fire-rated caulking", "Sports floor finish",
        ]
    }
    out = PROJECT_DIR / "estimate_target.json"
    out.write_text(json.dumps(estimate, indent=2))
    print(f"\nSaved: {out}")

if __name__ == "__main__":
    main()
