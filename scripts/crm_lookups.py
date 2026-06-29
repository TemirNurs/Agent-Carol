#!/usr/bin/env python3
"""
crm_lookups.py - Dropdown-value helpers for the CRM Bid Log sheet.

The CRM has data validation on these columns, with values pulled from
the `Lookups` tab:
  - Facility Type  (Lookups col B)
  - Bid Source     (Lookups col E)
  - Status         (Lookups col A)
  - Loss Reason    (Lookups col F)

Writing a value that isn't in the dropdown shows a red warning triangle in
Sheets even though gspread accepts it. This module:
  1. Caches the live dropdown options for each column on first access.
  2. Provides a map from freeform/legacy strings to the canonical dropdown
     value, so any auto-fill snaps to a valid choice.

Usage:
  from crm_lookups import canonicalize, valid_options
  loss = canonicalize("Loss Reason", "Came 2nd")
  # -> "Price Too High"
"""
from __future__ import annotations
import re
from functools import lru_cache


# Hand-curated map from freeform/legacy text to the canonical dropdown choice.
# Add entries as you encounter new variants. The lookup is case-insensitive.
_FREEFORM_TO_DROPDOWN = {
    "Loss Reason": {
        "gc went with another": "Competitor Relationship",
        "gc went with another sub": "Competitor Relationship",
        "lost to competitor": "Competitor Relationship",
        "competitor won": "Competitor Relationship",
        "came 2nd": "Price Too High",
        "came second": "Price Too High",
        "not low bidder": "Price Too High",
        "pricing 10% high": "Price Too High",
        "pricing too high": "Price Too High",
        "price high": "Price Too High",
        "10% high": "Price Too High",
        "15% higher than the budget": "Price Too High",
        "higher than budget": "Price Too High",
        "twice over": "Price Too High",
        "was twice over": "Price Too High",
        "was twice over, revised": "Price Too High",
        "gc lost project": "GC Lost Project",
        "gc didn't get the project": "GC Lost Project",
        "owner changed direction": "Owner Changed Direction",
        "owner pivot": "Owner Changed Direction",
        "scope mismatch": "Scope Mismatch",
        "scope changed": "Scope Mismatch",
        "wrong scope": "Scope Mismatch",
        "late submission": "Late Submission",
        "submitted late": "Late Submission",
        "no feedback": "No Feedback",
        "no response": "No Feedback",
        "ghosted": "No Feedback",
        "resubmitted": "",  # not a loss reason, clear
        "revised": "",
        "no decision yet": "",
    },
    "Facility Type": {
        "grocery": "Grocery Store",
        "supermarket": "Grocery Store",
        "big box": "Retail / Big Box",
        "retail": "Retail / Big Box",
        "hospital": "Medical / Hospital",
        "medical": "Medical / Hospital",
        "clinic": "Medical / Hospital",
        "dental": "Medical / Hospital",
        "vamc": "Medical / Hospital",
        "school": "School / Education",
        "education": "School / Education",
        "university": "School / Education",
        "hotel": "Hotel",
        "hilton": "Hotel",
        "suites": "Hotel",
        "marriott": "Hotel",
        "carvana": "Car Retail / Dealership",
        "adesa": "Car Retail / Dealership",
        "dealership": "Car Retail / Dealership",
        "commercial auto": "Car Retail / Dealership",
        "industrial": "Industrial",
        "warehouse": "Industrial",
        "office": "Commercial Office",
        "religious": "Religious",
        "church": "Religious",
        "gym": "Gym / Fitness",
        "fitness": "Gym / Fitness",
        "city": "City / Government",
        "government": "City / Government",
        "gas station": "Truck Stop / Gas Station",
        "truck stop": "Truck Stop / Gas Station",
        "restaurant": "Restaurant",
        "bar": "Bar",
        "amenity": "Amenity Center",
        "cinema": "Cinema / Entertainment",
        "theater": "Cinema / Entertainment",
        "senior living": "Senior Living / Healthcare",
        "assisted living": "Senior Living / Healthcare",
        "multi-family": "Residential Multi-Family",
        "multifamily": "Residential Multi-Family",
        "apartments": "Residential Multi-Family",
        "equipment rental": "Equipment Rentals",
        "sunbelt": "Equipment Rentals",
    },
    "Status": {
        "submitted": "Bid Submitted",
        "bid submitted": "Bid Submitted",
        "estimating": "Estimating",
        "itb received": "ITB Received",
        "awaiting": "Awaiting Decision",
        "awaiting decision": "Awaiting Decision",
        "pending": "Awaiting Decision",
        "won": "Won",
        "lost": "Lost",
        "no decision": "No Decision",
        "withdrawn": "Withdrawn",
        "on hold": "On Hold",
    },
    "Bid Source": {
        "invitation": "Invitation (GC)",
        "invitation (gc)": "Invitation (GC)",
        "gc invitation": "Invitation (GC)",
        "email": "Invitation (GC)",
        "plan room": "Plan Room",
        "constructconnect": "Plan Room",
        "construct connect": "Plan Room",
        "buildingconnected": "Plan Room",
        "bc": "Plan Room",
        "procore": "Online Portal",
        "online portal": "Online Portal",
        "portal": "Online Portal",
        "parkway portal": "Online Portal",
        "smartbidnet": "Online Portal",
        "isqft": "Online Portal",
        "cold outreach": "Cold Outreach",
        "outreach": "Cold Outreach",
        "repeat client": "Repeat Client",
        "repeat": "Repeat Client",
        "referral": "Referral",
    },
}


@lru_cache(maxsize=1)
def _load_lookups():
    """Read the live Lookups tab and return a dict of column->valid_options."""
    from crm_lib import get_sheet  # local import to avoid circular deps
    try:
        sh = get_sheet("Lookups")
        data = sh.get_all_values()
    except Exception:
        return {}
    if not data: return {}
    headers = data[0]
    out = {}
    for c_idx, h in enumerate(headers):
        if not h: continue
        values = []
        for row in data[1:]:
            v = (row[c_idx] if c_idx < len(row) else "").strip()
            if v:
                values.append(v)
        if values:
            out[h] = values
    return out


# Map Lookups-tab column name -> Bid Log column name (when they differ)
_LOOKUP_HEADER_ALIAS = {
    "Bid Status": "Status",   # Lookups col A header is "Bid Status"
}


def valid_options(crm_column):
    """Return the list of valid dropdown values for a Bid Log column."""
    lookups = _load_lookups()
    # Try direct + alias
    if crm_column in lookups:
        return lookups[crm_column]
    for k, v in _LOOKUP_HEADER_ALIAS.items():
        if v == crm_column and k in lookups:
            return lookups[k]
    return []


def canonicalize(crm_column, raw_value):
    """Return a valid dropdown value for the given (column, raw input).
    If raw_value already matches the dropdown (case-insensitive), return it.
    Otherwise look up the freeform-to-dropdown map. Returns "" if unmappable
    (caller should leave the cell blank rather than write an invalid value)."""
    if not raw_value:
        return ""
    raw = str(raw_value).strip()
    if not raw:
        return ""
    options = valid_options(crm_column)
    # Exact match (case-insensitive)
    for opt in options:
        if opt.lower() == raw.lower():
            return opt
    # Freeform substring match against the curated map
    freemap = _FREEFORM_TO_DROPDOWN.get(crm_column, {})
    raw_lower = raw.lower()
    if raw_lower in freemap:
        return freemap[raw_lower]
    # Partial substring match against curated map keys
    for k, v in freemap.items():
        if k in raw_lower or raw_lower in k:
            return v
    # Last-resort: substring against the actual dropdown options
    for opt in options:
        if opt.lower() in raw_lower or raw_lower in opt.lower():
            return opt
    return ""  # caller decides whether to write "Other" or leave blank


if __name__ == "__main__":
    # Smoke test
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print("Status options:", valid_options("Status"))
    print("Loss Reason options:", valid_options("Loss Reason"))
    print()
    test_cases = [
        ("Loss Reason", "Came 2nd"),
        ("Loss Reason", "GC went with another"),
        ("Loss Reason", "GC Lost Project"),
        ("Loss Reason", "Resubmitted"),
        ("Facility Type", "Commercial Auto"),
        ("Facility Type", "Grocery Store"),
        ("Status", "submitted"),
        ("Bid Source", "Procore"),
    ]
    for col, val in test_cases:
        result = canonicalize(col, val)
        print(f'  {col}: {val!r:<28} -> {result!r}')
