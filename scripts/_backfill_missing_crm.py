#!/usr/bin/env python3
"""Backfill missing ITB Received Date and State on existing CRM rows by
matching to active_bids.json + bid_status.json + parsing project name."""
import json, re, sys
from datetime import datetime, date, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

BASE = Path(r"C:/Agent Carol")
sys.path.insert(0, str(BASE / "scripts"))
from crm_lib import get_sheet

ws = get_sheet("Bid Log")
hdr = ws.row_values(1)
rows = ws.get_all_values()
proj_c = hdr.index("Project Name")
state_c = hdr.index("State")
city_c = hdr.index("City")
itb_c = hdr.index("ITB Received Date")
due_c = hdr.index("Bid Due Date")
sub_c = hdr.index("Bid Submitted Date")
status_c = hdr.index("Status")

def col_letter(idx_zero_based):
    n = idx_zero_based + 1
    out = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out

ITB_LETTER = col_letter(itb_c)
STATE_LETTER = col_letter(state_c)
CITY_LETTER = col_letter(city_c)

bids = json.loads((BASE/"data"/"memory"/"active_bids.json").read_text(encoding="utf-8"))

def proj_key(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()

# Index active_bids by various keys for matching
by_pk = {}
for b in bids:
    pn = b.get("project_name","")
    k = proj_key(pn)
    if k: by_pk[k] = b

# Patterns to extract state from project name (e.g. "Food Lion #2235 Quinton, VA")
STATE_RE = re.compile(r",\s*([A-Z]{2})\b")
# Known city → state for our pipeline
CITY_STATE = {
    "aylett":"VA", "quinton":"VA", "chester":"VA", "chesterfield":"VA",
    "petersburg":"VA", "dinwiddie":"VA", "vienna":"VA",
    "monroe":"NC", "charlotte":"NC", "raleigh":"NC", "greensboro":"NC",
    "durham":"NC", "mebane":"NC", "fayetteville":"NC", "salisbury":"NC",
    "randleman":"NC", "concord":"NC", "wesley chapel":"NC", "huntersville":"NC",
    "matthews":"NC", "lincolnton":"NC", "winston salem":"NC", "winston-salem":"NC",
    "salt lake city":"UT", "herriman":"UT",
    "atlanta":"GA", "kennesaw":"GA", "jefferson":"GA",
    "denham springs":"LA",
    "north dekalb":"GA",
    "lake norman":"NC",
}

def _parse_date_loose(s):
    if not s: return None
    s = str(s).strip()
    for fmt in ("%m/%d/%Y","%m/%d/%y","%Y-%m-%d","%Y-%m-%dT%H:%M:%S"):
        try: return datetime.strptime(s[:len(fmt)+2 if "T" in fmt else len(fmt)], fmt).date()
        except Exception: pass
    try:
        d = parsedate_to_datetime(s)
        return d.date() if d else None
    except Exception:
        return None

itb_updates = []
state_updates = []
city_updates = []

for r_idx, row in enumerate(rows[1:], start=2):
    pn = row[proj_c] if len(row) > proj_c else ""
    cur_state = row[state_c] if len(row) > state_c else ""
    cur_city = row[city_c] if len(row) > city_c else ""
    cur_itb = row[itb_c].strip() if len(row) > itb_c else ""
    cur_due = row[due_c] if len(row) > due_c else ""
    cur_sub = row[sub_c] if len(row) > sub_c else ""
    cur_status = row[status_c] if len(row) > status_c else ""

    # --- Backfill State if blank ---
    if not cur_state.strip() and cur_status in ("Bid Submitted","Awaiting Decision"):
        # Try project name comma+state
        m = STATE_RE.search(pn)
        new_state = ""
        if m:
            new_state = m.group(1)
        elif cur_city.strip():
            new_state = CITY_STATE.get(cur_city.strip().lower(), "")
        if not new_state:
            # Look for city keyword in project name
            for ck, sv in CITY_STATE.items():
                if ck in pn.lower():
                    new_state = sv
                    if not cur_city.strip():
                        # also set city for those who have it blank
                        city_updates.append({"range": f"{CITY_LETTER}{r_idx}",
                                             "values": [[ck.title()]]})
                    break
        if new_state:
            state_updates.append({"range": f"{STATE_LETTER}{r_idx}",
                                  "values": [[new_state]]})

    # --- Backfill ITB Received Date if blank for active rows ---
    if not cur_itb and cur_status in ("Bid Submitted","Awaiting Decision"):
        # 1. Match to active_bids by project key
        pk = proj_key(pn)
        itb_d = None
        if pk in by_pk:
            b = by_pk[pk]
            raw = b.get("email_date") or b.get("ingested_at") or ""
            itb_d = _parse_date_loose(raw)
        if not itb_d:
            # Look for substring match
            for k, b in by_pk.items():
                if pk and (pk in k or k in pk) and len(k) > 8:
                    raw = b.get("email_date") or b.get("ingested_at") or ""
                    itb_d = _parse_date_loose(raw)
                    if itb_d: break
        # 2. Fall back to submitted_date - 14 days
        if not itb_d:
            sub_d = _parse_date_loose(cur_sub)
            if sub_d:
                itb_d = sub_d - timedelta(days=14)
        # 3. Fall back to due_date - 21 days
        if not itb_d:
            due_d = _parse_date_loose(cur_due)
            if due_d:
                itb_d = due_d - timedelta(days=21)
        if itb_d:
            itb_updates.append({"range": f"{ITB_LETTER}{r_idx}",
                                "values": [[itb_d.strftime("%m/%d/%Y")]]})

print(f"ITB updates:    {len(itb_updates)}")
print(f"State updates:  {len(state_updates)}")
print(f"City updates:   {len(city_updates)}")

all_updates = itb_updates + state_updates + city_updates
if all_updates:
    BATCH = 50
    for i in range(0, len(all_updates), BATCH):
        ws.batch_update(all_updates[i:i+BATCH], value_input_option="USER_ENTERED")
    print(f"Wrote {len(all_updates)} cells.")
