#!/usr/bin/env python3
"""Update GC memory files with real CRM data."""
import json
from pathlib import Path

gc_dir = Path(r"C:\Users\Nursm\Agent Carol\data\memory\gc")
gc_dir.mkdir(parents=True, exist_ok=True)

gc_data = {
    "parkway-construction": {
        "name": "Parkway Construction",
        "contacts": [
            {"name": "Jim Hinton", "role": "Estimator/PM", "email": "jim@parkwayconstruction.com"},
            {"name": "Matthew Hinton", "role": "PM", "email": "matthew@parkwayconstruction.com"}
        ],
        "relationship": "primary",
        "projects_bid": 42,
        "projects_won": 35,
        "win_rate": 0.833,
        "avg_bid_amount": 91400,
        "total_revenue": 3200000,
        "pricing_notes": "Strongest relationship. 35 wins across retail, senior living, cinema, restaurant, industrial. Recent Hyatt bids came in DOUBLE market rate - lost both. Food Lion wins consistent at $25-40K. Boot Barn wins $15-25K. Senior living $50-150K range.",
        "preferred_markup": "existing-competitive",
        "key_wins": [
            "Boot Barn (multiple locations) - $15-25K each",
            "Food Lion (multiple) - $25-40K each",
            "Venture IV Senior Living - $148K",
            "Cinemark Oxford Valley - $116K",
            "Stone Theatres - $49K",
            "Fuzzy's Taco - $29K"
        ],
        "key_losses": [
            "Hyatt Place Blacksburg - bid $59K, market ~$30K (DOUBLE)",
            "Hyatt Place Burlington - bid $47K, market ~$25K (DOUBLE)"
        ],
        "loss_patterns": "Hyatt hotels: our bids consistently 2x market. Need to recalibrate hotel production rates. All other facility types competitive.",
        "facility_types": ["retail", "senior_living", "cinema", "restaurant", "hotel", "industrial", "grocery"],
        "communication_style": "Email preferred. Long-standing relationship. Direct communication.",
        "last_updated": "2026-04-02"
    },
    "lf-jennings": {
        "name": "LF Jennings",
        "contacts": [
            {"name": "Contact TBD", "role": "Estimator", "email": ""}
        ],
        "relationship": "existing",
        "projects_bid": 5,
        "projects_won": 2,
        "win_rate": 0.40,
        "avg_bid_amount": 193000,
        "total_revenue": 386000,
        "pricing_notes": "Target store GC. Won 2 Target remodels ($193K each). Lost Target Lincolnton by 25% - our bid was significantly higher. Large-scale retail remodel specialist.",
        "preferred_markup": "existing-competitive",
        "key_wins": [
            "Target Garner - $193K",
            "Target Fayetteville - $193K"
        ],
        "key_losses": [
            "Target Lincolnton - 25% above lowest bidder"
        ],
        "loss_patterns": "When we lose, we lose BIG (25% gap). Need tighter pricing on Target stores. Verify scope assumptions carefully.",
        "facility_types": ["retail_target"],
        "communication_style": "Professional, bid-focused",
        "last_updated": "2026-04-02"
    },
    "wimco": {
        "name": "WIMCO Corp",
        "contacts": [
            {"name": "Contact TBD", "role": "Estimator", "email": ""}
        ],
        "relationship": "new",
        "projects_bid": 3,
        "projects_won": 0,
        "win_rate": 0.0,
        "avg_bid_amount": 30000,
        "total_revenue": 0,
        "pricing_notes": "Food Lion GC. 0 wins so far - but 2 losses were because GC lost the project, not because our price was wrong. Still worth bidding. Dental office work too.",
        "preferred_markup": "new-competitive",
        "key_wins": [],
        "key_losses": [
            "Food Lion Siler City - GC lost project",
            "Food Lion Stoneville - GC lost project"
        ],
        "loss_patterns": "Losses not due to our pricing - GC lost the overall bid. Keep bidding with them.",
        "facility_types": ["grocery_food_lion", "dental"],
        "communication_style": "Standard bid process",
        "last_updated": "2026-04-02"
    },
    "wed-construction": {
        "name": "WED Construction",
        "contacts": [
            {"name": "Jonathan Pendergraft", "role": "Estimator/PM", "email": ""}
        ],
        "relationship": "existing",
        "projects_bid": 5,
        "projects_won": 0,
        "win_rate": 0.0,
        "avg_bid_amount": 28000,
        "total_revenue": 0,
        "pricing_notes": "Food Lion GC. 0 wins - consistently 10-15% higher than competitors on Food Lion stores. Need to sharpen pricing significantly for FL work with WED. Consider AGGRESSIVE tier.",
        "preferred_markup": "aggressive",
        "key_wins": [],
        "key_losses": [
            "Food Lion 1513 Asheboro - 10% above winner",
            "Food Lion Liberty - 12% above winner",
            "Food Lion Kernersville - 15% above winner"
        ],
        "loss_patterns": "Consistent 10-15% gap on Food Lion. Our FL production rates may be too conservative for WED's scope definitions. Review and tighten.",
        "facility_types": ["grocery_food_lion"],
        "communication_style": "Responsive, provides feedback on bid results",
        "last_updated": "2026-04-02"
    },
    "rick-shipman": {
        "name": "Rick Shipman Construction",
        "contacts": [
            {"name": "Rick Shipman", "role": "Owner", "email": ""}
        ],
        "relationship": "existing",
        "projects_bid": 4,
        "projects_won": 2,
        "win_rate": 0.50,
        "avg_bid_amount": 30000,
        "total_revenue": 60000,
        "pricing_notes": "Local Monroe-area GC. Won 2 Food Lions near Monroe (~$30K each). Good relationship for local work. Price-competitive but fair.",
        "preferred_markup": "existing-competitive",
        "key_wins": [
            "Food Lion Monroe - $28K",
            "Food Lion Indian Trail - $32K"
        ],
        "key_losses": [
            "Food Lion Marshville - close, 5% gap"
        ],
        "loss_patterns": "When we lose it's close. Good fit for local FL work within 30mi of Monroe.",
        "facility_types": ["grocery_food_lion"],
        "communication_style": "Direct, local relationship. Phone/text preferred.",
        "last_updated": "2026-04-02"
    },
    "hendrick-construction": {
        "name": "Hendrick Construction",
        "contacts": [
            {"name": "Contact TBD", "role": "Estimator", "email": ""}
        ],
        "relationship": "new",
        "projects_bid": 3,
        "projects_won": 0,
        "win_rate": 0.0,
        "avg_bid_amount": 45000,
        "total_revenue": 0,
        "pricing_notes": "Charlotte-area GC. 3 bids submitted, no results back yet. Office and medical facility work. Monitor for results.",
        "preferred_markup": "new-standard",
        "key_wins": [],
        "key_losses": [],
        "loss_patterns": "No data yet - awaiting bid results.",
        "facility_types": ["office", "medical"],
        "communication_style": "Standard bid process",
        "last_updated": "2026-04-02"
    },
    "cmc-building": {
        "name": "CMC Building Inc.",
        "contacts": [
            {"name": "Parin Bodiwala", "role": "Estimator", "email": ""}
        ],
        "relationship": "new",
        "projects_bid": 2,
        "projects_won": 0,
        "win_rate": 0.0,
        "avg_bid_amount": 35000,
        "total_revenue": 0,
        "pricing_notes": "Government/military GC. Camp Butner and Wilson County work. 1 bid - GC lost project. Worth continuing to bid for government work.",
        "preferred_markup": "new-standard",
        "key_wins": [],
        "key_losses": [
            "Camp Butner - GC lost project"
        ],
        "loss_patterns": "Loss was GC losing, not our pricing. Keep bidding.",
        "facility_types": ["government", "military"],
        "communication_style": "BuildingConnected portal",
        "last_updated": "2026-04-02"
    },
    "williams-company": {
        "name": "The Williams Company",
        "contacts": [
            {"name": "Contact TBD", "role": "Estimator", "email": ""}
        ],
        "relationship": "new",
        "projects_bid": 2,
        "projects_won": 0,
        "win_rate": 0.0,
        "avg_bid_amount": 180000,
        "total_revenue": 0,
        "pricing_notes": "Target store GC. Lost Target Pineville by 7% gap. Closer than LF Jennings losses. Worth re-bidding with tighter numbers.",
        "preferred_markup": "new-competitive",
        "key_wins": [],
        "key_losses": [
            "Target Pineville - 7% above lowest"
        ],
        "loss_patterns": "7% gap is closeable. Tighten production rates for next Target bid.",
        "facility_types": ["retail_target"],
        "communication_style": "Standard bid process",
        "last_updated": "2026-04-02"
    },
    "dl-meacham": {
        "name": "DL Meacham Construction",
        "contacts": [
            {"name": "Contact TBD", "role": "Estimator", "email": ""}
        ],
        "relationship": "existing",
        "projects_bid": 5,
        "projects_won": 3,
        "win_rate": 0.60,
        "avg_bid_amount": 86000,
        "total_revenue": 258000,
        "pricing_notes": "Amenity center specialist. 3 wins in TX ($258K total). Willing to travel for right projects. Good markup tolerance on amenity/clubhouse work.",
        "preferred_markup": "existing-standard",
        "key_wins": [
            "Amenity Center Celina TX - $95K",
            "Amenity Center Forney TX - $88K",
            "Amenity Center McKinney TX - $75K"
        ],
        "key_losses": [
            "Amenity Center Georgetown TX - travel cost made us uncompetitive"
        ],
        "loss_patterns": "Competitive within 4-5hr drive. Beyond that, travel costs push us out.",
        "facility_types": ["amenity_center", "clubhouse"],
        "communication_style": "Professional, repeat client relationship",
        "last_updated": "2026-04-02"
    }
}

for slug, data in gc_data.items():
    filepath = gc_dir / f"{slug}.json"
    with open(filepath, "w") as f:
        json.dump(data, f, indent=2)
    print(f"Saved: {filepath.name}")

print(f"\nDone. {len(gc_data)} GC files updated.")
