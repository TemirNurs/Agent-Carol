#!/usr/bin/env python3
"""Update facility type memory files with real CRM data."""
import json
from pathlib import Path

ft_dir = Path(r"C:\Users\Nursm\Agent Carol\data\memory\facility_types")
ft_dir.mkdir(parents=True, exist_ok=True)

facility_data = {
    "grocery_food_lion": {
        "type": "grocery_food_lion",
        "completed_count": 14,
        "avg_contract_value": 31000,
        "total_revenue": 434000,
        "avg_project_size_sf": 30000,
        "typical_scope": [
            "interior_walls_sales_floor", "back_of_house_rooms", "ceilings",
            "doors_frames", "columns", "wood_trim_millwork",
            "cooler_freezer_panels", "exterior_guard_posts",
            "exterior_metal", "anti_rodent_coating", "wallcovering"
        ],
        "common_exclusions": ["ACT_replacement", "floor_finishes", "decor_signage", "fire_lane_painting"],
        "typical_paint_systems": {
            "sales_floor_walls": "ProBlock Oil Primer + SW Pure White Eg-Shel",
            "sales_floor_columns": "SW Pure White Semi-Gloss Epoxy",
            "back_of_house": "SW Tony Taupe Epoxy / SW Softer Tan Low-Gloss",
            "doors_frames": "SW Grizzle Gray Semi-Gloss",
            "cooler_panels": "SW Softer Tan Electrostatic",
            "exterior": "Traffic Yellow guard posts + Acrolon 100 dock walls"
        },
        "typical_conditions": ["active_store_night_work", "phased_remodel", "existing_finishes_to_remain"],
        "avg_bid_price_per_sf": 0.95,
        "key_gcs": ["Parkway Construction", "Rick Shipman", "WED Construction", "WIMCO"],
        "pricing_notes": "Consistent $25-40K range. WED bids need AGGRESSIVE tier (we lose by 10-15%). Rick Shipman and Parkway are at TARGET tier. Always verify finish schedule with FL Construction Manager.",
        "last_updated": "2026-04-02"
    },
    "retail_boot_barn": {
        "type": "retail_boot_barn",
        "completed_count": 6,
        "avg_contract_value": 20000,
        "total_revenue": 120000,
        "avg_project_size_sf": 15000,
        "typical_scope": [
            "interior_walls", "ceilings", "doors_frames", "accent_walls", "restrooms"
        ],
        "common_exclusions": ["exterior", "wallcovering", "specialty_finishes"],
        "typical_paint_systems": {
            "sales_floor": "Standard latex flat/eggshell",
            "accent_walls": "Per brand color spec",
            "restrooms": "Semi-gloss latex"
        },
        "typical_conditions": ["new_construction_or_upfit", "standard_schedule"],
        "avg_bid_price_per_sf": 1.33,
        "key_gcs": ["Parkway Construction"],
        "pricing_notes": "Straightforward retail upfit. $15-25K range. Good margin work with Parkway.",
        "last_updated": "2026-04-02"
    },
    "retail_target": {
        "type": "retail_target",
        "completed_count": 2,
        "avg_contract_value": 193000,
        "total_revenue": 386000,
        "avg_project_size_sf": 100000,
        "typical_scope": [
            "interior_walls", "ceilings", "doors_frames", "columns",
            "back_of_house", "restrooms", "exterior_touch_up"
        ],
        "common_exclusions": ["flooring", "fixtures", "signage"],
        "typical_paint_systems": {
            "sales_floor": "Per Target brand spec",
            "back_of_house": "Standard commercial epoxy/latex"
        },
        "typical_conditions": ["active_store_night_work", "tight_schedule", "large_square_footage"],
        "avg_bid_price_per_sf": 1.93,
        "key_gcs": ["LF Jennings", "Williams Company"],
        "pricing_notes": "HIGH VALUE work ($193K avg). Lost Lincolnton by 25% with LF Jennings, lost Pineville by 7% with Williams. Need to tighten production rates. Worth pursuing aggressively.",
        "last_updated": "2026-04-02"
    },
    "cinema": {
        "type": "cinema",
        "completed_count": 3,
        "avg_contract_value": 72000,
        "total_revenue": 216000,
        "avg_project_size_sf": 40000,
        "typical_scope": [
            "auditorium_walls", "lobbies", "concession_areas",
            "restrooms", "corridors", "specialty_dark_paint"
        ],
        "common_exclusions": ["screen_walls", "acoustic_panels", "flooring"],
        "typical_paint_systems": {
            "auditoriums": "Dark flat latex (theater black/dark colors)",
            "lobbies": "Brand colors per spec",
            "restrooms": "Semi-gloss epoxy"
        },
        "typical_conditions": ["new_construction", "high_ceilings", "specialty_colors"],
        "avg_bid_price_per_sf": 1.80,
        "key_gcs": ["Parkway Construction"],
        "pricing_notes": "Good margin work. Cinemark and Stone Theatres. High ceilings add labor. $49-116K range.",
        "last_updated": "2026-04-02"
    },
    "senior_living": {
        "type": "senior_living",
        "completed_count": 2,
        "avg_contract_value": 120000,
        "total_revenue": 240000,
        "avg_project_size_sf": 60000,
        "typical_scope": [
            "unit_interiors", "common_areas", "corridors",
            "dining", "restrooms", "exterior"
        ],
        "common_exclusions": ["wallcovering_by_others", "specialty_coatings"],
        "typical_paint_systems": {
            "units": "Standard latex eggshell",
            "common_areas": "Durable latex semi-gloss",
            "corridors": "Scuff-resistant semi-gloss"
        },
        "typical_conditions": ["phased_occupancy", "multiple_units", "long_schedule"],
        "avg_bid_price_per_sf": 2.00,
        "key_gcs": ["Parkway Construction"],
        "pricing_notes": "High value, long duration. Venture IV was $148K. Good profit margin. Multiple unit repetition helps productivity.",
        "last_updated": "2026-04-02"
    },
    "hotel": {
        "type": "hotel",
        "completed_count": 0,
        "avg_contract_value": 0,
        "total_revenue": 0,
        "avg_project_size_sf": 50000,
        "typical_scope": [
            "guest_rooms", "lobbies", "corridors", "restrooms",
            "back_of_house", "exterior"
        ],
        "common_exclusions": ["wallcovering_by_others", "specialty_finishes"],
        "typical_paint_systems": {
            "guest_rooms": "Standard latex eggshell",
            "lobbies": "Premium finish per brand spec",
            "corridors": "Durable semi-gloss"
        },
        "typical_conditions": ["brand_spec_colors", "repetitive_rooms", "tight_schedule"],
        "avg_bid_price_per_sf": 0,
        "key_gcs": ["Parkway Construction", "Path Construction"],
        "pricing_notes": "WARNING: Lost Hyatt Place bids at DOUBLE market rate. Our hotel production rates are way off. Need to recalibrate before bidding hotel work. Towneplace Suites and Home2 Suites in current pipeline - price VERY aggressively.",
        "last_updated": "2026-04-02"
    },
    "amenity_center": {
        "type": "amenity_center",
        "completed_count": 3,
        "avg_contract_value": 86000,
        "total_revenue": 258000,
        "avg_project_size_sf": 15000,
        "typical_scope": [
            "interior_walls", "ceilings", "doors_frames",
            "restrooms", "pool_area", "exterior"
        ],
        "common_exclusions": ["pool_coatings", "specialty_waterproofing"],
        "typical_paint_systems": {
            "interior": "Premium latex eggshell/semi-gloss",
            "pool_area": "Moisture-resistant coatings",
            "exterior": "Elastomeric or premium exterior latex"
        },
        "typical_conditions": ["high_end_finish", "travel_required_TX"],
        "avg_bid_price_per_sf": 5.73,
        "key_gcs": ["DL Meacham Construction"],
        "pricing_notes": "TX travel work with DL Meacham. $75-95K range. Competitive within 4-5hr drive. Travel costs make us uncompetitive beyond that.",
        "last_updated": "2026-04-02"
    },
    "restaurant": {
        "type": "restaurant",
        "completed_count": 3,
        "avg_contract_value": 18000,
        "total_revenue": 54000,
        "avg_project_size_sf": 3500,
        "typical_scope": [
            "interior_walls", "ceilings", "restrooms", "kitchen_area", "exterior"
        ],
        "common_exclusions": ["kitchen_epoxy_floors", "hood_systems"],
        "typical_paint_systems": {
            "dining": "Latex eggshell per brand spec",
            "kitchen": "Semi-gloss or epoxy",
            "restrooms": "Semi-gloss latex"
        },
        "typical_conditions": ["small_fast_turnaround", "brand_specific_colors"],
        "avg_bid_price_per_sf": 5.14,
        "key_gcs": ["Parkway Construction", "Benchmark Building Solutions", "Integrity Construction"],
        "pricing_notes": "Quick jobs, good for filling schedule gaps. Fuzzy's Taco, Bojangles, Eggs Up Grill, Dutch Bros in pipeline.",
        "last_updated": "2026-04-02"
    },
    "government_military": {
        "type": "government_military",
        "completed_count": 1,
        "avg_contract_value": 45000,
        "total_revenue": 45000,
        "avg_project_size_sf": 20000,
        "typical_scope": [
            "interior_walls", "ceilings", "doors_frames", "exterior", "specialty_coatings"
        ],
        "common_exclusions": ["HAZMAT_abatement", "lead_paint_removal"],
        "typical_paint_systems": {
            "interior": "Per UFGS spec",
            "exterior": "Per UFGS spec"
        },
        "typical_conditions": ["prevailing_wage", "security_clearance", "long_procurement"],
        "avg_bid_price_per_sf": 2.25,
        "key_gcs": ["CMC Building", "Sauer Construction", "ECC", "VALIANT Construction"],
        "pricing_notes": "Government work often prevailing wage - factor into labor rates. Camp Butner, Fort Bragg, VA Medical Centers in pipeline. Good steady work if we win.",
        "last_updated": "2026-04-02"
    },
    "office": {
        "type": "office",
        "completed_count": 4,
        "avg_contract_value": 42000,
        "total_revenue": 168000,
        "avg_project_size_sf": 25000,
        "typical_scope": [
            "interior_walls", "ceilings", "doors_frames", "restrooms", "corridors"
        ],
        "common_exclusions": ["flooring", "ceiling_grid", "window_treatments"],
        "typical_paint_systems": {
            "offices": "Latex eggshell",
            "corridors": "Latex semi-gloss",
            "restrooms": "Semi-gloss epoxy or latex"
        },
        "typical_conditions": ["occupied_building", "phased_work", "after_hours"],
        "avg_bid_price_per_sf": 1.68,
        "key_gcs": ["Hendrick Construction", "various"],
        "pricing_notes": "Standard commercial work. Duke Health losses were $100K above lowest - our medical/office rates need recalibrating for large facilities.",
        "last_updated": "2026-04-02"
    }
}

for slug, data in facility_data.items():
    filepath = ft_dir / f"{slug}.json"
    with open(filepath, "w") as f:
        json.dump(data, f, indent=2)
    print(f"Saved: {filepath.name}")

print(f"\nDone. {len(facility_data)} facility type files updated.")
