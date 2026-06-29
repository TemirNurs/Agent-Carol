# CCF AI Estimating Agent — Carol

AI-powered commercial painting estimator for Carolina Commercial Finishes, built on OpenClaw + Claude.

## What It Does

Carol takes GC bid packages (plans, specs, scope letters) and walks you through building a detailed estimate and proposal using CCF's real production rates and pricing policy.

**Workflow:** Ingest Bid Docs → Extract SOW → Takeoff Plan → Accept Quantities → Calculate Estimate → Generate Proposal → Draft Email

**Access:** Web UI (PC), WhatsApp, or Telegram (mobile)

## Quick Start

### Prerequisites
- Node.js 22+ (for OpenClaw)
- Python 3.10+ (for tool scripts)
- Anthropic API key

### Install

```bash
# 1. Install OpenClaw
npm install -g openclaw@latest

# 2. Install Python dependencies
pip install -r requirements.txt

# 3. Set your API key
export ANTHROPIC_API_KEY="sk-ant-..."

# 4. Onboard OpenClaw (first time only)
openclaw onboard --install-daemon

# 5. Start the gateway
openclaw gateway
```

### Or use Docker

```bash
docker build -t ccf-carol .
docker run -p 18789:18789 -p 18793:18793 \
  -e ANTHROPIC_API_KEY="sk-ant-..." \
  ccf-carol
```

### Access

- **Web UI:** http://localhost:18789
- **Canvas (rich forms):** http://localhost:18793
- **WhatsApp:** Scan QR code during `openclaw channels login --channel whatsapp`
- **Telegram:** Create bot via @BotFather, add token to `openclaw.json`

## Running a Sample Estimate

1. Open the web UI or message Carol on WhatsApp
2. Say: "New project: Food Lion 1513, GC: WED Construction, Target tier, due March 18"
3. Upload bid documents (PDFs from `CC Finishes/CCF Templates/`)
4. Carol extracts the SOW — review and approve
5. Carol generates a takeoff plan — measure quantities
6. Paste or upload takeoff data
7. Carol calculates the estimate — review numbers
8. Carol generates the proposal — review and approve
9. Carol drafts the GC email — send when ready

## Project Structure

```
Agent Carol/
├── openclaw.json          # OpenClaw config (Claude + channels)
├── AGENTS.md              # Agent operating rules
├── SOUL.md                # Agent persona
├── USER.md                # User profile
├── skills/
│   └── ccf-estimator/
│       ├── SKILL.md       # Agent workflow definition
│       ├── scripts/       # Python tool scripts
│       │   ├── read_excel.py        # Pricing workbook parser
│       │   ├── estimate_engine.py   # Pure calculation engine
│       │   ├── estimate_to_xlsx.py  # Excel export
│       │   ├── parse_pdf.py         # PDF text extraction
│       │   ├── proposal_gen.py      # Proposal generator
│       │   └── project_store.py     # Project data management
│       └── references/    # Schema docs
├── data/
│   ├── pricing/           # CCF pricing config (from workbook)
│   ├── templates/         # Sample estimate Excel files
│   ├── proposals/         # Sample proposal PDFs
│   └── projects/          # Per-project run data
├── canvas/                # Responsive web UI pages
├── CC Finishes/           # Source data files
├── requirements.txt
└── Dockerfile
```

## Key Design Decisions

- **Estimation logic is pure Python** — `estimate_engine.py` takes quantities + rates, returns costs. No LLM. Testable, auditable, deterministic.
- **LLM only for soft tasks** — SOW extraction, takeoff planning, proposal wording, email drafting.
- **Pricing is config-driven** — Change the Excel workbook, re-run the parser, pricing updates automatically.
- **Phase gates** — Agent always stops between phases for user approval.
- **Multi-channel** — Same workflow from PC (web) or phone (WhatsApp/Telegram).

## Testing the Estimate Engine Directly

```bash
# Parse pricing workbook to JSON
python skills/ccf-estimator/scripts/read_excel.py \
  --file "CC Finishes/CCF-Pricing-Policy-Production-Rates.xlsx" \
  --section all > data/pricing/ccf-pricing-config.json

# Run estimate with sample takeoff
python skills/ccf-estimator/scripts/estimate_engine.py \
  --takeoff tests/sample_takeoff.json \
  --pricing data/pricing/ccf-pricing-config.json \
  --tier target --labor-rate 28 --overhead 0.12 --markup 0.20

# Export to Excel
python skills/ccf-estimator/scripts/estimate_to_xlsx.py \
  --estimate estimate.json --output estimate.xlsx

# Generate proposal
python skills/ccf-estimator/scripts/proposal_gen.py \
  --estimate estimate.json --project project.json --email
```

## Extending Later

- **Togal/STACK exports:** Upload CSV takeoff files directly
- **ConstructConnect/BuildingConnected:** Import bid invitations
- **Admin UI:** Change pricing tiers, rates, markup scenarios without touching code
- **Auto-bid:** Carol monitors new bids and auto-starts estimates
