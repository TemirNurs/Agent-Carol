# CRM Schema — CRM-Bid-Log Google Sheets workbook

**Status:** Tier 1 + Tier 2-starters as of 2026-05-13.
**Owner:** Nursultan Temirbaev | Edited by Claude Code sessions.

## 🔑 Stable identifiers — what scripts MUST use

| ID | Where | Source of truth | Why it matters |
|---|---|---|---|
| **Internal ID** | Bid Log col AE | UUID4 generated at row insert | Survives sort/insert. **Primary key for every script.** |
| **Contact ID** | Contacts col A | UUID4 | Stable contact reference. |
| **GC ID** | GC Directory col N | UUID4 | Stable GC reference. |
| `Bid #` | Bid Log col A | Live formula `=BID-&TEXT(ROW()-1,"0000")` | **Display label only — shifts every time user sorts.** Never persist this. |

**Rule:** Any script that stores a bid reference across runs (state file, JSON, log) MUST use `Internal ID`, not `Bid #`. Use `crm_lib.find_row_by_internal_id(uuid)` to look up live data.

---

## 📋 Sheets

### 1. **Bid Log** — every submitted/active bid, one row per (project, recipient)

Cols A-AG (33 total):

| Col | Name | Type | Notes |
|---|---|---|---|
| A | Bid # | formula | Display label — DO NOT persist |
| B | Project Name | text | |
| C | City | text | |
| D | State | text | NC/SC/VA/etc. |
| E | Facility Type | text | |
| F | GC / Client | text | Should match `GC Directory.GC / Company` |
| G | Contact Name | text | |
| H | Contact Email | text | **Multi-email cell** (space/comma separated). Primary is first. Split planned. |
| I | Contact Phone | text | |
| J | Bid Source | text | Invitation (GC) / ConstructConnect / BC / etc. |
| K | ITB Received Date | date mm/dd/yyyy | |
| L | Bid Due Date | date mm/dd/yyyy | |
| M | Bid Submitted Date | date mm/dd/yyyy | Used for `Days Until Decision` calc + sort-by Z-A |
| N | Bid Amount ($) | currency | |
| O | Scope Summary | text | Short |
| P | Status | enum | Bid Submitted / Awaiting Decision / Won / Lost / Withdrawn / No Bid / No Decision |
| Q | Award Date | date | |
| R | Contract Value ($) | currency | If won |
| S | Win/Loss | enum | PENDING / WIN / LOSS / WITHDRAWN |
| T | Loss Reason | text | |
| U | Days to Decision | formula | |
| V-AC | FU1-FU4 + dates | mixed | Legacy — being superseded by Activity Log |
| AD | Notes | text | Free-form |
| **AE** | **Internal ID** | UUID | **Primary key — never changes** |
| AF | Days Until Due | formula | `=IF(L<row>="","",INT(L<row>-TODAY()))` |
| AG | Tags | text | Comma-separated: parkway, food-lion, NC, $100K+, active |

### 2. **Activity Log** — every email / call / status change, timestamped

Cols A-J (10 total):

| Col | Name | Notes |
|---|---|---|
| A | Timestamp | ISO 8601 |
| B | Internal ID | FK to Bid Log |
| C | Bid # (snapshot) | Bid# at time of log — display only |
| D | Project (snapshot) | Project Name at time of log |
| E | Type | proposal_sent / follow_up / reply_received / call / note / status_change / bid_invitation / other |
| F | Direction | outbound / inbound / internal |
| G | Counterparty | "Jane Doe <jdoe@example.com>" |
| H | Channel | email / phone / sms / telegram / sheet / system |
| I | Summary | ≤300 chars |
| J | Reference / Link | optional |

**Writer:** `scripts/activity_log.py` (`log_event(...)`).
**Consumers:** chase_silent_followups.py (already wired); send_email.py (TBD); process_followup_replies.py (TBD).

### 3. **Contacts** — one row per person

Cols A-L (12 total):

| Col | Name |
|---|---|
| A | Contact ID (UUID) |
| B | First Name |
| C | Last Name |
| D | Email |
| E | Phone |
| F | Title |
| G | GC / Company |
| H | GC ID (FK → GC Directory) |
| I | Bid Count (running) |
| J | Last Touch |
| K | Status (Active / Inactive / Bounced) |
| L | Notes |

53 unique contacts backfilled from Bid Log on 2026-05-13.

### 4. **GC Directory** — one row per general contractor

Cols A-O (15 total). Pre-existing + 2 new (N, O):

| Col | Name |
|---|---|
| A | GC / Company |
| B | Primary Contact |
| C | Email |
| D | Phone |
| E | City |
| F | State |
| G | Total Bids |
| H | Wins |
| I | Win Rate |
| J | Total Revenue ($) |
| K | Relationship Status |
| L | Last Contact Date |
| M | Notes |
| **N** | **GC ID** (UUID — new 2026-05-13) |
| **O** | **Domain** (extracted from email — new 2026-05-13) |

31 GCs.

### 5. **Dashboard / Completed Projects / Lookups** — unchanged

---

## 🤖 Script reference

### crm_lib.py — canonical helpers

```python
from crm_lib import (
    get_sheet,                  # by name
    workbook,                   # full workbook handle
    new_internal_id,            # generate UUID4 for new row
    find_row_by_internal_id,    # look up row by stable key
    stable_key,                 # canonical key for any row dict
    append_row, append_rows,    # auto-generates Internal ID for Bid Log
    batch_update_rows,          # bulk updates by row_idx
    all_records_with_internal_id,  # iter rows guaranteed-have-IID
)
```

### activity_log.py — append to Activity Log sheet

```python
from activity_log import log_event
log_event(
    internal_id="...uuid...",   # OR bid_id="BID-0042" (will resolve)
    type="follow_up",
    direction="outbound",
    counterparty="estimator@examplegc.com",
    channel="email",
    summary="Chase attempt #4 — firm tone",
)
```

### chase_silent_followups.py — keyed by Internal ID

- State file `data/memory/aggressive_chase_state.json` is now keyed by UUID
- Each fire writes to Activity Log automatically
- `has_replied_since(project_name, contact_email, since_date)` matches by project keywords + sender domain (row-shift safe)
- File lock at `data/memory/chase_silent.lock` prevents concurrent runs

### deadline_alerts.py — Telegram pings for upcoming deadlines

- Reads Bid Log `Bid Due Date`
- Tiers: due-2, due-1, due-0, over-1, over-3
- State at `data/memory/deadline_alerts_sent.json` (dedup per bid per tier)

---

## ✏️ Rules for new scripts

1. **Read by Internal ID, write to Activity Log.** Every meaningful event = one row in Activity Log.
2. **Never persist `Bid #` across runs.** Look it up live for display only.
3. **`append_row(sheet_name="Bid Log", ...)` auto-creates Internal ID.** Just provide the data fields.
4. **Use `crm_lib._retry()` for any gspread call.** Handles 429 rate limits.
5. **Multi-email cells:** use `scripts/chase_silent_followups.first_email()` or split by `[\s,;]+` and pick first valid `@` addr.

---

## 🧱 Coming next (Tier 2 mid + Tier 3)

- Split `Bid Log.Contact Email` into Primary / Secondary / All (with `Contact ID` FK)
- Refactor `send_email.py` + `process_followup_replies.py` to write Activity Log
- Backfill Activity Log from Gmail Sent folder (last 90 days)
- (Tier 3) Migration plan to Airtable if/when 200+ active bids or multi-user editing.
