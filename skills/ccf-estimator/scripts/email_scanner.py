#!/usr/bin/env python3
"""
CCF Email Scanner for Bid Invitations
Scans Gmail for bid invitations, addenda, and bid-related emails.
Designed to be called by the OpenClaw agent using Gmail MCP tools.

This script outputs Gmail search queries and parsing instructions
for the agent to use with gmail_search_messages and gmail_read_message.

Usage:
  python email_scanner.py --action search-queries
  python email_scanner.py --action parse-email --json '<email_data>'
  python email_scanner.py --action categorize-emails --json '<emails_list>'
"""

import argparse
import json
import re
import sys
from datetime import datetime, timedelta


# Gmail search queries for finding bid invitations
BID_SEARCH_QUERIES = [
    # Direct bid invitations
    'subject:("bid invitation" OR "invitation to bid" OR "ITB") newer_than:7d',
    'subject:("request for proposal" OR "RFP" OR "request for quote" OR "RFQ") newer_than:7d',
    'subject:("painting" OR "wallcovering" OR "wall covering") subject:("bid" OR "proposal" OR "quote") newer_than:7d',

    # From known GCs
    'from:(parkway OR "wed construction" OR "williams company" OR "lf jennings" OR "cmc building") subject:(bid OR proposal OR quote OR project) newer_than:7d',

    # BuildingConnected / ConstructConnect notifications
    'from:(buildingconnected.com OR constructconnect.com OR isqft.com) newer_than:7d',

    # Addenda and changes
    'subject:("addendum" OR "addenda" OR "bid date change" OR "revised") ("painting" OR "wallcovering") newer_than:14d',

    # Scope letters
    'subject:("scope" OR "scope letter" OR "scope of work") ("painting" OR "finish") newer_than:7d',
]

# Keywords that indicate a bid invitation email
BID_KEYWORDS = [
    "bid invitation", "invitation to bid", "request for proposal",
    "request for quote", "RFP", "RFQ", "ITB",
    "bid date", "due date", "bid opening",
    "painting", "wallcovering", "wall covering", "interior finish",
    "Division 09", "Section 09", "09 91", "09 96",
    "pre-bid", "bid walk", "site visit",
    "plans available", "documents available", "planroom",
    "buildingconnected", "constructconnect", "isqft",
]

# Keywords for addenda
ADDENDUM_KEYWORDS = [
    "addendum", "addenda", "revision", "revised",
    "bid date change", "date extension", "new bid date",
    "scope change", "scope revision", "updated",
]

# Facility type detection from project names
FACILITY_PATTERNS = {
    "retail": ["food lion", "boot barn", "target", "walmart", "lidl", "aldi", "dollar", "cvs", "walgreens", "store #"],
    "warehouse": ["warehouse", "distribution", "fulfillment", "logistics", "storage"],
    "senior_living": ["senior", "assisted living", "memory care", "retirement", "elder"],
    "hospitality": ["hotel", "hyatt", "marriott", "hilton", "hampton", "holiday inn", "motel"],
    "restaurant": ["restaurant", "chick-fil-a", "mcdonald", "wendy", "panera", "chipotle"],
    "office": ["office", "upfit", "buildout", "tenant improvement", "TI"],
    "medical": ["medical", "hospital", "clinic", "dental", "urgent care", "health"],
    "education": ["school", "university", "college", "academy", "campus"],
    "religious": ["church", "mosque", "temple", "worship", "ministry"],
    "cinema": ["cinema", "theater", "theatre", "AMC", "regal"],
    "government": ["government", "county", "city hall", "municipal", "state"],
    "multi_family": ["apartment", "condo", "townhome", "residential", "multi-family"],
}


def get_search_queries():
    """Return Gmail search queries for bid-related emails."""
    return BID_SEARCH_QUERIES


def parse_email_for_bid(email_data):
    """Parse an email and extract bid information.
    email_data: dict with keys: subject, from, date, body, attachments
    """
    subject = str(email_data.get("subject", "")).lower()
    body = str(email_data.get("body", "")).lower()
    sender = str(email_data.get("from", "")).lower()
    full_text = f"{subject} {body}"

    # Determine email type
    email_type = "unknown"
    if any(kw in full_text for kw in BID_KEYWORDS[:6]):
        email_type = "bid_invitation"
    elif any(kw in full_text for kw in ADDENDUM_KEYWORDS):
        email_type = "addendum"
    elif "buildingconnected" in sender or "constructconnect" in sender:
        email_type = "portal_notification"

    # Extract project name (heuristic)
    project_name = None
    patterns = [
        r"project:\s*(.+?)(?:\n|$)",
        r"re:\s*(.+?)(?:\s*-\s*|\n|$)",
        r"(?:food lion|boot barn|target|hyatt)\s*(?:#?\d+\w*)?(?:\s+\w+)*",
    ]
    for pat in patterns:
        match = re.search(pat, email_data.get("subject", ""), re.IGNORECASE)
        if match:
            project_name = match.group(1).strip() if match.lastindex else match.group(0).strip()
            break
    if not project_name:
        project_name = email_data.get("subject", "Unknown Project")

    # Extract GC name from sender
    gc_name = _extract_gc_from_sender(sender, body)

    # Detect facility type
    facility_type = classify_facility(project_name + " " + body)

    # Extract bid date
    bid_date = _extract_date(full_text)

    # Extract location
    location = _extract_location(full_text)

    return {
        "email_type": email_type,
        "project_name": project_name,
        "gc": gc_name,
        "facility_type": facility_type,
        "bid_due": bid_date,
        "location": location,
        "has_attachments": bool(email_data.get("attachments")),
        "source": "email",
        "subject": email_data.get("subject", ""),
        "from": email_data.get("from", ""),
    }


def classify_facility(text):
    """Classify facility type from text."""
    text_lower = text.lower()
    for ftype, keywords in FACILITY_PATTERNS.items():
        if any(kw in text_lower for kw in keywords):
            return ftype
    return "commercial"


def _extract_gc_from_sender(sender, body):
    """Try to extract GC name from email sender or body."""
    known_gcs = {
        "parkway": "Parkway Construction",
        "wed": "WED Construction",
        "williams": "Williams Company",
        "jennings": "LF Jennings",
        "cmc": "CMC Building",
    }
    for key, name in known_gcs.items():
        if key in sender or key in body.lower():
            return name
    # Try to get from sender display name
    match = re.match(r"(.+?)\s*<", sender)
    if match:
        return match.group(1).strip()
    return None


def _extract_date(text):
    """Try to extract a bid due date from text."""
    patterns = [
        r"(?:bid\s+(?:due|date|opening))[\s:]+(\w+\s+\d{1,2},?\s+\d{4})",
        r"(?:due\s+(?:date|by))[\s:]+(\w+\s+\d{1,2},?\s+\d{4})",
        r"(\d{1,2}/\d{1,2}/\d{2,4})\s*(?:at\s+\d{1,2}:\d{2})?",
        r"(\w+\s+\d{1,2},?\s+\d{4})\s*(?:at\s+\d{1,2}:\d{2})?",
    ]
    for pat in patterns:
        match = re.search(pat, text, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return None


def _extract_location(text):
    """Try to extract location (city, state) from text."""
    nc_sc_pattern = r"(\w[\w\s]+,\s*(?:NC|SC|North Carolina|South Carolina)(?:\s+\d{5})?)"
    match = re.search(nc_sc_pattern, text, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return None


def categorize_emails(emails_list):
    """Categorize a list of parsed emails by type and facility."""
    categories = {}
    for email in emails_list:
        ftype = email.get("facility_type", "other")
        if ftype not in categories:
            categories[ftype] = []
        categories[ftype].append(email)

    summary = {}
    for ftype, emails in categories.items():
        summary[ftype] = {
            "count": len(emails),
            "projects": [e.get("project_name", "?") for e in emails],
        }

    return {"categories": categories, "summary": summary}


def main():
    parser = argparse.ArgumentParser(description="CCF Email Scanner")
    parser.add_argument("--action", required=True,
                       choices=["search-queries", "parse-email", "categorize-emails"])
    parser.add_argument("--json", default=None, help="JSON data input")
    args = parser.parse_args()

    if args.action == "search-queries":
        result = get_search_queries()
    elif args.action == "parse-email":
        data = json.loads(args.json)
        result = parse_email_for_bid(data)
    elif args.action == "categorize-emails":
        data = json.loads(args.json)
        result = categorize_emails(data)

    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
