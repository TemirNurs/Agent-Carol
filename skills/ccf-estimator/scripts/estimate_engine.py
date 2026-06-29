#!/usr/bin/env python3
"""
CCF Estimate Engine — Pure Deterministic Calculation
No LLM calls. Takes takeoff items + pricing config → returns estimate.

Usage:
  python estimate_engine.py --takeoff takeoff.json --pricing pricing.json --tier target --overhead 0.12 --markup 0.20
  python estimate_engine.py --takeoff takeoff.json --pricing pricing.json --tier floor --scenario "New GC"
"""

import argparse
import json
import sys
import math
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Optional


# --- Pricing tier resolution ---

# Illustrative defaults — real bands live in the gitignored pricing config
# (loaded via pricing_config at runtime); these are generic stand-ins so the
# public source ships no proprietary cost structure.
MARKUP_DEFAULTS = {
    "New GC – Must-Win Bid": (0.15, 0.20),
    "Existing GC – Competitive Bid": (0.20, 0.25),
    "Repeat GC – Invited Bid": (0.25, 0.35),
    "Sole Source / Negotiated": (0.30, 0.40),
    "Large Project (>$200K)": (0.15, 0.20),
    "Small Project (<$25K)": (0.30, 0.40),
}


def resolve_tier_price(unit_price_entry, tier):
    """Get the price for a tier from a unit_prices entry.
    tier: 'floor', 'target', 'premium'
    Returns the midpoint of the range."""
    key = f"{tier}_price"
    val = unit_price_entry.get(key)
    if val is None:
        return None
    if isinstance(val, (list, tuple)) and len(val) == 2:
        return (val[0] + val[1]) / 2
    return float(val)


def resolve_markup(scenario_name, markup_scenarios):
    """Get markup % for a scenario. Returns midpoint of range or markup_pct."""
    for s in markup_scenarios:
        name = s.get("scenario") or s.get("name", "")
        if scenario_name.lower() in name.lower():
            # Check for direct markup_pct first
            if "markup_pct" in s:
                return float(s["markup_pct"])
            r = s.get("markup_range")
            if r and isinstance(r, (list, tuple)):
                return (r[0] + r[1]) / 2
            return 0.20
    return 0.20


# --- Core calculation functions ---

def calc_labor_from_rate(quantity, prod_rate_sf_per_hr, labor_rate_per_hr, coats=1):
    """Calculate labor hours and cost from production rate.
    quantity: total SF/LF/EA
    prod_rate_sf_per_hr: SF/hr per painter
    labor_rate_per_hr: burdened $/hr
    coats: number of coats
    """
    if prod_rate_sf_per_hr <= 0:
        return {"hours": 0, "cost": 0}
    hours = (quantity * coats) / prod_rate_sf_per_hr
    cost = hours * labor_rate_per_hr
    return {"hours": round(hours, 2), "cost": round(cost, 2)}


def calc_labor_from_hrs_per_unit(quantity, hrs_per_unit, labor_rate_per_hr):
    """For items measured in hrs/door, hrs/frame, etc."""
    hours = quantity * hrs_per_unit
    cost = hours * labor_rate_per_hr
    return {"hours": round(hours, 2), "cost": round(cost, 2)}


def calc_material_from_cost_per_sf(quantity, cost_per_sf, coats=1):
    """Material cost = quantity * coats * $/SF."""
    cost = quantity * coats * cost_per_sf
    return {"cost": round(cost, 2)}


def calc_material_from_cost_per_unit(quantity, cost_per_unit):
    """Material cost = quantity * $/unit (for doors, frames, etc.)."""
    cost = quantity * cost_per_unit
    return {"cost": round(cost, 2)}


def calc_material_from_coverage(quantity, coverage_sf_per_gal, price_per_gal, coats=1):
    """Material cost using coverage rate.
    quantity: total SF
    coverage_sf_per_gal: SF per gallon
    price_per_gal: $ per gallon
    coats: number of coats
    """
    if coverage_sf_per_gal <= 0:
        return {"gallons": 0, "cost": 0}
    gallons = (quantity * coats) / coverage_sf_per_gal
    cost = gallons * price_per_gal
    return {"gallons": round(gallons, 2), "cost": round(cost, 2)}


def calc_line_item(item, pricing_config, tier="target", labor_rate=28.0):
    """Calculate a single estimate line item.

    item: dict with keys: area, task, task_code, quantity, unit, method,
          coats, prod_rate (optional override), material_cost_per_sf (optional),
          material_cost_per_unit (optional), material_cost_flat (optional),
          labor_hrs_override (optional)
    pricing_config: output from read_excel.py
    tier: floor/target/premium
    labor_rate: burdened hourly rate
    """
    quantity = float(item.get("quantity", 0))
    coats = int(item.get("coats", 1))
    unit = str(item.get("unit", "SF")).upper()

    # --- Labor ---
    prod_rate = item.get("prod_rate")
    labor_hrs_override = item.get("labor_hrs_override")
    if labor_hrs_override is not None:
        prod_rate = None  # Not used when hours are overridden
        labor = {"hours": float(labor_hrs_override), "cost": round(float(labor_hrs_override) * labor_rate, 2)}
    else:
        if prod_rate:
            prod_rate = float(prod_rate)
        else:
            prod_rate = _lookup_prod_rate(item, pricing_config, tier)

        hrs_per_unit = item.get("hrs_per_unit")
        if hrs_per_unit is not None:
            labor = calc_labor_from_hrs_per_unit(quantity, float(hrs_per_unit), labor_rate)
        elif prod_rate and prod_rate > 0:
            labor = calc_labor_from_rate(quantity, prod_rate, labor_rate, coats)
        else:
            labor = {"hours": 0, "cost": 0}

    # --- Material ---
    mat_flat = item.get("material_cost_flat")
    mat_per_sf = item.get("material_cost_per_sf")
    mat_per_unit = item.get("material_cost_per_unit")
    mat_coverage = item.get("material_coverage_sf_per_gal")
    mat_price_gal = item.get("material_price_per_gal")

    if mat_flat is not None:
        material = {"cost": round(float(mat_flat), 2)}
    elif mat_per_sf is not None:
        material = calc_material_from_cost_per_sf(quantity, float(mat_per_sf), coats)
    elif mat_per_unit is not None:
        material = calc_material_from_cost_per_unit(quantity, float(mat_per_unit))
    elif mat_coverage and mat_price_gal:
        material = calc_material_from_coverage(quantity, float(mat_coverage), float(mat_price_gal), coats)
    else:
        material = {"cost": 0}

    # --- Equipment (optional flat cost) ---
    equipment_cost = float(item.get("equipment_cost", 0))

    subtotal = labor["cost"] + material["cost"] + equipment_cost

    return {
        "area": item.get("area", ""),
        "task": item.get("task", ""),
        "task_code": item.get("task_code", ""),
        "quantity": quantity,
        "unit": unit,
        "coats": coats,
        "method": item.get("method", ""),
        "paint_system": item.get("paint_system", ""),
        "prod_rate": prod_rate if not labor_hrs_override else None,
        "labor_hours": labor["hours"],
        "labor_cost": labor["cost"],
        "material_cost": material["cost"],
        "equipment_cost": equipment_cost,
        "subtotal": round(subtotal, 2),
        "notes": item.get("notes", ""),
    }


def _lookup_prod_rate(item, pricing_config, tier):
    """Look up production rate from pricing config.
    Uses avg_rate for 'target', slow_rate for 'floor', fast_rate for 'premium'."""
    rates = pricing_config.get("production_rates", {})
    task_name = str(item.get("task", "")).lower().strip()

    tier_key = {"floor": "slow_rate", "target": "avg_rate", "premium": "fast_rate"}.get(tier, "avg_rate")

    for section in ["painting", "prep"]:
        for rate_entry in rates.get(section, []):
            entry_task = str(rate_entry.get("task", "")).lower().strip()
            if entry_task in task_name or task_name in entry_task:
                val = rate_entry.get(tier_key)
                if val is not None:
                    return float(val)
    return None


def build_estimate(takeoff_items, pricing_config, tier="target",
                   labor_rate=28.0, overhead_pct=0.12, markup_pct=0.20,
                   project_name="", gc="", bid_date=""):
    """Build a complete estimate from takeoff items.

    takeoff_items: list of dicts (from takeoff file or manual entry)
    pricing_config: output from read_excel.py
    tier: floor/target/premium
    labor_rate: burdened hourly rate (illustrative default — real bands live in
        the gitignored pricing config)
    overhead_pct: overhead percentage (illustrative default)
    markup_pct: markup on (direct + overhead)
    """
    line_items = []
    for item in takeoff_items:
        li = calc_line_item(item, pricing_config, tier, labor_rate)
        line_items.append(li)

    # Category subtotals
    categories = {}
    for li in line_items:
        cat = li.get("area", "Uncategorized")
        if cat not in categories:
            categories[cat] = {"labor_hours": 0, "labor_cost": 0, "material_cost": 0, "equipment_cost": 0, "subtotal": 0}
        categories[cat]["labor_hours"] += li["labor_hours"]
        categories[cat]["labor_cost"] += li["labor_cost"]
        categories[cat]["material_cost"] += li["material_cost"]
        categories[cat]["equipment_cost"] += li["equipment_cost"]
        categories[cat]["subtotal"] += li["subtotal"]

    # Round category totals
    for cat in categories:
        for k in categories[cat]:
            categories[cat][k] = round(categories[cat][k], 2)

    # Totals
    labor_total = round(sum(li["labor_cost"] for li in line_items), 2)
    material_total = round(sum(li["material_cost"] for li in line_items), 2)
    equipment_total = round(sum(li["equipment_cost"] for li in line_items), 2)
    labor_hours_total = round(sum(li["labor_hours"] for li in line_items), 2)

    direct_cost = round(labor_total + material_total + equipment_total, 2)
    overhead = round(direct_cost * overhead_pct, 2)
    subtotal_before_markup = round(direct_cost + overhead, 2)
    markup = round(subtotal_before_markup * markup_pct, 2)
    bid_price = round(subtotal_before_markup + markup, 2)

    # Metrics
    total_sf = sum(li["quantity"] for li in line_items if li["unit"] in ("SF", "SF/HR"))
    blended_rate = round(bid_price / total_sf, 2) if total_sf > 0 else 0

    estimate = {
        "project": {
            "name": project_name,
            "gc": gc,
            "bid_date": bid_date,
            "tier": tier,
            "labor_rate": labor_rate,
            "overhead_pct": overhead_pct,
            "markup_pct": markup_pct,
        },
        "line_items": line_items,
        "category_subtotals": categories,
        "totals": {
            "labor_hours": labor_hours_total,
            "labor_cost": labor_total,
            "material_cost": material_total,
            "equipment_cost": equipment_total,
            "direct_cost": direct_cost,
            "overhead": overhead,
            "subtotal_before_markup": subtotal_before_markup,
            "markup": markup,
            "bid_price": bid_price,
        },
        "metrics": {
            "total_sf": round(total_sf, 0),
            "blended_rate_per_sf": blended_rate,
            "crew_days_2_man": math.ceil(labor_hours_total / (2 * 8)) if labor_hours_total > 0 else 0,
            "crew_days_3_man": math.ceil(labor_hours_total / (3 * 8)) if labor_hours_total > 0 else 0,
        },
    }

    return estimate


# ---------------------------------------------------------------------------
# Facility-type adjustments
# ---------------------------------------------------------------------------

FACILITY_PATTERNS_FILE = Path(__file__).resolve().parent.parent.parent.parent / "data" / "memory" / "facility_patterns.json"
PIPELINE_FILE = Path(__file__).resolve().parent.parent.parent.parent / "data" / "memory" / "pipeline.json"


def load_facility_patterns() -> dict:
    """Load facility-type adjustment patterns."""
    if FACILITY_PATTERNS_FILE.exists():
        return json.loads(FACILITY_PATTERNS_FILE.read_text(encoding="utf-8"))
    return {}


def apply_facility_adjustments(line_items: list, facility_type: str,
                               patterns: dict = None) -> list:
    """Apply facility-type-specific adjustments to line items.

    Adjusts quantities and adds notes based on building type patterns.
    For example, K-12 gyms get 35% wall deductions for pads/scoreboards,
    and PEMB ceilings get 1.3x steel multiplier.

    Returns modified line_items list with adjustment notes.
    """
    if patterns is None:
        patterns = load_facility_patterns()

    ftype = facility_type.lower().replace("-", "_").replace(" ", "_")
    fp = patterns.get(ftype)
    if not fp:
        return line_items  # No adjustments for unknown types

    adjustments = fp.get("adjustments", {})
    adjusted = []

    for item in line_items:
        item = dict(item)  # Don't mutate original
        task = item.get("task_code", "").lower()
        area = item.get("area", "").lower()

        # Wall deductions (for gyms with pads, scoreboards, etc.)
        wall_deduction = adjustments.get("wall_deduction_pct", 0)
        if wall_deduction and ("wall" in task or "wall" in area):
            orig_qty = item["quantity"]
            item["quantity"] = round(orig_qty * (1 - wall_deduction), 0)
            item["notes"] = (item.get("notes", "") +
                             f" | {facility_type} wall deduction {wall_deduction:.0%} "
                             f"(orig {orig_qty:.0f} SF)").strip(" |")

        # Ceiling multiplier (for PEMB exposed structure)
        ceiling_mult = adjustments.get("ceiling_structure_multiplier", 0)
        if ceiling_mult and ("ceiling" in task or "ceiling" in area):
            orig_qty = item["quantity"]
            item["quantity"] = round(orig_qty * ceiling_mult, 0)
            item["notes"] = (item.get("notes", "") +
                             f" | {facility_type} structure mult {ceiling_mult}x "
                             f"(orig {orig_qty:.0f} SF)").strip(" |")

        # Extra coats for dark colors or antimicrobial
        extra_coats = adjustments.get("extra_coats", {})
        for pattern, add_coats in extra_coats.items():
            if pattern in task or pattern in area:
                item["coats"] = item.get("coats", 2) + add_coats
                item["notes"] = (item.get("notes", "") +
                                 f" | +{add_coats} coat ({pattern})").strip(" |")

        # Prod rate adjustment (slower for heights, preparation, etc.)
        rate_modifier = adjustments.get("prod_rate_modifier", 1.0)
        if rate_modifier != 1.0 and item.get("prod_rate"):
            item["prod_rate"] = round(item["prod_rate"] * rate_modifier, 0)
            item["notes"] = (item.get("notes", "") +
                             f" | rate adj {rate_modifier}x ({facility_type})").strip(" |")

        adjusted.append(item)

    return adjusted


def calibrate_from_history(estimate: dict, facility_type: str = "",
                           gc_name: str = "") -> dict:
    """Compare estimate against historical bids for calibration.

    Reads completed projects from pipeline.json to find similar past bids.
    Returns calibration data: confidence score, historical avg $/SF, variance.
    """
    calibration = {
        "confidence": "low",
        "historical_matches": 0,
        "avg_historical_per_sf": None,
        "current_per_sf": estimate.get("metrics", {}).get("blended_rate_per_sf", 0),
        "variance_pct": None,
        "notes": [],
    }

    if not PIPELINE_FILE.exists():
        calibration["notes"].append("No pipeline history available")
        return calibration

    pipeline = json.loads(PIPELINE_FILE.read_text(encoding="utf-8"))
    projects = pipeline.get("projects", {})

    # Find completed bids with known outcomes
    historical = []
    for slug, proj in projects.items():
        if proj.get("outcome") in ("won", "lost") and proj.get("bid_amount"):
            historical.append(proj)

    if not historical:
        calibration["notes"].append("No completed bids to calibrate against")
        return calibration

    # Filter by facility type if available
    matches = historical
    if facility_type:
        ft_lower = facility_type.lower()
        type_matches = [p for p in historical
                        if ft_lower in p.get("name", "").lower()
                        or ft_lower in p.get("notes", "").lower()]
        if type_matches:
            matches = type_matches
            calibration["notes"].append(f"Matched {len(type_matches)} {facility_type} bids")

    # Filter by GC if available
    if gc_name:
        gc_matches = [p for p in matches
                      if gc_name.lower() in p.get("gc", "").lower()]
        if gc_matches:
            matches = gc_matches
            calibration["notes"].append(f"Matched {len(gc_matches)} bids with GC '{gc_name}'")

    calibration["historical_matches"] = len(matches)

    # Confidence scoring
    if len(matches) >= 5:
        calibration["confidence"] = "high"
    elif len(matches) >= 2:
        calibration["confidence"] = "medium"
    else:
        calibration["confidence"] = "low"

    # Calculate historical average $/SF (rough — uses bid_amount / 20000 as proxy)
    # TODO: store actual SF with completed bids for better calibration
    if matches and calibration["current_per_sf"] is not None:
        # Use bid amounts as-is for comparison
        avg_bid = sum(p["bid_amount"] for p in matches) / len(matches)
        calibration["avg_historical_bid"] = round(avg_bid, 0)
        calibration["current_bid"] = estimate.get("totals", {}).get("bid_price", 0)

        if avg_bid > 0:
            variance = (calibration["current_bid"] - avg_bid) / avg_bid
            calibration["variance_pct"] = round(variance * 100, 1)

            if abs(variance) > 0.15:
                calibration["notes"].append(
                    f"WARNING: Current bid is {variance:+.0%} vs historical avg "
                    f"(${avg_bid:,.0f}). Review line items."
                )

    # Win rate for this GC
    if gc_name:
        gc_bids = [p for p in historical if gc_name.lower() in p.get("gc", "").lower()]
        wins = sum(1 for p in gc_bids if p.get("outcome") == "won")
        if gc_bids:
            calibration["gc_win_rate"] = round(wins / len(gc_bids) * 100, 0)
            calibration["notes"].append(
                f"GC win rate: {wins}/{len(gc_bids)} ({calibration['gc_win_rate']:.0f}%)"
            )

    return calibration


def get_confidence_score(facility_type: str = "", gc_name: str = "") -> str:
    """Quick confidence check: high/medium/low based on historical data."""
    if not PIPELINE_FILE.exists():
        return "low"

    pipeline = json.loads(PIPELINE_FILE.read_text(encoding="utf-8"))
    completed = [p for p in pipeline.get("projects", {}).values()
                 if p.get("outcome") in ("won", "lost") and p.get("bid_amount")]

    if facility_type:
        ft = facility_type.lower()
        completed = [p for p in completed
                     if ft in p.get("name", "").lower() or ft in p.get("notes", "").lower()]

    if len(completed) >= 5:
        return "high"
    elif len(completed) >= 2:
        return "medium"
    return "low"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="CCF Estimate Engine")
    parser.add_argument("--takeoff", required=True, help="Path to takeoff JSON file")
    parser.add_argument("--pricing", required=True, help="Path to pricing config JSON file")
    parser.add_argument("--tier", default="target", choices=["floor", "target", "premium"])
    parser.add_argument("--labor-rate", type=float, default=28.0, help="Burdened labor rate $/hr")
    parser.add_argument("--overhead", type=float, default=0.12, help="Overhead percentage")
    parser.add_argument("--markup", type=float, default=0.20, help="Markup percentage")
    parser.add_argument("--scenario", default=None, help="Markup scenario name (overrides --markup)")
    parser.add_argument("--project-name", default="", help="Project name")
    parser.add_argument("--gc", default="", help="General contractor")
    parser.add_argument("--bid-date", default="", help="Bid date")
    parser.add_argument("--facility-type", default="", help="Facility type for adjustments (k12_gym, medical, retail, office)")
    args = parser.parse_args()

    with open(args.takeoff) as f:
        takeoff_data = json.load(f)
    # Support both flat array and {takeoff_items: [...]} format
    if isinstance(takeoff_data, list):
        takeoff_items = takeoff_data
    else:
        takeoff_items = takeoff_data.get("takeoff_items", takeoff_data)

    with open(args.pricing) as f:
        pricing_config = json.load(f)

    markup = args.markup
    if args.scenario:
        markup_scenarios = pricing_config.get("pricing_policy", {}).get("markup_scenarios", [])
        markup = resolve_markup(args.scenario, markup_scenarios)

    # Apply facility-type adjustments before building estimate
    if args.facility_type:
        takeoff_items = apply_facility_adjustments(takeoff_items, args.facility_type)

    estimate = build_estimate(
        takeoff_items=takeoff_items,
        pricing_config=pricing_config,
        tier=args.tier,
        labor_rate=args.labor_rate,
        overhead_pct=args.overhead,
        markup_pct=markup,
        project_name=args.project_name,
        gc=args.gc,
        bid_date=args.bid_date,
    )

    # Add calibration if facility type or GC provided
    if args.facility_type or args.gc:
        estimate["calibration"] = calibrate_from_history(
            estimate, facility_type=args.facility_type, gc_name=args.gc)

    print(json.dumps(estimate, indent=2))


if __name__ == "__main__":
    main()
