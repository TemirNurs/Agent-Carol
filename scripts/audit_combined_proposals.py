#!/usr/bin/env python3
"""
audit_combined_proposals.py - Find Sent proposals that covered MULTIPLE stores
in one email, then verify each store has its own CRM row.

Combined emails are common from CCF — the user often submits proposals for
2-3 store numbers in a single email to a multi-store GC (WIMCO, WED, etc.).
track_submissions.py historically only created ONE bid record per email,
missing the secondary stores.

This audit:
  1. Scans Sent folder for proposals in the past N days
  2. For each, parses subject + body to find ALL (store_number, amount) pairs
  3. Compares against CRM — flags any store missing a row
  4. With --apply, inserts the missing rows
"""
from __future__ import annotations
import argparse
import imaplib
import email as email_lib
import json
import os
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from email.header import decode_header
from pathlib import Path

# 5/30 fix — load .env so GMAIL_APP_PASSWORD / API keys are present when
# Carol (OpenClaw/Telegram) shells out to this script. A shelled child does
# NOT inherit the daemon's env, so without this the credential reads below
# return '' and the script fails (e.g. IMAP login). Absolute path → cwd-safe.
try:
    from pathlib import Path as _CCF_P
    from dotenv import load_dotenv as _ccf_load_dotenv
    _ccf_load_dotenv(_CCF_P(__file__).resolve().parent.parent / ".env")
except Exception:
    pass

try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except: pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

GMAIL_USER = os.environ.get("GMAIL_USER", "estimates@carolinacommercialfinishes.com")
GMAIL_PASS = os.environ.get("GMAIL_APP_PASSWORD", "")

# Internal-sender markers: company-domain substrings + team alias addresses,
# sourced from env so no personal handles are hardcoded.
_OWN_DOM_SUBSTR = tuple(d for d in os.environ.get("CCF_OWN_DOMAINS", "carolinacommercialfinishes").split(",") if d)
_TEAM_ALIASES = tuple(a for a in os.environ.get("OWNER_ALIAS_EMAILS", "").split(",") if a)
# Markers used to detect a recipient/sender that is internal (vs a real GC).
INTERNAL_MARKERS = _OWN_DOM_SUBSTR + _TEAM_ALIASES
# Variant including the bare company-domain substring (used where the To-field
# may carry the role addresses estimates@/cs@ as well as the domain).
INTERNAL_MARKERS_ROLE = ("estimates@carolina", "cs@carolina") + INTERNAL_MARKERS


def decode_h(s):
    if not s: return ""
    out = ""
    for p, e in decode_header(s):
        out += p.decode(e or "utf-8", errors="replace") if isinstance(p, bytes) else p
    return out


def get_body(msg):
    text_p = text_h = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain" and not text_p:
                try: text_p = part.get_payload(decode=True).decode("utf-8", errors="replace")
                except: pass
            elif ct == "text/html" and not text_h:
                try: text_h = part.get_payload(decode=True).decode("utf-8", errors="replace")
                except: pass
    else:
        try:
            decoded = msg.get_payload(decode=True).decode("utf-8", errors="replace")
            if msg.get_content_type() == "text/html": text_h = decoded
            else: text_p = decoded
        except: pass
    if text_p.strip(): return text_p
    # HTML to text fallback
    s = re.sub(r"<style[^>]*>.*?</style>", " ", text_h, flags=re.S | re.I)
    s = re.sub(r"<script[^>]*>.*?</script>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</p>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    for ent, rep in [("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                     ("&quot;", '"'), ("&#39;", "'")]:
        s = s.replace(ent, rep)
    return re.sub(r"\n\s*\n+", "\n\n", s).strip()


# Patterns matching store-number references
STORE_PATTERNS = [
    re.compile(r"#\s*(\d{3,4})[\-A-Z]?", re.I),       # "#NNNN" / "#NNNN-A"
    re.compile(r"(?:store|#)\s*[#:]?\s*(\d{4})", re.I),  # "Store #NNNN"
]


def extract_store_amounts(body: str, subject: str) -> list[dict]:
    """Return list of {store: '#NNNN', amount: '$X,XXX', city: '...'} pairs found in subject+body.
    Looks for patterns like:
      - "Grocery Store #NNNN ... $XX,XXX"
      - "#NNNN-A — City, ST ... TOTAL: $XX,XXX.00"
      - bullet lists with multiple stores
    Only flags as combined if 2+ stores appear in the SUBJECT line — body-only
    references are typically historical context, not new submissions."""
    results = []
    # Find unique store numbers in SUBJECT (must be 2+ to qualify as combined)
    subj_nums = set()
    for pat in STORE_PATTERNS:
        for m in pat.finditer(subject or ""):
            n = m.group(1)
            if len(n) >= 3 and len(n) <= 5:
                subj_nums.add(n)
    if len(subj_nums) < 2:
        return []   # not a combined email — only count when subject says so
    store_nums = subj_nums

    # For each store number, find the closest $ amount in the body
    # Strategy: locate the store in the body, then scan ahead for $ amount
    # within ~200 chars (allow any char incl. em-dashes / colons via re.S + .*?)
    for sn in store_nums:
        body_norm = body or ""
        # Limit to 200 chars to avoid spanning into the next store's amount
        for m in re.finditer(rf"#\s*0*{sn}.{{0,200}}?\$\s*([\d,]+(?:\.\d{{2}})?)",
                              body_norm, re.S):
            amount = "$" + m.group(1)
            # Try to find city near this match (within the captured span)
            ctx = body_norm[max(0, m.start() - 10):m.end() + 30]
            city_m = re.search(
                r"#0*{0}[^A-Za-z]*([A-Z][a-z]+(?:[\s\-][A-Z][a-z]+)*)".format(sn),
                ctx)
            city = city_m.group(1).strip() if city_m else ""
            results.append({"store": "#" + sn.zfill(4), "amount": amount, "city": city})
            break  # first match per store
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    since = (date.today() - timedelta(days=args.days)).strftime("%d-%b-%Y")
    M = imaplib.IMAP4_SSL("imap.gmail.com")
    M.login(GMAIL_USER, GMAIL_PASS)
    M.select('"[Gmail]/Sent Mail"')

    print(f"Scanning Sent folder since {since}...")
    seen_ids = set()
    all_proposals = []
    for q in [
        f'(SINCE "{since}" SUBJECT "proposal")',
        f'(SINCE "{since}" SUBJECT "bid submission")',
    ]:
        st, ids = M.search(None, q)
        if st != "OK" or not ids[0]: continue
        for mid in ids[0].split():
            if mid in seen_ids: continue
            seen_ids.add(mid)
            st, data = M.fetch(mid, '(BODY.PEEK[])')
            if st != "OK": continue
            msg = email_lib.message_from_bytes(data[0][1])
            subj = decode_h(msg.get("Subject", ""))
            # skip follow-ups
            if subj.startswith("Follow-Up:") or "Re: Follow-Up" in subj:
                continue
            to_field = decode_h(msg.get("To", ""))
            # Skip internal-only sends
            if any(x in to_field.lower() for x in INTERNAL_MARKERS_ROLE):
                # Could still have GC recipient — check carefully
                non_internal = [a for a in re.findall(r"[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}", to_field)
                                if not any(x in a.lower() for x in INTERNAL_MARKERS)]
                if not non_internal:
                    continue
            body = get_body(msg)
            stores = extract_store_amounts(body, subj)
            if stores:
                all_proposals.append({
                    "subject": subj,
                    "to": to_field,
                    "date": msg.get("Date", ""),
                    "stores": stores,
                })
    M.logout()

    print(f"\n=== Found {len(all_proposals)} combined-store proposal emails ===\n")
    for p in all_proposals:
        print(f"  {p['date'][:25]}  → {p['to'][:50]}")
        print(f"    Subj: {p['subject'][:75]}")
        for s in p["stores"]:
            print(f"      - {s['store']}  {s['amount']}  {s['city']}")
        print()

    # Cross-check with CRM
    print("=== Verifying CRM coverage ===\n")
    from crm_lib import get_sheet
    sh = get_sheet("Bid Log")
    hdrs = sh.row_values(1)
    rows = sh.get_all_values()
    crm_index = {}  # (store_num_clean, recipient_email) -> row_idx
    for ri, r in enumerate(rows[1:], start=2):
        d = {h: (r[i] if i < len(r) else "") for i, h in enumerate(hdrs)}
        pn = d.get("Project Name", "") or ""
        # Capture stores with `#NNNN` OR `NNNN Food Lion` style (4-digit at start)
        store_set = set()
        for m in re.finditer(r"#\s*0*(\d{3,4})", pn):
            store_set.add(m.group(1).lstrip("0") or "0")
        for m in re.finditer(r"\b(\d{4})\s+Food\s+Lion", pn, re.I):
            store_set.add(m.group(1).lstrip("0") or "0")
        ce = (d.get("Contact Email", "") or "").lower()
        for store in store_set:
            for em in re.split(r"[\s,;]+", ce):
                if "@" in em:
                    crm_index[(store, em.strip())] = ri

    missing = []
    for p in all_proposals:
        # Identify primary recipient
        rcpts = re.findall(r"<([^>]+)>|([\w.%+-]+@[\w.-]+\.[A-Za-z]{2,})", p["to"])
        non_internal = []
        for tup in rcpts:
            addr = (tup[0] or tup[1]).strip().lower()
            if not any(x in addr for x in INTERNAL_MARKERS):
                non_internal.append(addr)
        recipient = non_internal[0] if non_internal else ""
        for s in p["stores"]:
            store_clean = s["store"].lstrip("#").lstrip("0") or "0"
            store_lookup = store_clean.zfill(0) if not store_clean else store_clean
            # Try both with and without leading zeros
            found = (store_clean, recipient) in crm_index
            if not found:
                for k in crm_index:
                    if k[1] == recipient and k[0].lstrip("0") == store_clean.lstrip("0"):
                        found = True; break
            if not found:
                missing.append({
                    "subject": p["subject"][:60],
                    "date": p["date"][:25],
                    "recipient": recipient,
                    "store": s["store"],
                    "amount": s["amount"],
                    "city": s["city"],
                })

    print(f"=== {len(missing)} (store, recipient) pairs missing from CRM ===\n")
    for m in missing:
        print(f"  {m['date']}  {m['store']}  {m['amount']:<12} {m['city']:<20} -> {m['recipient']}")
        print(f"    [from email: {m['subject']}]")
        print()

    if not args.apply or not missing:
        return

    # Insert missing rows
    from crm_lib import append_rows
    BID_FORMULA = '="BID-"&TEXT(ROW()-1,"0000")'
    new_rows = []
    for m in missing:
        # Build the row
        proj = f"Food Lion {m['store']} {m['city']}".strip().rstrip(",")
        # State guess from city
        state = ""
        if m["city"] in ("Randleman", "Greensboro", "Asheboro", "Winston-Salem", "Winston Salem",
                          "Quinton", "Chester", "Chesterfield"):
            state = "NC" if m["city"] not in ("Quinton", "Chester", "Chesterfield") else "VA"
        date_obj = None
        try:
            from email.utils import parsedate_to_datetime
            date_obj = parsedate_to_datetime(m["date"]).date()
        except Exception: pass
        date_str = date_obj.strftime("%m/%d/%Y") if date_obj else ""
        # GC name from recipient domain. The GC directory lives in the
        # gitignored data/memory/gc_crm.json (domain -> name, derived from each
        # GC's stored email); fall back to the title-cased domain when unknown.
        gc_name = ""
        if m["recipient"]:
            domain = m["recipient"].split("@", 1)[-1].lower()
            domain_map = {}
            _gc_crm_path = ROOT / "data" / "memory" / "gc_crm.json"
            if _gc_crm_path.exists():
                try:
                    for _gn, _info in json.loads(_gc_crm_path.read_text(encoding="utf-8")).items():
                        _em = (_info.get("email") or "").strip().lower()
                        if "@" in _em:
                            domain_map[_em.split("@", 1)[-1]] = _gn
                except Exception:
                    pass
            gc_name = domain_map.get(domain, domain.split(".")[0].title())
        new_rows.append({
            "Bid #": BID_FORMULA,
            "Project Name": proj,
            "City": m["city"],
            "State": state,
            "Facility Type": "Grocery Store",
            "GC / Client": gc_name,
            "Contact Email": m["recipient"],
            "Bid Source": "Invitation (GC)",
            "Bid Submitted Date": date_str,
            "Bid Amount ($)": m["amount"],
            "Status": "Bid Submitted",
            "Win/Loss": "PENDING",
            "Notes": f"Backfilled from combined-store email: '{m['subject'][:55]}'",
        })
    print(f"\nAppending {len(new_rows)} new rows to CRM...")
    n = append_rows("Bid Log", new_rows)
    print(f"Wrote {n} rows.")


if __name__ == "__main__":
    main()
