#!/usr/bin/env python3
"""
CCF Trade Filter
Filters bid opportunities to trades CCF bids on.

Updated 2026-05-09: added Low Voltage / Structured Cabling / Communications
trades after CCF expanded its ConstructConnect subscription to include LV.

We bid on:
  - Painting, Wallcovering, Wall Covering, Interior Painting, Finishes
  - Low Voltage / Structured Cabling / Communications / Telecom / Data
"""

# Trades we bid on (case-insensitive matching)
OUR_TRADES = [
    # Painting & wallcovering
    "painting",
    "wallcovering",
    "wall covering",
    "interior painting",
    "painting & wall covering",
    "painting & wallcovering",
    "painting and wallcovering",
    "painting and wall covering",
    "finishes",  # may include painting — keep but flag for review
    # Low voltage / structured cabling (added 2026-05-09)
    "low voltage",
    "low-voltage",
    "structured cabling",
    "structured wiring",
    "communications",            # Procore/CC trade name for LV
    "communications cabling",
    "data cabling",
    "telecommunications",
    "telecom",
    "voice and data",
    "voice & data",
    "voice/data",
    "fiber optic",
    "network cabling",
    "structural lv cabling",
    "structural cabling",
    "cabling",                   # broad — flag for review but allow
]

# Trades we NEVER bid on
EXCLUDED_TRADES = [
    "flooring",
    "firestopping",
    "framing",
    "drywall",
    "ceramic tile",
    "resilient tile",
    "wood trusses",
    "sound panels",
    "sound absorbing",
    "hvac",
    # NOTE: "electrical" removed from exclusion 2026-05-09 because CC sometimes
    # tags LV / structured cabling as "Electrical - Low Voltage" or similar.
    # If a project is JUST high-voltage electrical (no LV/cabling component),
    # the project_name + scope text will let us filter manually.
    "plumbing",
    "fire protection",
    "roofing",
    "concrete",
    "masonry",
    "structural steel",
    "demolition",
    "excavation",
    "paving",
    "landscaping",
    "mechanical",
    "insulation",
    "waterproofing",
    "glass",
    "glazing",
    "doors & hardware",
    "elevator",
    "sprinkler",
]


def is_our_trade(trade_name):
    """Check if a trade is one we bid on.
    Returns: True if it's painting/wallcovering, False otherwise.
    """
    if not trade_name:
        return False
    trade_lower = trade_name.strip().lower()

    # Check exclusions first (explicit NO)
    for excluded in EXCLUDED_TRADES:
        if excluded in trade_lower:
            return False

    # Check if it matches our trades
    for our_trade in OUR_TRADES:
        if our_trade in trade_lower:
            return True

    return False


def filter_bids(bids):
    """Filter a list of bid opportunities to only our trades.
    Returns: (matching_bids, filtered_out_count)
    """
    matching = []
    filtered_out = 0

    for bid in bids:
        trade = bid.get("trade", "") or ""
        trades_list = bid.get("trades", [])

        # Check single trade field
        if trade and is_our_trade(trade):
            matching.append(bid)
            continue

        # Check trades list
        if trades_list:
            has_our_trade = any(is_our_trade(t) for t in trades_list)
            if has_our_trade:
                matching.append(bid)
                continue

        # If no trade info at all, include it (could be painting)
        if not trade and not trades_list:
            bid["_trade_note"] = "No trade specified — review manually"
            matching.append(bid)
            continue

        filtered_out += 1

    return matching, filtered_out


def main():
    import json, sys
    # Test with sample trades
    test_trades = [
        "Painting", "Flooring", "Painting & Wall Covering", "Firestopping",
        "Interior Painting", "Framing, Drywall & General Trades",
        "Ceramic Tile", "Finishes", "Sound Panels", "Wallcovering",
        "Resilient Tile Flooring", "Wall Panels",
    ]
    print("Trade filter test:")
    for t in test_trades:
        result = "PASS" if is_our_trade(t) else "SKIP"
        print(f"  {result}: {t}")


if __name__ == "__main__":
    main()
