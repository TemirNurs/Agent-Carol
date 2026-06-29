#!/usr/bin/env python3
"""
CCF Facility Type Classifier
Classifies projects by facility type using keyword matching + memory patterns.
Usage:
  python facility_classifier.py --text "Food Lion Store #2655 Remodel in Clemmons NC"
  python facility_classifier.py --text "Hyatt Select Charlotte" --owner "Hyatt Hotels"
"""

import argparse
import json
import sys

FACILITY_TYPES = {
    "retail_food_lion": {
        "label": "Retail — Food Lion",
        "keywords": ["food lion"],
        "priority": 10,
    },
    "retail_boot_barn": {
        "label": "Retail — Boot Barn",
        "keywords": ["boot barn"],
        "priority": 10,
    },
    "retail_target": {
        "label": "Retail — Target",
        "keywords": ["target"],
        "priority": 10,
    },
    "retail_general": {
        "label": "Retail",
        "keywords": ["store", "retail", "shop", "walmart", "lidl", "aldi", "dollar general",
                     "dollar tree", "cvs", "walgreens", "lowes", "home depot", "autozone",
                     "o'reilly", "advance auto", "tractor supply"],
        "priority": 5,
    },
    "warehouse": {
        "label": "Warehouse / Distribution",
        "keywords": ["warehouse", "distribution", "fulfillment", "logistics", "storage facility",
                     "industrial", "manufacturing"],
        "priority": 5,
    },
    "senior_living": {
        "label": "Senior Living",
        "keywords": ["senior living", "assisted living", "memory care", "retirement community",
                     "retirement home", "elder care", "nursing", "skilled nursing"],
        "priority": 5,
    },
    "hospitality": {
        "label": "Hospitality",
        "keywords": ["hotel", "hyatt", "marriott", "hilton", "hampton inn", "holiday inn",
                     "comfort inn", "best western", "motel", "resort", "lodge", "inn"],
        "priority": 5,
    },
    "restaurant": {
        "label": "Restaurant",
        "keywords": ["restaurant", "chick-fil-a", "mcdonald", "wendy", "panera", "chipotle",
                     "starbucks", "dunkin", "subway", "taco bell", "burger king", "cookout",
                     "bojangles", "waffle house", "cracker barrel", "zaxby"],
        "priority": 5,
    },
    "office": {
        "label": "Office",
        "keywords": ["office", "upfit", "buildout", "tenant improvement", "corporate",
                     "headquarters", "coworking", "flex space"],
        "priority": 3,
    },
    "medical": {
        "label": "Medical / Healthcare",
        "keywords": ["medical", "hospital", "clinic", "dental", "urgent care", "health center",
                     "pharmacy", "surgical", "orthopedic", "pediatric", "veterinary"],
        "priority": 5,
    },
    "education": {
        "label": "Education",
        "keywords": ["school", "university", "college", "academy", "campus", "gymnasium",
                     "library", "dormitory", "student"],
        "priority": 5,
    },
    "religious": {
        "label": "Religious",
        "keywords": ["church", "mosque", "temple", "synagogue", "worship", "ministry",
                     "chapel", "cathedral", "parish"],
        "priority": 5,
    },
    "cinema": {
        "label": "Cinema / Entertainment",
        "keywords": ["cinema", "theater", "theatre", "amc", "regal", "cinemark",
                     "entertainment", "bowling", "arcade"],
        "priority": 5,
    },
    "government": {
        "label": "Government / Municipal",
        "keywords": ["government", "county", "city hall", "municipal", "state building",
                     "courthouse", "fire station", "police", "post office", "federal"],
        "priority": 5,
    },
    "multi_family": {
        "label": "Multi-Family Residential",
        "keywords": ["apartment", "condo", "townhome", "multi-family", "residential",
                     "housing", "duplex"],
        "priority": 5,
    },
}


def classify(text, owner=None, gc=None):
    """Classify a project by facility type.
    Returns: {"type": "retail_food_lion", "label": "Retail — Food Lion", "confidence": 0.95}
    """
    combined = f"{text} {owner or ''} {gc or ''}".lower()

    best_match = None
    best_priority = -1
    best_keyword = None

    for ftype, config in FACILITY_TYPES.items():
        for kw in config["keywords"]:
            if kw in combined:
                if config["priority"] > best_priority:
                    best_match = ftype
                    best_priority = config["priority"]
                    best_keyword = kw

    if best_match:
        confidence = 0.95 if best_priority >= 10 else 0.75 if best_priority >= 5 else 0.50
        return {
            "type": best_match,
            "label": FACILITY_TYPES[best_match]["label"],
            "confidence": confidence,
            "matched_keyword": best_keyword,
        }

    return {
        "type": "commercial",
        "label": "Commercial (General)",
        "confidence": 0.30,
        "matched_keyword": None,
    }


def classify_batch(projects):
    """Classify multiple projects and return categorized summary."""
    results = []
    categories = {}

    for p in projects:
        text = p.get("project_name", "") + " " + p.get("description", "")
        owner = p.get("owner")
        gc = p.get("gc")
        c = classify(text, owner, gc)
        c["project_name"] = p.get("project_name", "Unknown")
        results.append(c)

        label = c["label"]
        if label not in categories:
            categories[label] = 0
        categories[label] += 1

    # Build summary string like "2 warehouses, 5 retail, 1 senior living"
    summary_parts = []
    for label, count in sorted(categories.items(), key=lambda x: -x[1]):
        summary_parts.append(f"{count} {label}")

    return {
        "classifications": results,
        "summary": ", ".join(summary_parts),
        "categories": categories,
    }


def main():
    parser = argparse.ArgumentParser(description="CCF Facility Classifier")
    parser.add_argument("--text", default=None, help="Project text to classify")
    parser.add_argument("--owner", default=None, help="Project owner")
    parser.add_argument("--gc", default=None, help="General contractor")
    parser.add_argument("--batch", default=None, help="JSON array of projects")
    args = parser.parse_args()

    if args.batch:
        projects = json.loads(args.batch)
        result = classify_batch(projects)
    elif args.text:
        result = classify(args.text, args.owner, args.gc)
    else:
        parser.print_help()
        sys.exit(1)

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
