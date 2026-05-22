#!/usr/bin/env python3
"""Move the 4 wrongly-attributed Food Lion #1336 submissions from the
food-lion-2235-quinton-va override entry to a proper 1336-food-lion-quinton-va
entry, AND ensure active_bids.json has the #1336 bid so crm_writeback can
add real rows."""
import json, sys
from pathlib import Path
from datetime import datetime

try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

BASE = Path(r"C:/Agent Carol")
BS = BASE / "data" / "memory" / "bid_status.json"
AB = BASE / "data" / "memory" / "active_bids.json"

bs = json.loads(BS.read_text(encoding="utf-8"))
ab = json.loads(AB.read_text(encoding="utf-8"))

# 1. Pull out the wrongly-attached 1336 submissions from the 2235 override
src = bs["overrides"].get("food-lion-2235-quinton-va", {})
wrong = [s for s in src.get("submissions", []) if "#1336" in s.get("subject","")]
right = [s for s in src.get("submissions", []) if "#1336" not in s.get("subject","")]
print(f"Wrong (#1336) submissions wrongly tagged to #2235: {len(wrong)}")
print(f"Correct (non-#1336) submissions kept on #2235:    {len(right)}")
src["submissions"] = right
# Recompute the canonical submitted_* fields from the remaining (correct) subs
if right:
    latest = max(right, key=lambda s: s.get("at",""))
    src["submitted_at"] = latest.get("at","")
    src["submitted_subject"] = latest.get("subject","")
    src["submitted_to"] = latest.get("to","")
bs["overrides"]["food-lion-2235-quinton-va"] = src

# 2. Move them to the 1336 slug
slug = "food-lion-1336-quinton-va"
existing = bs["overrides"].get(slug, {})
# If there's an "1336-food-lion-quinton-va" stub, merge from it too
stub = bs["overrides"].pop("1336-food-lion-quinton-va", {})
if stub:
    for k, v in stub.items():
        existing.setdefault(k, v)
existing.setdefault("submissions", [])
# Replace, dedup by (to, at, subject)
key = lambda s: (s.get("to",""), s.get("at",""), s.get("subject",""))
seen_keys = {key(s) for s in existing["submissions"]}
for s in wrong:
    if key(s) not in seen_keys:
        existing["submissions"].append(s)
        seen_keys.add(key(s))
existing["status"] = "submitted"
existing["project_name"] = "Food Lion #1336 Quinton, VA"
existing["updated_at"] = datetime.now().isoformat(timespec="seconds")
if existing["submissions"]:
    latest = max(existing["submissions"], key=lambda s: s.get("at",""))
    existing["submitted_at"] = latest.get("at","")
    existing["submitted_subject"] = latest.get("subject","")
    existing["submitted_to"] = latest.get("to","")
existing["orphan"] = False
bs["overrides"][slug] = existing
print(f"#1336 slug '{slug}' now has {len(existing['submissions'])} submissions")

# 3. Add a corresponding entry to active_bids.json so crm_writeback can match
has_1336 = any("1336" in (b.get("project_name","")) and "quinton" in b.get("project_name","").lower()
                for b in ab)
if not has_1336:
    ab.append({
        "project_name": "Food Lion #1336 Quinton, VA",
        "gc": "Farris Interior Installation",
        "trade": "Painting & Wallcovering",
        "due_date": "05/19/2026",
        "city": "Quinton",
        "state": "VA",
        "source": "email",
        "source_detail": "Tanner Barber (Farris Interior Installation)",
        "opportunity_id": "",
        "portal_url": "",
        "sf": 0,
        "email_date": "Tue, 05 May 2026 19:43:30 +0000",
        "ingested_at": datetime.now().isoformat(timespec="seconds"),
        "distance_miles": 222,
    })
    print("Added Food Lion #1336 Quinton, VA to active_bids.json")
else:
    print("active_bids.json already has #1336 — skipped add")

BS.write_text(json.dumps(bs, indent=2), encoding="utf-8")
AB.write_text(json.dumps(ab, indent=2), encoding="utf-8")
print("Files saved.")
