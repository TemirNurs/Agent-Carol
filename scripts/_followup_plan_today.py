#!/usr/bin/env python3
"""Build today's follow-up plan: every active bid (Bid Submitted / Awaiting
Decision), grouped by contact, with last-touch date and recommended next-touch
timing per the escalating cadence (72h → 24h → 12h → 8h → 6h)."""
import json, re, sys
from collections import defaultdict
from datetime import datetime, date, timedelta
from pathlib import Path

try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

BASE = Path(r"C:/Agent Carol")
sys.path.insert(0, str(BASE / "scripts"))
from crm_lib import get_sheet

# Pull CRM rows
ws = get_sheet("Bid Log")
rows = ws.get_all_values()
hdr = rows[0]
def col(name): return hdr.index(name)

ACTIVE = ("Bid Submitted", "Awaiting Decision")

# Pull bid_status.json for last-touch dates
bs = json.loads((BASE/"data"/"memory"/"bid_status.json").read_text(encoding="utf-8"))
overrides = bs.get("overrides", {})
history   = bs.get("history", [])

# Last chase email per (project_core, recipient)
def slugify(s):
    s = re.sub(r"[^a-z0-9\s-]", "", (s or "").lower())
    s = re.sub(r"\s+", "-", s).strip("-")
    return re.sub(r"-+", "-", s)[:80]

last_chase = defaultdict(lambda: None)  # (slug, recipient) -> datetime
last_send  = defaultdict(lambda: None)  # slug -> dt of last activity to anyone
attempts   = defaultdict(int)            # (slug, recipient) -> count of chases
for h in history:
    trig = h.get("trigger","")
    if trig not in ("chase","chase_silent","followup","chase_silent_followups"): continue
    slug = h.get("slug","")
    to = (h.get("to_email","") or "").strip().lower()
    at = h.get("at","")
    try: dt = datetime.fromisoformat(at)
    except Exception: continue
    if not slug or not to: continue
    cur = last_chase[(slug, to)]
    if cur is None or dt > cur:
        last_chase[(slug, to)] = dt
    attempts[(slug, to)] += 1
    cur2 = last_send[slug]
    if cur2 is None or dt > cur2:
        last_send[slug] = dt

# Build plan
buckets = defaultdict(list)
for row in rows[1:]:
    if len(row) < len(hdr): continue
    status = row[col("Status")]
    if status not in ACTIVE: continue
    proj = row[col("Project Name")]
    gc   = row[col("GC / Client")]
    contact = row[col("Contact Name")]
    email = (row[col("Contact Email")] or "").strip().lower()
    bid_id = row[col("Bid #")]
    sub_date = row[col("Bid Submitted Date")]
    state = row[col("State")]
    if not email: continue
    slug = slugify(proj)
    key = (slug, email)
    last = last_chase[key]
    n = attempts[key]
    # Submission date
    try: sub_dt = datetime.strptime(sub_date, "%m/%d/%Y")
    except Exception: sub_dt = None
    # Determine bucket
    days_since = None
    if last:
        days_since = (datetime.now() - last).days
    elif sub_dt:
        days_since = (datetime.now() - sub_dt).days
    # Bucket logic
    if status == "Awaiting Decision":
        bucket = "3-decision"
    elif n == 0:
        bucket = "1-first-touch"
    else:
        bucket = "2-rechase"
    buckets[bucket].append({
        "bid_id": bid_id,
        "proj": proj,
        "gc": gc,
        "contact": contact,
        "email": email,
        "status": status,
        "sub_date": sub_date,
        "state": state,
        "last_chase": last.strftime("%m/%d") if last else "(never)",
        "attempts": n,
        "days_since": days_since,
    })

# Group bucket 1 + 2 by recipient for consolidated chase
def emit(name, items):
    print(f"\n{'='*88}\n  {name}  ({len(items)} bid(s))\n{'='*88}")
    by_email = defaultdict(list)
    for it in items:
        by_email[it["email"]].append(it)
    for email, lst in sorted(by_email.items()):
        # Pull contact name from first
        contact = lst[0]["contact"] or email.split("@")[0]
        gc = lst[0]["gc"]
        print(f"\n  → {contact}  <{email}>  ({gc})  [{len(lst)} bid(s)]")
        for it in sorted(lst, key=lambda x: -(x['days_since'] or 0)):
            print(f"      {it['bid_id']}  {it['proj'][:38]:<40}  {it['state']:<3}  "
                  f"sub={it['sub_date']:<10}  last_chase={it['last_chase']:<6}  "
                  f"attempts={it['attempts']}  days_since={it['days_since']}")

print(f"\n{'#'*88}\n  TODAY'S FOLLOW-UP PLAN — {date.today().strftime('%A, %B %d, %Y')}\n{'#'*88}")

# Bucket 1: never chased before — first touch
emit("BUCKET 1 — FIRST CHASE (submitted but never followed up)", buckets["1-first-touch"])

# Bucket 2: chased once+ — escalating cadence
emit("BUCKET 2 — RE-CHASE (already chased, escalating cadence)", buckets["2-rechase"])

# Bucket 3: Awaiting Decision — special handling
emit("BUCKET 3 — DECISION-PENDING (Awaiting Decision status)", buckets["3-decision"])
