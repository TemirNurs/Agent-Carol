#!/usr/bin/env python3
"""Update GC memory files with contact info from CRM + CC scrapes."""
import json
from pathlib import Path

gc_dir = Path(r"C:\Users\Nursm\Agent Carol\data\memory\gc")

# Contact info gathered from CRM, CC project pages, and emails
contact_updates = {
    "wimco.json": {
        "contacts": [
            {"name": "SuSu Hunniecutt", "role": "GC Bidder/Estimator", "email": "susu@wimcocorp.com", "phone": "(252) 946-5175"},
            {"name": "John Thompson", "role": "Contact", "phone": "(828) 446-8976"}
        ]
    },
    "parkway-construction.json": {
        "contacts": [
            {"name": "Jim Hinton", "role": "Estimator/PM", "email": "jim@parkwayconstruction.com"},
            {"name": "Matthew Hinton", "role": "PM", "email": "matthew@parkwayconstruction.com"}
        ]
    },
    "cmc-building.json": {
        "contacts": [
            {"name": "Parin Bodiwala", "role": "Estimator", "email": ""}
        ]
    },
    "wed-construction.json": {
        "contacts": [
            {"name": "Jonathan Pendergraft", "role": "Estimator/PM", "email": ""}
        ]
    }
}

# Also add Integrity Construction as new GC (from email bid invites)
integrity_data = {
    "name": "Integrity Construction Management",
    "contacts": [
        {"name": "Taylor Davis", "role": "Senior Estimator", "email": "tdavis@integrity-cm.com", "phone": "+1 678-787-1246"}
    ],
    "relationship": "new",
    "projects_bid": 2,
    "projects_won": 0,
    "win_rate": 0.0,
    "avg_bid_amount": 0,
    "total_revenue": 0,
    "pricing_notes": "Marietta, GA based. Active bid invites: Dutch Bros Spartanburg, Midway Lake Norman (amenity clubhouse), Lippard Lane Industrial. Also sent specs/RFI responses. Engaged GC.",
    "preferred_markup": "new-competitive",
    "key_wins": [],
    "key_losses": [],
    "loss_patterns": "No data yet - new relationship.",
    "facility_types": ["restaurant", "amenity_center", "industrial"],
    "communication_style": "BuildingConnected portal. Sends specs and RFI responses proactively. Responsive.",
    "last_updated": "2026-04-02"
}

# Update existing GC files
for filename, updates in contact_updates.items():
    filepath = gc_dir / filename
    if filepath.exists():
        data = json.load(open(filepath))
        data.update(updates)
        data["last_updated"] = "2026-04-02"
        with open(filepath, "w") as f:
            json.dump(data, f, indent=2)
        print(f"Updated: {filename}")
    else:
        print(f"NOT FOUND: {filename}")

# Create Integrity Construction
filepath = gc_dir / "integrity-construction.json"
with open(filepath, "w") as f:
    json.dump(integrity_data, f, indent=2)
print(f"Created: integrity-construction.json")

print(f"\nDone. GC contacts updated.")
