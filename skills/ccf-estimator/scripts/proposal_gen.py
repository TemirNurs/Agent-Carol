#!/usr/bin/env python3
"""
CCF Proposal Generator
Generates a proposal document from estimate data matching CCF's format.
Usage: python proposal_gen.py --estimate estimate.json --project project.json [--format md|html]
"""

import argparse
import json
import sys
from pathlib import Path
from datetime import datetime


COMPANY_HEADER = """C A R O L I N A   C O M M E R C I A L   F I N I S H E S
Budget Painting and Wallcovering LLC | 3308 Chancellor Lane, Monroe, NC 28110 | (980) 348-1827
cs@carolinacommercialfinishes.com"""

COMPANY_FOOTER = "Carolina Commercial Finishes | Budget Painting and Wallcovering LLC | (980) 348-1827 | cs@carolinacommercialfinishes.com"

DEFAULT_INCLUSIONS = [
    "All labor, materials, and equipment for scope described above",
    "Sherwin-Williams paint products per specification",
    "Surface preparation — sanding, caulking, patching, spot priming",
    "Masking and protection of adjacent surfaces and fixtures",
    "Clean-up and protection of completed work daily",
    "Project supervision and quality control",
    "OSHA-compliant safety program",
    "$1M / $2M General Liability insurance",
    "Workers' Compensation insurance per NC requirements",
]

DEFAULT_EXCLUSIONS = [
    "Wallcovering — all types (vinyl, Acrovyn, fabric)",
    "Electrostatic painting",
    "ACT / drop ceiling painting",
    "Pressure washing (GC scope)",
    "Drywall repair beyond minor patching",
    "Lead / asbestos abatement",
    "Scaffolding or swing stage (priced separately if required)",
]

DEFAULT_TERMS = {
    "payment": "Net 30 from date of invoice. Progress billing monthly for projects exceeding 30 calendar days.",
    "change_orders": "Any work not described in the Scope of Work above requires a written change order signed by both parties before work commences.",
    "warranty": "Carolina Commercial Finishes warrants all workmanship for one (1) year from date of substantial completion. Paint manufacturer warranty applies separately. Warranty does not cover damage caused by others, abuse, or normal wear and tear.",
    "insurance": "General Liability $1M per occurrence / $2M aggregate. Workers' Compensation per NC state requirements. Certificates of Insurance provided upon request.",
    "dispute": "Any disputes shall first be addressed through good-faith negotiation, then mediation in the state of North Carolina, before either party pursues legal remedies.",
}

# Alternate terms for specific project types
BOOT_BARN_TERMS = {
    "payment": "Net 30 from date of invoice. Progress billing on projects exceeding 2 weeks. 50% deposit may be required for material procurement on wallcovering projects.",
    "change_orders": "Any work not included in this proposal will be priced and approved in writing before commencement. Change orders billed at $42.00/hr (T&M) plus materials.",
    "warranty": "Two (2) year warranty on workmanship. Manufacturer's warranty on all materials applies separately.",
    "insurance": "Carolina Commercial Finishes carries General Liability and Workers' Compensation insurance. Certificates of Insurance available upon request.",
    "access": "Continuous, unobstructed access to all work areas is required. Any delays due to access restrictions may result in schedule and cost adjustments.",
}

FOOD_LION_TERMS = {
    "payment": "Net 30 from date of invoice. Progress billing monthly for projects exceeding 30 calendar days.",
    "change_orders": "Any changes to scope of work must be authorized in writing via change order prior to execution.",
    "warranty": "Carolina Commercial Finishes warrants all workmanship for one (1) year from date of substantial completion. Paint manufacturer warranty applies separately. Warranty does not cover damage caused by others, abuse, or normal wear and tear.",
    "insurance": "General Liability $1M per occurrence / $2M aggregate. Workers' Compensation per NC state requirements. Certificates of Insurance provided upon request.",
    "schedule": "Work schedule coordinated with GC per project requirements. Sales area painting is night work only (10 PM - 8 AM).",
    "material_escalation": "Material price escalation clause: If paint prices increase >5% before project start, pricing subject to adjustment.",
    "dispute": "Any disputes shall first be addressed through good-faith negotiation, then mediation in the state of North Carolina, before either party pursues legal remedies.",
}


def format_currency(amount):
    """Format number as currency."""
    return f"${amount:,.0f}" if amount == int(amount) else f"${amount:,.2f}"


def generate_proposal_md(estimate, project):
    """Generate proposal in Markdown format matching CCF style."""
    proj = estimate.get("project", {})
    totals = estimate.get("totals", {})
    metrics = estimate.get("metrics", {})
    categories = estimate.get("category_subtotals", {})

    project_name = project.get("name", proj.get("name", ""))
    location = project.get("location", "")
    owner = project.get("owner", "")
    gc = project.get("gc", proj.get("gc", ""))
    bid_date = project.get("bid_date", proj.get("bid_date", ""))
    prepared_by = project.get("prepared_by", "Nursultan Temirbaev")
    paint_spec = project.get("paint_spec", "Sherwin-Williams")
    building_area = project.get("building_area", "")
    start_date = project.get("start_date", "")
    end_date = project.get("end_date", "")

    # Custom scope text and inclusions/exclusions from project
    scope_sections = project.get("scope_sections", [])
    inclusions = project.get("inclusions", DEFAULT_INCLUSIONS)
    exclusions = project.get("exclusions", DEFAULT_EXCLUSIONS)
    notes = project.get("notes", [])
    terms = project.get("terms", DEFAULT_TERMS)

    bid_price = totals.get("bid_price", 0)
    today = datetime.now().strftime("%B %d, %Y")

    lines = []

    # Page 1: Header + Summary
    lines.append(COMPANY_HEADER)
    lines.append("")
    lines.append("# PROPOSAL FOR")
    lines.append("## Painting")
    lines.append("---")
    lines.append("")
    lines.append(f"**Project:** {project_name}")
    if location:
        lines.append(f"**Location:** {location}")
    if owner:
        lines.append(f"**Owner:** {owner}")
    if gc:
        lines.append(f"**General Contractor:** {gc}")
    if paint_spec:
        lines.append(f"**Paint Specification:** {paint_spec}")
    if building_area:
        lines.append(f"**Building Area:** {building_area}")
    lines.append("")

    # Proposal Summary
    lines.append("## PROPOSAL SUMMARY")
    lines.append("")
    lines.append("| Scope | Amount |")
    lines.append("|-------|--------|")
    for cat_name, cat_data in categories.items():
        lines.append(f"| {cat_name} | {format_currency(cat_data['subtotal'])} |")
    lines.append(f"| **TOTAL PROPOSAL AMOUNT** | **{format_currency(bid_price)}** |")
    lines.append("")
    lines.append(f"**Date:** {today}")
    lines.append(f"**Prepared By:** {prepared_by}")
    lines.append("**Valid For:** 30 Days")
    lines.append("")

    # Page 2: Scope of Work
    lines.append("---")
    lines.append("## SCOPE OF WORK")
    lines.append("")

    if scope_sections:
        for section in scope_sections:
            lines.append(f"### {section.get('title', '')}")
            lines.append(section.get("body", ""))
            lines.append("")
    else:
        # Auto-generate from categories
        section_num = 1
        for cat_name, cat_data in categories.items():
            lines.append(f"### {section_num}. {cat_name.upper()} — {format_currency(cat_data['subtotal'])}")
            # List line items in this category
            for li in estimate.get("line_items", []):
                if li.get("area") == cat_name:
                    lines.append(f"- {li['task']}: {li['quantity']:.0f} {li['unit']}")
            lines.append("")
            section_num += 1

    lines.append(f"**TOTAL PROPOSAL AMOUNT: {format_currency(bid_price)}**")
    lines.append("")

    # Page 3: Inclusions & Exclusions
    lines.append("---")
    lines.append("## INCLUSIONS & EXCLUSIONS")
    lines.append("")
    lines.append("### INCLUSIONS")
    for inc in inclusions:
        lines.append(f"- {inc}")
    lines.append("")
    lines.append("### EXCLUSIONS")
    for exc in exclusions:
        lines.append(f"- {exc}")
    lines.append("")

    # Notes & Assumptions
    if notes:
        lines.append("### NOTES & ASSUMPTIONS")
        for i, note in enumerate(notes, 1):
            lines.append(f"{i}. {note}")
        lines.append("")

    # Page 4: Terms & Conditions
    lines.append("---")
    lines.append("## TERMS & CONDITIONS")
    lines.append("")
    if isinstance(terms, dict):
        term_num = 1
        for title, body in terms.items():
            lines.append(f"### {term_num}. {title.replace('_', ' ').title()}")
            lines.append(body)
            lines.append("")
            term_num += 1
    elif isinstance(terms, list):
        for i, term in enumerate(terms, 1):
            lines.append(f"### {i}. {term.get('title', '')}")
            lines.append(term.get("body", ""))
            lines.append("")

    if start_date or end_date:
        lines.append(f"### Schedule")
        if start_date:
            lines.append(f"Work begins: {start_date}")
        if end_date:
            lines.append(f"Target completion: {end_date}")
        lines.append("")

    # Acceptance
    lines.append("---")
    lines.append("## ACCEPTANCE & AUTHORIZATION")
    lines.append("")
    lines.append(f"By signing below, the parties agree to the scope, terms, and pricing outlined in this proposal for the total contract amount of {format_currency(bid_price)}.")
    lines.append("")
    lines.append("| | General Contractor | Carolina Commercial Finishes |")
    lines.append("|---|---|---|")
    lines.append("| Authorized Signature | ___________________ | ___________________ |")
    lines.append("| Printed Name | ___________________ | ___________________ |")
    lines.append("| Title | ___________________ | ___________________ |")
    lines.append("| Date | ___________________ | ___________________ |")
    lines.append("")
    lines.append(f"*{COMPANY_FOOTER}*")

    return "\n".join(lines)


def generate_gc_email(estimate, project):
    """Generate a draft GC email to send with the proposal."""
    proj = estimate.get("project", {})
    totals = estimate.get("totals", {})

    project_name = project.get("name", proj.get("name", ""))
    gc = project.get("gc", proj.get("gc", ""))
    gc_contact = project.get("gc_contact", "")
    bid_price = totals.get("bid_price", 0)

    email = {
        "subject": f"Painting Proposal — {project_name}",
        "body": f"""Hi {gc_contact or gc},

Please find attached our proposal for the painting scope on {project_name}.

Total Proposal Amount: {format_currency(bid_price)}

Scope includes all interior and exterior painting per plans and specifications. Please see the attached proposal for full scope, inclusions, exclusions, and terms.

We appreciate the opportunity to bid on this project. Please don't hesitate to reach out with any questions or if you need any clarifications.

Best regards,
Nursultan Temirbaev
Carolina Commercial Finishes
(980) 348-1827
cs@carolinacommercialfinishes.com"""
    }
    return email


def main():
    parser = argparse.ArgumentParser(description="CCF Proposal Generator")
    parser.add_argument("--estimate", required=True, help="Path to estimate JSON")
    parser.add_argument("--project", required=True, help="Path to project JSON")
    parser.add_argument("--format", default="md", choices=["md", "html"], help="Output format")
    parser.add_argument("--email", action="store_true", help="Also generate GC email draft")
    args = parser.parse_args()

    with open(args.estimate) as f:
        estimate = json.load(f)
    with open(args.project) as f:
        project = json.load(f)

    if args.format == "md":
        proposal = generate_proposal_md(estimate, project)
    else:
        proposal = generate_proposal_md(estimate, project)

    print(proposal)

    if args.email:
        email = generate_gc_email(estimate, project)
        print("\n\n--- EMAIL DRAFT ---")
        print(json.dumps(email, indent=2))


if __name__ == "__main__":
    main()
