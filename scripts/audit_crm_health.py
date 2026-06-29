#!/usr/bin/env python3
"""
audit_crm_health.py - Comprehensive CRM health check.

For each row in Bid Log:
  1. DUPLICATE: same project + same Contact Email already exists?
  2. BOUNCE: did Gmail return a "Mail Delivery Failure" for that recipient?
  3. NOT BIDDING: did the contact reply saying they're not pursuing?
  4. FOLLOW-UP GAP: did we send follow-ups not reflected in CRM FU columns?

Outputs a report. With --apply, writes Notes column updates flagging the
issues so you can quickly see which rows need attention.

Usage:
  python scripts/audit_crm_health.py                # dry-run report
  python scripts/audit_crm_health.py --apply        # write Notes
  python scripts/audit_crm_health.py --days 365     # gmail search window
"""

from __future__ import annotations

import argparse
import difflib
import email as email_lib
import imaplib
import json
import os
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from email.header import decode_header
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "scripts"))

GMAIL_USER = os.environ.get("GMAIL_USER", "estimates@carolinacommercialfinishes.com")
GMAIL_PASS = os.environ.get("GMAIL_APP_PASSWORD", "")

from crm_writeback import match_score

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

# Patterns to detect a delivery-failure / bounce email
BOUNCE_SENDER_PATTERNS = [
    r"mailer[- ]?daemon@",
    r"postmaster@",
    r"mail[- ]?delivery[- ]?subsystem@",
]
BOUNCE_SUBJECT_PATTERNS = [
    r"undeliverable",
    r"mail\s+delivery\s+(?:failed|failure)",
    r"delivery\s+status\s+notification",
    r"failure\s+notice",
    r"returned\s+mail",
    r"could\s+not\s+be\s+delivered",
    r"address\s+(?:not\s+found|rejected)",
]

# Patterns in body of GC replies that suggest "we're not bidding on this"
NOT_BIDDING_PATTERNS = [
    r"\bnot\s+(?:going\s+to\s+be\s+)?bidding\b",
    r"\bnot\s+pursuing\b",
    r"\bpassing\s+on\s+(?:this\s+)?(?:one|project|bid)\b",
    r"\bwe\s+(?:won'?t|will\s+not)\s+(?:be\s+)?bid",
    r"\bdecided\s+not\s+to\s+bid\b",
    r"\bno\s+longer\s+(?:bidding|pursuing)\b",
    r"\bwithdraw(?:ing|n)?\s+(?:our\s+)?bid\b",
    r"\bproject\s+(?:has\s+been\s+)?cancel(?:l?ed)?\b",
    r"\bowner\s+(?:has\s+)?(?:cancel(?:l?ed|ling)|put\s+on\s+hold)\b",
]


def decode_h(value):
    if not value: return ""
    out = ""
    for p, e in decode_header(value):
        out += p.decode(e or "utf-8", errors="replace") if isinstance(p, bytes) else p
    return out


def fetch_gmail_data(days=365):
    """Pull Inbox (replies + bounces) and Sent (follow-ups) headers for matching.
    Returns dict with:
      bounces: list of {recipient_email, date, subject}
      inbound_replies: list of {from_email, from_name, date, subject, body}
      sent_followups: list of {to_email, date, subject}
    """
    since = (date.today() - timedelta(days=days)).strftime("%d-%b-%Y")
    M = imaplib.IMAP4_SSL("imap.gmail.com")
    M.login(GMAIL_USER, GMAIL_PASS)

    # --- Bounces from INBOX ---
    M.select("INBOX")
    bounces = []
    bounce_ids = set()
    for q in [
        f'(SINCE "{since}" FROM "mailer-daemon")',
        f'(SINCE "{since}" FROM "postmaster")',
        f'(SINCE "{since}" SUBJECT "undeliverable")',
        f'(SINCE "{since}" SUBJECT "delivery status notification")',
        f'(SINCE "{since}" SUBJECT "failure notice")',
    ]:
        st, ids = M.search(None, q)
        if st != "OK" or not ids[0]:
            continue
        for mid in ids[0].split():
            if mid in bounce_ids: continue
            bounce_ids.add(mid)
            st, data = M.fetch(mid, '(BODY.PEEK[])')
            if st != "OK": continue
            msg = email_lib.message_from_bytes(data[0][1])
            subj = decode_h(msg.get("Subject", ""))
            body = _msg_body(msg)
            # The recipient who bounced is usually mentioned in body
            # like "your message wasn't delivered to user@host"
            bounced_to = None
            m = re.search(r"\b([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})\b", body)
            if m:
                bounced_to = m.group(1).lower()
            bounces.append({
                "subject": subj,
                "from": decode_h(msg.get("From", "")),
                "date": msg.get("Date", ""),
                "bounced_to": bounced_to,
                "body_excerpt": body[:400],
            })

    # --- All inbound replies (we'll match to CRM by sender email) ---
    inbound = []
    seen_inbound = set()
    st, ids = M.search(None, f'(SINCE "{since}")')
    if st == "OK" and ids[0]:
        for mid in ids[0].split():
            if mid in seen_inbound: continue
            seen_inbound.add(mid)
            st, data = M.fetch(mid, '(BODY.PEEK[])')
            if st != "OK": continue
            msg = email_lib.message_from_bytes(data[0][1])
            fr = decode_h(msg.get("From", ""))
            m = re.search(r"<([^>]+)>|([\w.%+-]+@[\w.-]+\.[A-Za-z]{2,})", fr)
            if not m: continue
            from_email = (m.group(1) or m.group(2)).strip().lower()
            # Skip if this is one of our addresses (bounces handled above)
            if from_email.endswith("@carolinacommercialfinishes.com"): continue
            if "mailer-daemon" in from_email or "postmaster" in from_email: continue
            subj = decode_h(msg.get("Subject", ""))
            body = _msg_body(msg)
            inbound.append({
                "from_email": from_email,
                "from_name": fr,
                "subject": subj,
                "date": msg.get("Date", ""),
                "body_excerpt": body[:1500],
            })

    # --- Follow-ups we sent ---
    M.select('"[Gmail]/Sent Mail"')
    followups = []
    seen_fu = set()
    for q in [f'(SINCE "{since}" SUBJECT "Follow-Up")', f'(SINCE "{since}" SUBJECT "Following Up")']:
        st, ids = M.search(None, q)
        if st != "OK" or not ids[0]: continue
        for mid in ids[0].split():
            if mid in seen_fu: continue
            seen_fu.add(mid)
            st, data = M.fetch(mid, '(BODY.PEEK[HEADER])')
            if st != "OK": continue
            msg = email_lib.message_from_bytes(data[0][1])
            subj = decode_h(msg.get("Subject", ""))
            to = decode_h(msg.get("To", ""))
            recipients = re.findall(r"<([^>]+)>|([\w.%+-]+@[\w.-]+\.[A-Za-z]{2,})", to)
            for tup in recipients:
                rcpt = (tup[0] or tup[1]).strip().lower()
                if rcpt.endswith("@carolinacommercialfinishes.com"): continue
                followups.append({
                    "to_email": rcpt,
                    "subject": subj,
                    "date": msg.get("Date", ""),
                })

    M.logout()
    return {"bounces": bounces, "inbound": inbound, "followups": followups}


def _msg_body(msg):
    try:
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    try:
                        return part.get_payload(decode=True).decode("utf-8", errors="replace")
                    except Exception: pass
        else:
            return msg.get_payload(decode=True).decode("utf-8", errors="replace")
    except Exception:
        pass
    return ""


def parse_date_safe(s):
    if not s: return None
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S",
                "%a, %d %b %Y", "%m/%d/%Y", "%Y-%m-%d"):
        try: return datetime.strptime(s.strip()[:30], fmt).date()
        except Exception: pass
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(s).date()
    except Exception:
        return None


def matches_not_bidding(text):
    if not text: return None
    s = text.lower()
    for pat in NOT_BIDDING_PATTERNS:
        m = re.search(pat, s)
        if m: return m.group(0)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--apply", action="store_true",
                    help="Append flag notes to CRM Notes column for problem rows")
    args = ap.parse_args()

    from crm_lib import get_sheet, batch_update_rows
    sh = get_sheet("Bid Log")
    hdrs = sh.row_values(1)
    rows = sh.get_all_values()

    # Build CRM row index
    crm_rows = []
    for r_idx, row in enumerate(rows[1:], start=2):
        d = {h: (row[i] if i < len(row) else "") for i, h in enumerate(hdrs)}
        if not d.get("Project Name", "").strip(): continue
        d["_row_idx"] = r_idx
        d["_bid_id"] = d.get("Bid #", "").strip()
        d["_project_norm"] = re.sub(r"\W+", " ", d.get("Project Name", "").lower()).strip()
        d["_email"] = (d.get("Contact Email", "") or "").strip().lower()
        # multi-email cell -> first one
        if " " in d["_email"] or "," in d["_email"]:
            parts = re.split(r"[\s,;]+", d["_email"])
            d["_email"] = next((p for p in parts if "@" in p), "")
        crm_rows.append(d)

    print(f"[health] inspecting {len(crm_rows)} CRM rows...")

    # === 1. DUPLICATE DETECTION ===
    print(f"\n[1/4] Duplicate scan...")
    by_key = defaultdict(list)
    for r in crm_rows:
        if r["_email"] and r["_project_norm"]:
            by_key[(r["_project_norm"], r["_email"])].append(r)
    duplicates = []
    for key, group in by_key.items():
        if len(group) > 1:
            duplicates.append(group)
    print(f"  duplicate (project + contact email) groups: {len(duplicates)}")
    for grp in duplicates:
        print(f"  - {grp[0]['Project Name'][:35]:<35} {grp[0]['_email'][:30]:<30}")
        for r in grp:
            print(f"      row {r['_row_idx']:<4} {r['_bid_id']:<10} amt={(r.get('Bid Amount ($)') or '')[:10]}")

    # === 2-3. PULL GMAIL DATA ===
    print(f"\n[2/4] Pulling Gmail data ({args.days}d)...")
    g = fetch_gmail_data(days=args.days)
    print(f"  bounces: {len(g['bounces'])} | inbound: {len(g['inbound'])} | followups: {len(g['followups'])}")

    # === 2. BOUNCE MATCHING ===
    print(f"\n[2/4] Matching bounces to CRM rows...")
    bounce_emails = set()
    bounce_info = {}
    for b in g["bounces"]:
        em = b.get("bounced_to")
        if em:
            bounce_emails.add(em)
            bounce_info[em] = b
    bounced_rows = []
    for r in crm_rows:
        if r["_email"] and r["_email"] in bounce_emails:
            r["_bounce"] = bounce_info[r["_email"]]
            bounced_rows.append(r)
    print(f"  CRM rows with bounced recipient: {len(bounced_rows)}")
    for r in bounced_rows[:20]:
        print(f"  - row {r['_row_idx']:<4} {r['_bid_id']:<10} {r['Project Name'][:30]:<30} -> {r['_email']}")

    # === 3. NOT-BIDDING DETECTION ===
    print(f"\n[3/4] Scanning inbound for 'not bidding' replies...")
    not_bidding_by_email = {}  # email -> {snippet, date}
    for msg in g["inbound"]:
        fr = msg["from_email"]
        body = msg.get("body_excerpt", "")
        hit = matches_not_bidding(body)
        if hit:
            d = parse_date_safe(msg["date"])
            cur = not_bidding_by_email.get(fr)
            if cur is None or (d and (not cur["date"] or d > cur["date"])):
                not_bidding_by_email[fr] = {
                    "snippet": hit,
                    "date": d,
                    "subject": msg["subject"],
                    "context": body[:200],
                }
    not_bidding_rows = []
    for r in crm_rows:
        if r["_email"] and r["_email"] in not_bidding_by_email:
            r["_not_bidding"] = not_bidding_by_email[r["_email"]]
            not_bidding_rows.append(r)
    print(f"  CRM rows where contact said NOT bidding: {len(not_bidding_rows)}")
    for r in not_bidding_rows[:30]:
        nb = r["_not_bidding"]
        print(f"  - row {r['_row_idx']:<4} {r['_bid_id']:<10} {r['Project Name'][:30]:<30} -> {r['_email'][:30]:<30} hit='{nb['snippet']}'")

    # === 4. FOLLOW-UP GAP DETECTION ===
    print(f"\n[4/4] Comparing follow-up history vs CRM FU columns...")
    fu_by_email = defaultdict(list)
    for fu in g["followups"]:
        fu_by_email[fu["to_email"]].append(fu)
    fu_gap_rows = []
    for r in crm_rows:
        if not r["_email"]: continue
        actual_fus = fu_by_email.get(r["_email"], [])
        if not actual_fus: continue
        # Count FU dates set in CRM
        fu_cols = ["FU1 Date", "FU2 Date", "FU3 Date", "FU4 Date"]
        crm_fu_count = sum(1 for c in fu_cols if (r.get(c) or "").strip())
        if len(actual_fus) > crm_fu_count:
            r["_fu_actual"] = len(actual_fus)
            r["_fu_crm"] = crm_fu_count
            fu_gap_rows.append(r)
    print(f"  CRM rows with un-logged follow-ups: {len(fu_gap_rows)}")
    for r in fu_gap_rows[:30]:
        print(f"  - row {r['_row_idx']:<4} {r['_bid_id']:<10} {r['Project Name'][:30]:<30}: gmail={r['_fu_actual']} FUs, CRM logs={r['_fu_crm']}")

    # === SUMMARY ===
    print()
    print("=" * 70)
    print("AUDIT SUMMARY")
    print("=" * 70)
    print(f"  Duplicate (project, GC) groups: {len(duplicates)}")
    print(f"  Bounces (recipient never got email): {len(bounced_rows)}")
    print(f"  'Not bidding' responses: {len(not_bidding_rows)}")
    print(f"  Un-logged follow-ups: {len(fu_gap_rows)}")
    print()

    # === APPLY: write Notes column ===
    if not args.apply:
        print("Dry-run. Re-run with --apply to write Notes flags.")
        return

    updates = []
    for r in bounced_rows:
        cur_notes = (r.get("Notes") or "").strip()
        flag = f"[BOUNCE] {r['_email']} did NOT receive our proposal — try alternate contact"
        if flag not in cur_notes:
            new_notes = (cur_notes + " | " + flag).strip(" |")
            updates.append((r["_row_idx"], "Notes", new_notes))
    for r in not_bidding_rows:
        cur_notes = (r.get("Notes") or "").strip()
        nb = r["_not_bidding"]
        date_str = nb["date"].strftime("%m/%d/%Y") if nb["date"] else "?"
        flag = f"[NOT BIDDING] {date_str} — they said \"{nb['snippet']}\""
        if "[NOT BIDDING]" not in cur_notes:
            new_notes = (cur_notes + " | " + flag).strip(" |")
            updates.append((r["_row_idx"], "Notes", new_notes))
    for r in fu_gap_rows:
        cur_notes = (r.get("Notes") or "").strip()
        flag = f"[FU GAP] gmail={r['_fu_actual']} FUs, CRM logs={r['_fu_crm']}"
        if "[FU GAP]" not in cur_notes:
            new_notes = (cur_notes + " | " + flag).strip(" |")
            updates.append((r["_row_idx"], "Notes", new_notes))

    print(f"Writing {len(updates)} Note flags...")
    if updates:
        batch_update_rows("Bid Log", updates)
    print("Done.")


if __name__ == "__main__":
    main()
