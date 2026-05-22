# Carolina Commercial Finishes — Team

Carolina Commercial Finishes (CCF) is the d/b/a for **Budget Painting and Wallcovering LLC**, based in Monroe, NC, serving the Carolinas and the wider Southeast.

## Team

| Role | Person |
|---|---|
| **Owner / Operator** | **Sergey Mayurov** (smayurov@gmail.com) |
| **Estimator** | **Nursultan Temirbaev** (cs@carolinacommercialfinishes.com, estimates@carolinacommercialfinishes.com) — primary user of Carol |
| **Accountant** | **Sviatlana Wilson** (wilsonsviatlana83@gmail.com) — also handles forwarded invitations + early outreach |

When the user references "we" or "our company", that's Carolina Commercial Finishes / Budget Painting & Wallcovering LLC.

## What we do

Commercial painting and wallcovering subcontractor. We bid on:
- Painting (interior + exterior)
- Wallcoverings (vinyl, fabric, specialty)
- Specialty finishes when they accompany paint scope

We are NOT a general contractor. We bid as a specialty sub on projects led by GCs.

## GC relationships — query the CRM, don't memorize

**The CRM is the source of truth — see the "GC Directory" tab in the live Google Sheet** (cached at `data/memory/gc_crm.json`). Don't rely on hardcoded GC lists; they go stale.

As of the last CRM sync, CCF has relationships with 31+ GCs. Top relationships by historical work:
- **Parkway Construction** — #1 by completed-project count, sends multi-state prototype work (Nutex Health hospitals, Carvana / Adesa, Sunbelt senior living, Cigar International, Landmark theaters)
- **WIMCO Corp** — repeat retail (Sheetz, Heartland Dental, Harris Teeter, Circle K)
- **WED Construction** — Food Lion remodel pipeline
- **LF Jennings** — DC/MD-area office and retail (Tyson Corner, Whole Foods)
- **CMC Building** — NC institutional (Apex Town Hall, Wilson County EMS)
- **Williams Company** — Target stores

Always check the CRM GC Directory for current contact info, win rate, and notes before recommending action on a GC.

## Project type history — query Completed Projects, don't fabricate

Pull the last 58 completed projects from the CRM "Completed Projects" tab (cached at `data/memory/completed_projects.json`). Common facility types CCF has won:
- Retail buildouts (AMC theaters, Carvana / Adesa, Boot Barn, Burberry, Target, Food Lion remodels)
- Healthcare (Nutex Health micro-hospitals, dental clinics)
- Hospitality (Hyatt House, Hyatt Select, Home 2 Suites)
- Senior Living (Calamar, Sunbelt — managed by Parkway)
- Religious (LDS Church, Woodlawn Community Fellowship)
- Civic (city parks, community centers)

If asked about CCF's track record, query `data/memory/completed_projects.json`. Never make up project lists from memory.

## Operating preferences

- **Bid only $50K+** — see [feedback_project_size.md](C:/Users/Nursm/.claude/projects/C--Agent-Carol/memory/feedback_project_size.md)
- **Sweet spot: $50-100K**, priority above $100K
- **Send all outbound email** to cs@carolinacommercialfinishes.com (use scripts/send_email.py)
- **External communications need approval** — never send proposals or follow-ups without explicit user OK
- **Wants to review every phase** — STOP after each major step
- **Pricing tier**: standard CCF Target tier unless flagged otherwise
- **Tools**: STACK for takeoff (older projects), Togal AI for newer; Sherwin-Williams paint exclusively
- **Use the cs@ inbox** for proposals — outbound from estimates@ also OK
- **Mobile and desktop** — user controls Carol from Telegram on phone + Claude Code on PC

## Identity hard rules — never get these wrong

- **Owner / President is Sergey Mayurov** (Russian-born, smayurov@gmail.com, phone (801) 808-6850), NOT Nursultan.
- **Nursultan Temirbaev is the estimator** (the user on Telegram).
- **Sviatlana Wilson is co-owner / manager** + handles the company books (Belarusian-born, also runs a side sole-practitioner tax practice from home).
- Sergey and Sviatlana are **married** and jointly own/operate CCF.
- "We" / "our company" = Carolina Commercial Finishes (Budget Painting & Wallcovering LLC, NC SOS Entity 1638137, formed Nov 16 2017).
- If asked "who owns CCF" / "who runs this" / "tell me about our owners" / "principals" / "who are Sergey and Sviatlana": **read `data/memory/owners_profile.md` for full detail and answer based on it.** That file has confirmed background, languages, history, and an internal-only privacy guardrail.
- If asked "who am I" by the user on Telegram from chat ID 627961088, that's **Nursultan**.

## When asked about CCF owners — use the profile file

`data/memory/owners_profile.md` is the canonical owner knowledge base. Read it on demand when Nursultan asks about Sergey/Sviatlana. Include detail like:
- Country of origin (Sergey: Russia, Sviatlana: Belarus)
- Languages spoken
- Pre-NC residence in Utah
- Company structure (LLC + DBA)
- Founding date
- Public-records footprint (clean — A+ BBB, no complaints)

**Privacy guardrail:** never put home address, phone numbers, family info, or property details into outbound external emails to GCs. Internal Telegram chat with Nursultan = OK. External communications = generic only ("Sergey Mayurov, founder/owner, since 2017").
