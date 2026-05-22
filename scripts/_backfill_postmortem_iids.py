#!/usr/bin/env python3
"""Backfill Internal ID into existing loss_postmortem sidecars by matching
project name + GC against current CRM Bid Log."""
import json, re, sys
from pathlib import Path

try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

BASE = Path(r"C:/Agent Carol")
sys.path.insert(0, str(BASE / "scripts"))
from crm_lib import all_records

DIR = BASE / "data" / "memory" / "loss_postmortems"

# Build {(name_norm, gc_norm): internal_id} from current CRM
def norm(s):
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())

rows = all_records("Bid Log")
sig_to_iid = {}
for r in rows:
    iid = (r.get("Internal ID") or "").strip()
    if not iid: continue
    sig = (norm(r.get("Project Name","")), norm(r.get("GC / Client","")))
    if sig[0]:
        sig_to_iid[sig] = iid

print(f"CRM rows with Internal ID indexed: {len(sig_to_iid)}")

# Scan all .json sidecars
updated = 0
unmatched = 0
for jf in DIR.glob("*.json"):
    try:
        d = json.loads(jf.read_text(encoding="utf-8"))
    except Exception:
        continue
    if d.get("internal_id"):
        continue
    sig = (norm(d.get("name","")), norm(d.get("gc","")))
    iid = sig_to_iid.get(sig)
    if iid:
        d["internal_id"] = iid
        jf.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
        updated += 1
    else:
        unmatched += 1

print(f"Backfilled internal_id: {updated}")
print(f"Unmatched (no CRM row?): {unmatched}")
