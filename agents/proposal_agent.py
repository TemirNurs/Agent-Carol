#!/usr/bin/env python3
"""
Proposal Agent — Generates professional proposals and GC emails
===============================================================
Activated by Carol Core after owner approves estimate.
Generates HTML proposals matching the CCF template style from real proposals:
  - Boot Barn #1660, Food Lion #1510A, Food Lion #0387, Food Lion #1513, Venture IV

Layout (4-5 pages):
  Page 1: Cover — "PROPOSAL FOR", project name, details, pricing summary, scope overview
  Page 2: Detailed scope — room-by-room tables (walls, ceilings, doors & frames, exterior)
  Page 3: Wallcovering (if any), Prep & Misc
  Page 4: Inclusions, Exclusions, Notes & Assumptions
  Page 5: Terms & Conditions, Acceptance & Signature block

Output formats:
  - HTML (always generated)
  - PDF (via weasyprint if available)
  - Markdown (simplified for WhatsApp/text)
"""

import json
import math
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
PROJECTS_DIR = DATA_DIR / "projects"
TEMPLATES_DIR = DATA_DIR / "templates" / "proposals"

# CCF Company Information
CCF = {
    "company": "Carolina Commercial Finishes",
    "legal": "Budget Painting and Wallcovering LLC",
    "owner": "Nursultan Temirbaev",
    "title": "Project Manager",
    "phone": "(980) 348-1827",
    "email": "cs@carolinacommercialfinishes.com",
    "estimates_email": "estimates@carolinacommercialfinishes.com",
    "address": "3308 Chancellor Lane, Monroe, NC 28110",
    "city": "Monroe",
    "state": "NC",
    "zip": "28110",
    "insurance_gl": "$1M per occurrence / $2M aggregate",
    "insurance_wc": "Per NC state requirements",
    "warranty_years": 1,
    "change_order_rate": 42.00,  # $/hr T&M
    "payment_terms": "Net 30",
    "proposal_validity_days": 30,
}

# Standard exclusions for painting proposals
STANDARD_EXCLUSIONS = [
    "Electrostatic painting of any kind",
    "Floor painting / traffic markings / epoxy floor systems",
    "ACT / drop ceiling painting or replacement",
    "Pressure washing (GC scope)",
    "Drywall repair beyond minor patching",
    "Lead / asbestos abatement",
    "Scaffolding or swing stage (priced separately if required)",
    "Storefront glazing or window work",
    "Signage fabrication or installation",
]

STANDARD_INCLUSIONS = [
    "All labor, materials, and equipment for scope described above",
    "Sherwin-Williams paint products per National Account pricing",
    "Surface preparation — sanding, caulking, patching, spot priming",
    "Masking and protection of adjacent surfaces and fixtures",
    "Daily cleanup and protection of completed work",
    "Touch-up and final punch list",
    "Project supervision and quality control",
    "OSHA-compliant safety program",
    f"General Liability insurance ({CCF['insurance_gl']})",
    f"Workers' Compensation insurance ({CCF['insurance_wc']})",
]


class ProposalAgent:
    """Generates professional proposals and GC submission emails."""

    def __init__(self, pipeline_state=None):
        self.pipeline = pipeline_state

    # ==================================================================
    #  MAIN: Draft Proposal
    # ==================================================================

    def draft_proposal(self, slug: str) -> str:
        """
        Generate a full HTML proposal from estimate + SOW.
        Saves: proposal.html, proposal_draft.md, gc_email_draft.txt
        Returns status message for the owner.
        """
        proj_dir = PROJECTS_DIR / slug
        est_path = proj_dir / "estimate.json"
        sow_path = proj_dir / "sow.json"

        if not est_path.exists():
            return f"No estimate found for {slug}. Complete estimating first."

        estimate = json.loads(est_path.read_text(encoding="utf-8"))
        sow = {}
        if sow_path.exists():
            sow = json.loads(sow_path.read_text(encoding="utf-8"))

        proj = self.pipeline.get_project(slug) if self.pipeline else {}
        if not proj:
            proj = {}

        total = estimate.get("summary", {}).get("total_bid", 0)

        # Generate HTML proposal
        html = self._build_html_proposal(slug, proj, estimate, sow)
        html_path = proj_dir / "proposal.html"
        html_path.write_text(html, encoding="utf-8")

        # Generate markdown (for WhatsApp / quick review)
        md = self._build_proposal_md(slug, proj, estimate, sow)
        md_path = proj_dir / "proposal_draft.md"
        md_path.write_text(md, encoding="utf-8")

        # Generate GC email draft
        email = self._build_gc_email(slug, proj, estimate)
        email_path = proj_dir / "gc_email_draft.txt"
        email_path.write_text(email, encoding="utf-8")

        # Try PDF generation
        pdf_status = self._generate_pdf(html_path, proj_dir / "proposal.pdf")

        if self.pipeline:
            self.pipeline.update_stage(slug, "proposal_ready")

        return (
            f"Proposal drafted for {proj.get('name', slug)}\n"
            f"Total bid: ${total:,.2f}\n\n"
            f"Files saved:\n"
            f"  proposal.html — Full formatted proposal\n"
            f"  proposal_draft.md — Text version\n"
            f"  gc_email_draft.txt — Email to GC\n"
            f"  {pdf_status}\n\n"
            f"Review the proposal, then say:\n"
            f"  \"send proposal {proj.get('name', slug)}\" to email it to the GC"
        )

    # ==================================================================
    #  HTML PROPOSAL BUILDER
    # ==================================================================

    def _build_html_proposal(self, slug: str, proj: dict, estimate: dict, sow: dict) -> str:
        """Build full HTML proposal matching CCF template style."""
        s = estimate.get("summary", {})
        now = datetime.now()
        total = s.get("total_bid", 0)

        project_name = proj.get("name", slug)
        gc_name = proj.get("gc", "General Contractor")
        location = f"{proj.get('city', '')}, {proj.get('state', '')}"
        due_date = proj.get("due_date", "")

        # Determine scope categories and subtotals
        painting_total, wallcovering_total, exterior_total = self._calculate_subtotals(estimate, sow)

        # Build line items for scope tables
        interior_items = [i for i in estimate.get("line_items", [])
                          if "exterior" not in i.get("task_code", "").lower()
                          and "wallcovering" not in i.get("task_code", "").lower()]
        exterior_items = [i for i in estimate.get("line_items", [])
                          if "exterior" in i.get("task_code", "").lower()]
        wallcovering_items = [i for i in estimate.get("line_items", [])
                              if "wallcovering" in i.get("task_code", "").lower()]

        # Build scope overview bullets
        scope_bullets = self._build_scope_overview(sow, estimate)

        # Build inclusions/exclusions
        inclusions = self._build_inclusions(sow, proj)
        exclusions = self._build_exclusions(sow)
        notes = self._build_notes(sow, proj)

        # Estimate duration
        labor_hours = s.get("labor_hours", 0)
        crew_size = 3
        hours_per_day = 8
        work_days = max(1, math.ceil(labor_hours / (crew_size * hours_per_day)))

        # Build the HTML
        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Proposal — {project_name}</title>
<style>
{self._get_css()}
</style>
</head>
<body>

<!-- PAGE 1: COVER -->
<div class="page">
  <div class="header-bar">
    <div class="proposal-for">P R O P O S A L &nbsp; F O R</div>
  </div>

  <h1 class="project-title">{self._html_escape(project_name)}</h1>
  <p class="subtitle">Painting{' & Wallcovering' if wallcovering_total > 0 else ''} Services</p>

  <div class="info-block">
    <table class="info-table">
      <tr><td class="label">PROJECT</td><td>{self._html_escape(project_name)}</td></tr>
      <tr><td class="label">LOCATION</td><td>{self._html_escape(location)}</td></tr>
      <tr><td class="label">PREPARED FOR</td><td>{self._html_escape(gc_name)}</td></tr>
      <tr><td class="label">DATE</td><td>{now.strftime('%B %d, %Y')}</td></tr>
      <tr><td class="label">PREPARED BY</td><td>{CCF['company']}</td></tr>
      <tr><td class="label">VALID FOR</td><td>{CCF['proposal_validity_days']} Days</td></tr>
    </table>
  </div>

  <h2>PROPOSAL SUMMARY</h2>
  <table class="summary-table">
    <thead>
      <tr><th class="scope-col">SCOPE</th><th class="amount-col">AMOUNT</th></tr>
    </thead>
    <tbody>
      {'<tr><td>Painting</td><td class="amount">${:,.2f}</td></tr>'.format(painting_total) if painting_total > 0 else ''}
      {'<tr><td>Exterior Painting</td><td class="amount">${:,.2f}</td></tr>'.format(exterior_total) if exterior_total > 0 else ''}
      {'<tr><td>Wallcovering</td><td class="amount">${:,.2f}</td></tr>'.format(wallcovering_total) if wallcovering_total > 0 else ''}
      <tr class="total-row"><td><strong>TOTAL</strong></td><td class="amount"><strong>${total:,.2f}</strong></td></tr>
    </tbody>
  </table>

  <h2>SCOPE OVERVIEW</h2>
  <ul class="scope-overview">
    {''.join(f'<li>{self._html_escape(b)}</li>' for b in scope_bullets)}
  </ul>

  {self._footer()}
</div>

<!-- PAGE 2: DETAILED SCOPE -->
<div class="page">
  <div class="page-header">CAROLINA COMMERCIAL FINISHES<span class="page-num">Page 2</span></div>

  <h2>SCOPE OF WORK — PAINTING</h2>

  {self._build_walls_table(sow, interior_items)}

  {self._build_ceilings_table(sow)}

  {self._build_doors_table(sow)}

  {self._build_exterior_table(sow, exterior_items) if exterior_items or sow.get('exterior_painting', {}).get('surfaces') else ''}

  {self._footer()}
</div>

<!-- PAGE 3: WALLCOVERING + PREP -->
{self._build_wallcovering_page(sow, wallcovering_items) if wallcovering_items or sow.get('wallcovering', {}).get('locations') else ''}

<!-- PAGE 4: INCLUSIONS / EXCLUSIONS / NOTES -->
<div class="page">
  <div class="page-header">CAROLINA COMMERCIAL FINISHES<span class="page-num">Page {4 if wallcovering_items else 3}</span></div>

  <h2>INCLUSIONS</h2>
  <ul class="check-list">
    {''.join(f'<li>{self._html_escape(inc)}</li>' for inc in inclusions)}
  </ul>

  <h2>EXCLUSIONS</h2>
  <ul class="x-list">
    {''.join(f'<li>{self._html_escape(exc)}</li>' for exc in exclusions)}
  </ul>

  <h2>NOTES & ASSUMPTIONS</h2>
  <ol class="notes-list">
    {''.join(f'<li>{self._html_escape(n)}</li>' for n in notes)}
  </ol>

  {self._footer()}
</div>

<!-- PAGE 5: TERMS & ACCEPTANCE -->
<div class="page">
  <div class="page-header">CAROLINA COMMERCIAL FINISHES<span class="page-num">Page {5 if wallcovering_items else 4}</span></div>

  <h2>TERMS & CONDITIONS</h2>

  <div class="terms">
    <h3>Payment Terms</h3>
    <p>{CCF['payment_terms']} from date of invoice. Progress billing on projects exceeding 2 weeks.</p>

    <h3>Proposal Validity</h3>
    <p>This proposal is valid for {CCF['proposal_validity_days']} days from the date shown on Page 1.</p>

    <h3>Warranty</h3>
    <p>Carolina Commercial Finishes warrants all workmanship for {CCF['warranty_years']} year(s) from date of substantial completion. Paint manufacturer warranty applies separately. Warranty does not cover damage caused by others, abuse, or normal wear and tear.</p>

    <h3>Insurance</h3>
    <p>General Liability {CCF['insurance_gl']}. Workers' Compensation {CCF['insurance_wc']}. Certificates of Insurance provided upon request.</p>

    <h3>Change Orders</h3>
    <p>Any work not included in this proposal will be priced and approved in writing before commencement. Change orders billed at ${CCF['change_order_rate']:.2f}/hr (T&M) plus materials.</p>

    <h3>Schedule</h3>
    <p>Work schedule to be coordinated with General Contractor. Estimated duration: {work_days} working days with a {crew_size}-person crew (subject to site access and sequencing).</p>

    <h3>Access</h3>
    <p>Continuous, unobstructed access to all work areas is required. Any delays due to access restrictions may result in schedule and cost adjustments.</p>

    <h3>Dispute Resolution</h3>
    <p>Any disputes shall first be addressed through good-faith negotiation, then mediation in the state of North Carolina, before either party pursues legal remedies.</p>
  </div>

  <div class="acceptance-bar">ACCEPTANCE & AUTHORIZATION</div>
  <p>By signing below, the parties agree to the scope, pricing, and terms outlined in this proposal for the total contract amount of <strong>${total:,.2f}</strong>.</p>

  <div class="signature-block">
    <div class="sig-col">
      <div class="sig-line"></div>
      <p>Authorized Signature — General Contractor</p>
      <div class="sig-line"></div>
      <p>Printed Name / Title</p>
      <div class="sig-line"></div>
      <p>Date</p>
    </div>
    <div class="sig-col">
      <div class="sig-line"></div>
      <p>Authorized Signature — Carolina Commercial Finishes</p>
      <p><strong>{CCF['owner']}</strong></p>
      <div class="sig-line"></div>
      <p>Date</p>
    </div>
  </div>

  <div class="final-footer">
    <strong>{CCF['company']}</strong><br>
    {CCF['legal']}<br>
    {CCF['address']} | {CCF['phone']} | {CCF['email']}
  </div>

  {self._footer()}
</div>

</body>
</html>"""
        return html

    # ------------------------------------------------------------------
    #  Scope Detail Builders
    # ------------------------------------------------------------------

    def _build_walls_table(self, sow: dict, interior_items: list) -> str:
        """Build interior walls scope table."""
        ip = sow.get("interior_painting", {})
        walls = ip.get("walls", []) if isinstance(ip, dict) else []

        if not walls and not interior_items:
            return ""

        rows = ""
        total_sf = 0

        # Use SOW wall data if available (room-by-room)
        if walls:
            for w in walls:
                sf = w.get("estimated_sf", 0)
                total_sf += sf
                area = self._html_escape(w.get("area", "General"))
                finish = w.get("finish", "")
                method = w.get("method", "")
                coats = w.get("coats", "")
                system = finish or f"{method}, {coats}" if method else coats
                rows += f"<tr><td>{area}</td><td class='num'>{sf:,.0f} SF</td><td>{self._html_escape(system)}</td></tr>\n"
        else:
            # Fall back to estimate line items
            for item in interior_items:
                if "wall" in item.get("area", "").lower() or "wall" in item.get("task_code", ""):
                    sf = item.get("quantity", 0)
                    total_sf += sf
                    rows += f"<tr><td>{self._html_escape(item.get('area', ''))}</td><td class='num'>{sf:,.0f} {item.get('unit', 'SF')}</td><td>{item.get('method', '')}</td></tr>\n"

        if not rows:
            return ""

        return f"""
  <h3>Interior Walls</h3>
  <table class="scope-table">
    <thead><tr><th>AREA / DESCRIPTION</th><th>SF</th><th>SYSTEM / METHOD</th></tr></thead>
    <tbody>
      {rows}
      <tr class="subtotal"><td><strong>Total Interior Wall SF</strong></td><td class='num'><strong>{total_sf:,.0f}</strong></td><td></td></tr>
    </tbody>
  </table>"""

    def _build_ceilings_table(self, sow: dict) -> str:
        ip = sow.get("interior_painting", {})
        ceilings = ip.get("ceilings", []) if isinstance(ip, dict) else []
        if not ceilings:
            return ""

        rows = ""
        total_sf = 0
        for c in ceilings:
            sf = c.get("estimated_sf", 0)
            total_sf += sf
            rows += f"<tr><td>{self._html_escape(c.get('area', ''))}</td><td class='num'>{sf:,.0f} SF</td><td>{self._html_escape(c.get('type', ''))}</td><td>{c.get('method', '')}</td></tr>\n"

        return f"""
  <h3>Ceilings</h3>
  <table class="scope-table">
    <thead><tr><th>AREA</th><th>SF</th><th>TYPE</th><th>METHOD</th></tr></thead>
    <tbody>
      {rows}
      <tr class="subtotal"><td><strong>Total Ceiling SF</strong></td><td class='num'><strong>{total_sf:,.0f}</strong></td><td></td><td></td></tr>
    </tbody>
  </table>"""

    def _build_doors_table(self, sow: dict) -> str:
        ip = sow.get("interior_painting", {})
        doors = ip.get("doors", {}) if isinstance(ip, dict) else {}
        frames = ip.get("frames", {}) if isinstance(ip, dict) else {}

        if not isinstance(doors, dict) or not doors.get("count"):
            return ""

        rows = ""
        door_count = doors.get("count", 0)
        door_type = doors.get("type", "standard")
        rows += f"<tr><td>HM Doors (2 sides + frame)</td><td class='num'>{door_count} EA</td><td>2 coats</td></tr>\n"

        if isinstance(frames, dict) and frames.get("count") and frames["count"] != door_count:
            rows += f"<tr><td>Door Frames</td><td class='num'>{frames['count']} EA</td><td>2 coats</td></tr>\n"

        return f"""
  <h3>Doors & Frames ({door_count} Units)</h3>
  <table class="scope-table">
    <thead><tr><th>ITEM</th><th>QTY</th><th>SYSTEM</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>"""

    def _build_exterior_table(self, sow: dict, exterior_items: list) -> str:
        ep = sow.get("exterior_painting", {})
        surfaces = ep.get("surfaces", []) if isinstance(ep, dict) else []

        rows = ""
        if surfaces:
            for surf in surfaces:
                sf = surf.get("estimated_sf", 0)
                rows += f"<tr><td>{self._html_escape(surf.get('area', ''))}</td><td class='num'>{sf:,.0f} SF</td><td>{surf.get('method', '')}</td></tr>\n"
        else:
            for item in exterior_items:
                rows += f"<tr><td>{self._html_escape(item.get('area', ''))}</td><td class='num'>{item.get('quantity', 0):,.0f} {item.get('unit', 'SF')}</td><td>{item.get('method', '')}</td></tr>\n"

        if not rows:
            return ""

        return f"""
  <h3>Exterior Painting</h3>
  <table class="scope-table">
    <thead><tr><th>ITEM / DESCRIPTION</th><th>QTY</th><th>SYSTEM</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>"""

    def _build_wallcovering_page(self, sow: dict, wc_items: list) -> str:
        wc = sow.get("wallcovering", {})
        locations = wc.get("locations", []) if isinstance(wc, dict) else []

        rows = ""
        if locations:
            for loc in locations:
                sf = loc.get("estimated_sf", 0)
                rows += f"<tr><td>{self._html_escape(loc.get('area', ''))}</td><td class='num'>{sf:,.0f} SF</td><td>{self._html_escape(loc.get('type', 'Type II Vinyl'))}</td></tr>\n"
        elif wc_items:
            for item in wc_items:
                rows += f"<tr><td>{self._html_escape(item.get('area', ''))}</td><td class='num'>{item.get('quantity', 0):,.0f} SF</td><td>{item.get('method', 'install')}</td></tr>\n"

        if not rows:
            return ""

        return f"""
<div class="page">
  <div class="page-header">CAROLINA COMMERCIAL FINISHES<span class="page-num">Page 3</span></div>

  <h2>WALLCOVERING SCOPE</h2>
  <table class="scope-table">
    <thead><tr><th>ITEM</th><th>SF</th><th>TYPE</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>

  <h2>PREP & MISCELLANEOUS</h2>
  <table class="scope-table">
    <thead><tr><th>ITEM</th></tr></thead>
    <tbody>
      <tr><td>Surface prep — scrape, sand, patch, caulk all areas</td></tr>
      <tr><td>Masking & protection</td></tr>
      <tr><td>MEP painting — exposed pipes, conduit, equipment per spec</td></tr>
      <tr><td>Touch-up & punch list</td></tr>
      <tr><td>Mobilization / demobilization</td></tr>
      <tr><td>Equipment (airless sprayer, scissor lift as needed)</td></tr>
    </tbody>
  </table>

  {self._footer()}
</div>"""

    # ------------------------------------------------------------------
    #  Content Builders
    # ------------------------------------------------------------------

    def _build_scope_overview(self, sow: dict, estimate: dict) -> list:
        bullets = []
        ip = sow.get("interior_painting", {})
        summary = sow.get("summary", {})

        if isinstance(ip, dict):
            walls = ip.get("walls", [])
            if walls:
                total_wall_sf = sum(w.get("estimated_sf", 0) for w in walls)
                if total_wall_sf:
                    bullets.append(f"Interior wall painting — {len(walls)} areas (~{total_wall_sf:,.0f} SF)")
                else:
                    bullets.append(f"Interior wall painting — {len(walls)} areas")

            ceilings = ip.get("ceilings", [])
            if ceilings:
                total_ceil_sf = sum(c.get("estimated_sf", 0) for c in ceilings)
                if total_ceil_sf:
                    bullets.append(f"Ceiling painting — {len(ceilings)} areas (~{total_ceil_sf:,.0f} SF)")

            doors = ip.get("doors", {})
            if isinstance(doors, dict) and doors.get("count"):
                bullets.append(f"Door and frame painting — {doors['count']} units")

            trim = ip.get("trim", [])
            if trim:
                total_lf = sum(t.get("lf", 0) for t in trim)
                if total_lf:
                    bullets.append(f"Trim / base painting (~{total_lf:,.0f} LF)")

        ep = sow.get("exterior_painting", {})
        if isinstance(ep, dict) and ep.get("surfaces"):
            total_ext_sf = sum(s.get("estimated_sf", 0) for s in ep["surfaces"])
            if total_ext_sf:
                bullets.append(f"Exterior painting (~{total_ext_sf:,.0f} SF)")
            else:
                bullets.append("Exterior painting")

        wc = sow.get("wallcovering", {})
        if isinstance(wc, dict) and wc.get("locations"):
            total_wc_sf = sum(l.get("estimated_sf", 0) for l in wc["locations"])
            if total_wc_sf:
                bullets.append(f"Commercial wallcovering installation ({total_wc_sf:,.0f} SF)")

        bullets.append("Surface preparation, masking, protection, and equipment")
        bullets.append(f"Mobilization and demobilization ({CCF['city']}, {CCF['state']})")

        # Special conditions
        sc = sow.get("special_conditions", {})
        if isinstance(sc, dict):
            if sc.get("night_work"):
                bullets.append("Night work schedule per project requirements")
            if sc.get("prevailing_wage"):
                bullets.append("Prevailing wage / Davis-Bacon rates applied")

        return bullets

    def _build_inclusions(self, sow: dict, proj: dict) -> list:
        inclusions = list(STANDARD_INCLUSIONS)

        sc = sow.get("special_conditions", {})
        if isinstance(sc, dict):
            if sc.get("night_work"):
                inclusions.append("Night work schedule per project requirements")
            if sc.get("high_work"):
                inclusions.append("Scissor lift rental for elevated work")

        return inclusions

    def _build_exclusions(self, sow: dict) -> list:
        exclusions = list(STANDARD_EXCLUSIONS)

        # Add SOW-specific exclusions
        sow_excl = sow.get("exclusions", [])
        if isinstance(sow_excl, list):
            for ex in sow_excl:
                if ex not in exclusions:
                    exclusions.append(ex)

        return exclusions

    def _build_notes(self, sow: dict, proj: dict) -> list:
        notes = [
            "Pricing based on bid documents, takeoff data, and SOW analysis.",
            "All paint products from Sherwin-Williams per National Account program.",
            f"Pricing valid for {CCF['proposal_validity_days']} days from date of proposal.",
            f"Payment terms: {CCF['payment_terms']}.",
        ]

        sc = sow.get("special_conditions", {})
        if isinstance(sc, dict):
            if sc.get("night_work"):
                notes.append("Sales area painting is night work only (10 PM - 8 AM) — active operating store.")
            if sc.get("occupied"):
                notes.append("All work coordinated with building management in occupied building.")
            if sc.get("phased"):
                notes.append("Phased work — additional mobilization may be required per phase.")
            if sc.get("notes"):
                notes.append(sc["notes"])

        notes.append("Additional work beyond stated scope will require written change order.")
        return notes

    def _calculate_subtotals(self, estimate: dict, sow: dict) -> tuple:
        """Split total bid into painting / wallcovering / exterior subtotals."""
        painting = 0
        wallcovering = 0
        exterior = 0

        for item in estimate.get("line_items", []):
            total = item.get("line_total", 0)
            code = item.get("task_code", "")

            if "wallcovering" in code:
                wallcovering += total
            elif "exterior" in code:
                exterior += total
            else:
                painting += total

        # Apply proportional overhead + profit
        s = estimate.get("summary", {})
        direct = s.get("direct_cost", 1)
        total_bid = s.get("total_bid", 0)
        markup = total_bid / direct if direct > 0 else 1.3

        painting = round(painting * markup, 2)
        wallcovering = round(wallcovering * markup, 2)
        exterior = round(exterior * markup, 2)

        # Adjust rounding
        diff = total_bid - (painting + wallcovering + exterior)
        painting += diff

        if exterior == 0 and wallcovering == 0:
            painting = total_bid

        return painting, wallcovering, exterior

    # ------------------------------------------------------------------
    #  GC Email
    # ------------------------------------------------------------------

    def _build_gc_email(self, slug: str, proj: dict, estimate: dict) -> str:
        total = estimate.get("summary", {}).get("total_bid", 0)
        name = proj.get("name", slug)
        gc = proj.get("gc", "")
        now = datetime.now()

        return (
            f"Subject: {name} — Painting Proposal — Carolina Commercial Finishes\n\n"
            f"{now.strftime('%B %d, %Y')}\n\n"
            f"{gc}\n\n"
            f"RE: {name}\n\n"
            f"Dear Estimating Team,\n\n"
            f"Thank you for the opportunity to provide this proposal for Painting"
            f"{' and Wallcovering' if any('wallcovering' in i.get('task_code', '') for i in estimate.get('line_items', [])) else ''}"
            f" services on the above-referenced project. "
            f"Carolina Commercial Finishes is pleased to submit the following for your review.\n\n"
            f"TOTAL PROPOSAL AMOUNT: ${total:,.2f}\n\n"
            f"Key conditions:\n"
            f"  - This proposal is valid for {CCF['proposal_validity_days']} days\n"
            f"  - {CCF['payment_terms']} payment terms\n"
            f"  - {CCF['warranty_years']}-year workmanship warranty\n"
            f"  - GL insurance: {CCF['insurance_gl']}\n\n"
            f"Please find our detailed proposal attached. We welcome any questions "
            f"and would appreciate knowing the anticipated award date.\n\n"
            f"Best regards,\n\n"
            f"{CCF['owner']}\n"
            f"{CCF['title']}\n"
            f"{CCF['company']}\n"
            f"{CCF['phone']}\n"
            f"{CCF['email']}\n"
        )

    def draft_gc_email(self, slug: str, gc_contact: str = "", gc_email: str = "") -> str:
        """Public method to draft/redraft GC email."""
        proj_dir = PROJECTS_DIR / slug
        est_path = proj_dir / "estimate.json"
        if not est_path.exists():
            return "No estimate found."

        estimate = json.loads(est_path.read_text(encoding="utf-8"))
        proj = self.pipeline.get_project(slug) if self.pipeline else {}
        if not proj:
            proj = {}

        email = self._build_gc_email(slug, proj, estimate)
        email_path = proj_dir / "gc_email_draft.txt"
        email_path.write_text(email, encoding="utf-8")
        return f"GC email drafted. Saved to {email_path}"

    # ------------------------------------------------------------------
    #  Markdown Proposal (for WhatsApp / text)
    # ------------------------------------------------------------------

    def _build_proposal_md(self, slug: str, proj: dict, estimate: dict, sow: dict) -> str:
        s = estimate.get("summary", {})
        now = datetime.now()
        total = s.get("total_bid", 0)
        project_name = proj.get("name", slug)
        gc = proj.get("gc", "")
        painting, wallcovering, exterior = self._calculate_subtotals(estimate, sow)

        lines = [
            f"# PROPOSAL FOR",
            f"## {project_name}",
            f"### Painting{' & Wallcovering' if wallcovering > 0 else ''} Services",
            f"",
            f"**Prepared for:** {gc}",
            f"**Date:** {now.strftime('%B %d, %Y')}",
            f"**Prepared by:** {CCF['company']}",
            f"",
            f"---",
            f"",
            f"## PROPOSAL SUMMARY",
            f"",
        ]

        if painting > 0:
            lines.append(f"| Painting | ${painting:,.2f} |")
        if exterior > 0:
            lines.append(f"| Exterior Painting | ${exterior:,.2f} |")
        if wallcovering > 0:
            lines.append(f"| Wallcovering | ${wallcovering:,.2f} |")
        lines.append(f"| **TOTAL** | **${total:,.2f}** |")
        lines.append("")

        # Scope overview
        lines.append("## SCOPE OVERVIEW")
        for bullet in self._build_scope_overview(sow, estimate):
            lines.append(f"- {bullet}")
        lines.append("")

        # Line items
        lines.append("## SCOPE DETAIL")
        for item in estimate.get("line_items", []):
            lines.append(f"- {item.get('area', '?')}: {item.get('quantity', 0):,.0f} {item.get('unit', 'SF')}")
        lines.append("")

        # Inclusions/Exclusions
        lines.append("## INCLUSIONS")
        for inc in self._build_inclusions(sow, proj)[:8]:
            lines.append(f"- {inc}")
        lines.append("")
        lines.append("## EXCLUSIONS")
        for exc in self._build_exclusions(sow)[:8]:
            lines.append(f"- {exc}")
        lines.append("")

        # Terms
        lines.extend([
            "## TERMS",
            f"- Payment: {CCF['payment_terms']}",
            f"- Valid: {CCF['proposal_validity_days']} days",
            f"- Warranty: {CCF['warranty_years']} year workmanship",
            f"- Change orders: ${CCF['change_order_rate']:.0f}/hr T&M",
            "",
            "---",
            f"**{CCF['company']}**",
            f"{CCF['legal']}",
            f"{CCF['address']} | {CCF['phone']} | {CCF['email']}",
        ])

        return "\n".join(lines)

    # ------------------------------------------------------------------
    #  PDF Generation
    # ------------------------------------------------------------------

    def _generate_pdf(self, html_path: Path, pdf_path: Path) -> str:
        """Try generating PDF from HTML using weasyprint."""
        try:
            import weasyprint
            doc = weasyprint.HTML(filename=str(html_path))
            doc.write_pdf(str(pdf_path))
            return f"proposal.pdf — PDF generated"
        except ImportError:
            # Try command-line weasyprint
            try:
                result = subprocess.run(
                    ["weasyprint", str(html_path), str(pdf_path)],
                    capture_output=True, text=True, timeout=30,
                )
                if result.returncode == 0:
                    return f"proposal.pdf — PDF generated"
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass
            return "(PDF not generated — install weasyprint: pip install weasyprint)"

    # ------------------------------------------------------------------
    #  CSS Stylesheet (matching CCF navy/gold template)
    # ------------------------------------------------------------------

    @staticmethod
    def _get_css() -> str:
        return """
@page { size: letter; margin: 0.75in; }
body { font-family: 'Segoe UI', Calibri, Arial, sans-serif; font-size: 10pt; color: #1a1a1a; line-height: 1.5; margin: 0; }

.page { page-break-after: always; padding: 20px 0; }
.page:last-child { page-break-after: auto; }

/* Header bar */
.header-bar { background: #1B2A4A; color: white; padding: 30px 40px 20px; text-align: center; margin: -20px -0px 30px; }
.proposal-for { font-size: 12pt; letter-spacing: 6px; font-weight: 300; }

.page-header { background: #1B2A4A; color: white; padding: 8px 20px; font-weight: bold; font-size: 10pt; margin: -20px 0 20px; display: flex; justify-content: space-between; }
.page-num { float: right; }

/* Title */
.project-title { text-align: center; font-size: 24pt; font-weight: bold; color: #1B2A4A; margin: 10px 0 5px; }
.subtitle { text-align: center; font-size: 13pt; color: #666; font-style: italic; margin-bottom: 30px; }

/* Info block */
.info-block { margin: 20px 0 30px; }
.info-table { border-collapse: collapse; width: 100%; }
.info-table td { padding: 6px 10px; border: none; }
.info-table .label { font-weight: bold; color: #1B2A4A; width: 160px; text-transform: uppercase; font-size: 9pt; }

/* Summary table */
.summary-table { width: 100%; border-collapse: collapse; margin: 15px 0 30px; }
.summary-table th { background: #1B2A4A; color: white; padding: 10px 15px; text-align: left; font-size: 9pt; text-transform: uppercase; }
.summary-table td { padding: 10px 15px; border-bottom: 1px solid #ddd; }
.summary-table .scope-col { width: 70%; }
.summary-table .amount-col { width: 30%; text-align: right; }
.summary-table .amount { text-align: right; font-family: 'Consolas', monospace; }
.summary-table .total-row { background: #f0f4f8; }
.summary-table .total-row td { border-top: 2px solid #1B2A4A; font-size: 12pt; }

/* Scope tables */
h2 { color: #1B2A4A; font-size: 14pt; border-bottom: 3px solid #C5963A; padding-bottom: 5px; margin-top: 25px; }
h3 { color: #1B2A4A; font-size: 11pt; margin-top: 20px; }

.scope-table { width: 100%; border-collapse: collapse; margin: 10px 0 20px; font-size: 9.5pt; }
.scope-table th { background: #1B2A4A; color: white; padding: 7px 10px; text-align: left; font-size: 8.5pt; text-transform: uppercase; }
.scope-table td { padding: 6px 10px; border-bottom: 1px solid #e0e0e0; }
.scope-table tr:nth-child(even) { background: #f8f9fa; }
.scope-table .num { text-align: right; font-family: 'Consolas', monospace; }
.scope-table .subtotal { background: #f0f4f8; font-weight: bold; }

/* Scope overview */
.scope-overview { padding-left: 20px; }
.scope-overview li { margin-bottom: 4px; }

/* Check/X lists */
.check-list { list-style: none; padding-left: 10px; }
.check-list li::before { content: "\\2714 "; color: #2e7d32; margin-right: 8px; }
.check-list li { margin-bottom: 3px; }

.x-list { list-style: none; padding-left: 10px; }
.x-list li::before { content: "\\2718 "; color: #c62828; margin-right: 8px; }
.x-list li { margin-bottom: 3px; }

.notes-list li { margin-bottom: 4px; }

/* Terms */
.terms h3 { font-size: 10pt; color: #333; margin: 12px 0 4px; }
.terms p { margin: 0 0 8px 15px; font-size: 9.5pt; }

/* Acceptance */
.acceptance-bar { background: #1B2A4A; color: white; padding: 10px 20px; font-weight: bold; font-size: 11pt; margin: 30px 0 15px; }
.signature-block { display: flex; gap: 40px; margin: 30px 0; }
.sig-col { flex: 1; }
.sig-line { border-bottom: 1px solid #333; height: 30px; margin-bottom: 5px; }
.sig-col p { font-size: 9pt; color: #666; margin: 2px 0 15px; }

/* Footer */
.page-footer { border-top: 1px solid #ccc; padding-top: 8px; margin-top: 40px; font-size: 8pt; color: #888; display: flex; justify-content: space-between; }
.final-footer { text-align: center; font-size: 9pt; color: #666; margin-top: 40px; padding-top: 15px; border-top: 2px solid #1B2A4A; }

@media print {
  .page { page-break-after: always; }
  body { font-size: 10pt; }
}
"""

    @staticmethod
    def _footer() -> str:
        return f"""
  <div class="page-footer">
    <span>{CCF['legal']} | DBA {CCF['company']}</span>
    <span>{CCF['phone']} | {CCF['email']}</span>
  </div>"""

    @staticmethod
    def _html_escape(text: str) -> str:
        if not text:
            return ""
        return (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))
