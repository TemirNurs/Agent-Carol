#!/usr/bin/env python3
"""
audit_crm_submissions.py - Reconcile EVERY sent proposal against the CRM.

For each Sent-folder proposal email in the last N days:
  1. Parse subject -> project name
  2. Parse To + Cc -> all GC recipients
  3. Parse body -> bid amount
  4. For each (project, recipient) pair, check if a matching CRM row exists.
     Match key: (Project Name fuzzy >= 0.50) AND (Contact Email == recipient).
  5. Missing pairs are queued for insertion into the next placeholder rows.

Usage:
  python scripts/audit_crm_submissions.py                   # dry-run
  python scripts/audit_crm_submissions.py --apply           # insert missing
  python scripts/audit_crm_submissions.py --days 365        # window (default 365)
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

INTERNAL_DOMAINS = ("carolinacommercialfinishes.com", "ccfpaint.com")

# Personal email addresses of the internal team - never count as CRM GC rows.
# Sourced from env (OWNER_ALIAS_EMAILS) so no personal addresses are hardcoded.
PERSONAL_EMAIL_BLACKLIST = {
    a.strip().lower()
    for a in os.environ.get("OWNER_ALIAS_EMAILS", "").split(",")
    if a.strip()
}

# Subject patterns that indicate the email is NOT a bid submission - skip entirely.
NON_PROPOSAL_SUBJECT_PATTERNS = [
    r"subcontractor\s+prequalification",
    r"subcontractor\s+introduction",
    r"vendor\s+registration",
    r"prequalification\s+(?:form|inquiry)",
    r"^\s*introduction\b",
    r"insurance\s+(?:certificate|coi)",
    r"w[- ]?9\b",
    r"vendor\s+setup",
]

# Reuse the matcher and GC-lookup from crm_writeback
from crm_writeback import match_score, _gc_info_for_email, slugify, parse_date, _parse_email_date

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

# Snap dropdown-column values to valid options
try:
    from crm_lookups import canonicalize as _canon
except Exception:
    def _canon(col, val): return val


def decode_h(value):
    if not value: return ""
    out = ""
    for p, e in decode_header(value):
        out += p.decode(e or "utf-8", errors="replace") if isinstance(p, bytes) else p
    return out


def parse_addresses(field):
    """Return list of email addresses from a To/Cc field, lowercased & deduped.
    Filters out: internal CCF domains, team-member personal addresses."""
    if not field: return []
    field = decode_h(field)
    addrs = re.findall(r"<([^>]+)>|([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})", field)
    out = []
    for tup in addrs:
        addr = (tup[0] or tup[1]).strip().lower()
        if not addr: continue
        if any(addr.endswith("@" + d) for d in INTERNAL_DOMAINS):
            continue
        if addr in PERSONAL_EMAIL_BLACKLIST:
            continue
        if addr not in out:
            out.append(addr)
    return out


def is_non_proposal_subject(subject):
    """True if this email subject is admin/admin (not a bid submission)."""
    if not subject: return True
    s = subject.lower()
    for pat in NON_PROPOSAL_SUBJECT_PATTERNS:
        if re.search(pat, s):
            return True
    return False


def parse_display_name(field, target_email):
    """Try to pull display name '"Name" <email>' for a specific recipient."""
    if not field: return ""
    field = decode_h(field)
    # match "Name" <email>  OR  Name <email>
    pat = re.compile(r'(?:"([^"]+)"|([^,<]+?))\s*<' + re.escape(target_email) + r'>',
                     re.IGNORECASE)
    m = pat.search(field)
    if m: return (m.group(1) or m.group(2) or "").strip()
    return ""


# Generic mailbox prefixes (not real people - leave Contact Name blank)
_GENERIC_PREFIXES = {
    "bids", "info", "office", "estimating", "estimate", "estimates",
    "subcontract", "subcontractor", "subcontractors", "prequal",
    "prequalification", "qualifications", "opportunity", "opportunities",
    "team", "admin", "noreply", "no-reply", "mail", "sales", "contact",
    "vendor", "vendors", "purchasing", "ap", "accounting",
}

def name_from_email_prefix(email_addr):
    """Best-effort contact name from the local-part of an email.
    Examples:
      jane.doe@example.com -> 'Jane Doe'
      jdoe@example.com -> 'J. Doe'
      jane@example.com -> 'Jane'
      bids@example.com -> '' (generic)
      john.smith@example.com -> 'John Smith'"""
    if not email_addr or "@" not in email_addr:
        return ""
    local = email_addr.split("@", 1)[0].lower()
    if local in _GENERIC_PREFIXES:
        return ""
    # firstname.lastname
    if "." in local:
        parts = [p for p in local.split(".") if p]
        if all(p.isalpha() and len(p) >= 2 for p in parts):
            return " ".join(p.title() for p in parts)
    # firstname_lastname or firstname-lastname
    for sep in ("_", "-"):
        if sep in local:
            parts = [p for p in local.split(sep) if p]
            if all(p.isalpha() and len(p) >= 2 for p in parts):
                return " ".join(p.title() for p in parts)
    # flastname pattern: single initial + surname (e.g. jsmith, jdoe)
    if local.isalpha() and 5 <= len(local) <= 15:
        # Heuristic: first char is initial only if remainder >= 4 chars
        if len(local) >= 5:
            return f"{local[0].upper()}. {local[1:].title()}"
    # Fallback: title-case the whole local
    if local.isalpha():
        return local.title()
    return ""


def extract_project_from_subject(subject):
    """Normalize the project portion of a proposal email subject.
    Strips reply/forward/follow-up prefixes and trailing 'Painting Proposal',
    'Bid Submission', 'Carolina Commercial Finishes', GC project codes, etc."""
    s = re.sub(r"\s+", " ", subject or "").strip()
    s = re.sub(r"^(Fwd:|RE:|Re:|FW:|Fw:)\s*", "", s, flags=re.I).strip()
    s = re.sub(r"^Follow[- ]?Up\s*[:—\-]\s*", "", s, flags=re.I).strip()
    s = re.sub(r"^REVISED\s+Bid\s*[-—]\s*", "", s, flags=re.I).strip()
    s = re.sub(r"^(?:Painting\s+Proposal|CCF\s+Bid|Bid\s+Submission)\s*[—\-:]\s*",
               "", s, flags=re.I).strip()
    s = re.sub(r"^JCC\s+", "", s, flags=re.I).strip()
    # Strip GC project-code suffix ("| Project 2025291.00", "(Project #1234)")
    s = re.sub(r"\s*[|\(\[]\s*Project\s+(?:#\s*)?[\d.\-]+\s*[\)\]]?\s*$",
               "", s, flags=re.I)
    # Strip "— Revised Proposal Attached" / "— Updated Proposal" etc.
    s = re.sub(r"\s*[—\-|]\s*(?:Revised|Updated|Attached|Final)\s+(?:Proposal|Bid|Pricing)?(?:\s+Attached)?$",
               "", s, flags=re.I)
    # Trim trailing markers — handle pipe `|` as separator
    s = re.sub(r"\s*[—\-|]\s*Painting\s+(?:&\s+\w+\s+)?(?:Proposal|Bid\s+Submission).*$",
               "", s, flags=re.I)
    s = re.sub(r"\s*[—\-|]\s*(?:Painting|Proposal|Bid\s+Submission|Bid|Quote|Estimate).*$",
               "", s, flags=re.I)
    s = re.sub(r"\s*[—\-|]\s*Carolina\s+Commercial.*$", "", s, flags=re.I)
    s = re.sub(r"\s*[—\-|]\s*CCF.*$", "", s, flags=re.I)
    # Collapse "Food Lion —" / "Food Lion -" leftover dash artifacts
    s = re.sub(r"\bFood\s+Lion\s+[—\-]\s+", "Food Lion ", s, flags=re.I)
    return s.strip("- :").strip()


def msg_body_text(msg):
    body = ""
    try:
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    try:
                        body = part.get_payload(decode=True).decode("utf-8", errors="replace")
                        break
                    except Exception: pass
        else:
            body = msg.get_payload(decode=True).decode("utf-8", errors="replace")
    except Exception:
        pass
    return body[:10000]


def extract_amount(body):
    """First plausible $ amount in body (5K..5M)."""
    if not body: return None
    for m in re.finditer(r"\$\s*([\d,]+(?:\.\d{2})?)", body):
        try:
            v = int(m.group(1).replace(",", "").split(".")[0])
            if 5_000 <= v <= 5_000_000:
                return f"${m.group(1)}"
        except Exception:
            continue
    return None


def fetch_proposals(days=365):
    """Pull every proposal-pattern email from Sent. Returns list of dicts."""
    since = (date.today() - timedelta(days=days)).strftime("%d-%b-%Y")
    queries = [
        f'(SINCE "{since}" SUBJECT "proposal")',
        f'(SINCE "{since}" SUBJECT "bid submission")',
        f'(SINCE "{since}" SUBJECT "painting" SUBJECT "bid")',
        f'(SINCE "{since}" SUBJECT "painting" SUBJECT "proposal")',
        f'(SINCE "{since}" SUBJECT "carolina commercial")',
    ]
    M = imaplib.IMAP4_SSL("imap.gmail.com")
    M.login(GMAIL_USER, GMAIL_PASS)
    M.select('"[Gmail]/Sent Mail"')
    seen, results = set(), []
    for q in queries:
        st, ids = M.search(None, q)
        if st != "OK" or not ids[0]: continue
        for mid in ids[0].split():
            if mid in seen: continue
            seen.add(mid)
            st, data = M.fetch(mid, '(BODY.PEEK[])')
            if st != "OK": continue
            msg = email_lib.message_from_bytes(data[0][1])
            subj = decode_h(msg.get("Subject", ""))
            to_field = msg.get("To", "")
            cc_field = msg.get("Cc", "")
            recipients = parse_addresses(to_field) + parse_addresses(cc_field)
            # Dedupe
            recipients = list(dict.fromkeys(recipients))
            date_raw = msg.get("Date", "")
            sent_date = _parse_email_date(date_raw) or parse_date(date_raw)
            body = msg_body_text(msg)
            amount = extract_amount(body)
            results.append({
                "subject": subj.strip(),
                "to_field": to_field,
                "cc_field": cc_field,
                "recipients": recipients,
                "date_raw": date_raw,
                "sent_date": sent_date,
                "amount": amount,
                "is_revised": bool(re.match(r"^REVISED\s+Bid", subj, re.I)),
                "is_reply": bool(re.match(r"^(Re:|Fwd:|FW:|Fw:)", subj, re.I)),
                # Match "Follow-Up:" / "Follow-Up —" / "Follow-Up -"
                "is_followup": bool(re.match(r"^Follow[- ]?Up\s*[:—\-]", subj, re.I)),
            })
    M.logout()
    return results


def normalize_project(name):
    if not name: return ""
    s = re.sub(r"[^a-z0-9\s]", " ", name.lower())
    s = re.sub(r"\s+", " ", s).strip()
    return s


def project_signature(name):
    """Stable signature for grouping: lowercased + word-set."""
    return frozenset(t for t in normalize_project(name).split() if len(t) >= 3)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--apply", action="store_true",
                    help="Actually insert missing rows into CRM")
    ap.add_argument("--show-covered", action="store_true",
                    help="Verbose: also list (project, GC) pairs already in CRM")
    ap.add_argument("--match-threshold", type=float, default=0.50)
    args = ap.parse_args()

    print(f"[audit] scanning Sent folder past {args.days} days...")
    proposals = fetch_proposals(days=args.days)
    print(f"[audit] found {len(proposals)} sent proposal emails")

    # Group by (project_subject_normalized, recipient)
    # Each unique (project, recipient) pair represents one bid in the CRM
    pairs = {}  # key = (project_sig, recipient) -> {project_name, recipient, date, amount, count}
    for p in proposals:
        if p["is_followup"]:
            continue  # follow-ups don't represent NEW bid submissions
        if is_non_proposal_subject(p["subject"]):
            continue  # admin / prequalification / vendor-setup emails
        if not p["recipients"]:
            continue  # internal-only sends (after blacklist filtering)
        proj = extract_project_from_subject(p["subject"])
        if not proj or len(proj) < 5:
            continue
        sig = project_signature(proj)
        if not sig:
            continue
        for rcpt in p["recipients"]:
            key = (sig, rcpt)
            existing = pairs.get(key)
            if existing is None or (p["sent_date"] and
                                    (not existing["sent_date"] or
                                     p["sent_date"] >= existing["sent_date"])):
                pairs[key] = {
                    "project_name": proj,
                    "recipient": rcpt,
                    "sent_date": p["sent_date"],
                    "amount": p["amount"] or (existing or {}).get("amount"),
                    "subject": p["subject"],
                    "is_revised": p["is_revised"],
                    "to_field": p["to_field"],
                    "cc_field": p["cc_field"],
                    "send_count": (existing or {}).get("send_count", 0) + 1,
                }
            else:
                pairs[key]["send_count"] = pairs[key].get("send_count", 0) + 1
                if p["amount"] and not pairs[key].get("amount"):
                    pairs[key]["amount"] = p["amount"]

    print(f"[audit] unique (project, recipient) pairs: {len(pairs)}")

    # Load CRM Bid Log
    from crm_lib import get_sheet
    bid_sheet = get_sheet("Bid Log")
    headers = bid_sheet.row_values(1)
    all_rows = bid_sheet.get_all_values()
    existing_rows = []
    placeholder_rows = []
    in_use_bid_ids = set()
    for r_idx, row in enumerate(all_rows[1:], start=2):
        d = {h: (row[i] if i < len(row) else "") for i, h in enumerate(headers)}
        bid_id = d.get("Bid #", "").strip()
        proj = d.get("Project Name", "").strip()
        if bid_id and proj:
            existing_rows.append({"row_idx": r_idx, "data": d})
            in_use_bid_ids.add(bid_id)
        elif bid_id and not proj:
            placeholder_rows.append({"row_idx": r_idx, "bid_id": bid_id})
    # Skip placeholders whose Bid# is already in use
    placeholder_rows = [p for p in placeholder_rows if p["bid_id"] not in in_use_bid_ids]

    print(f"[audit] CRM has {len(existing_rows)} bid rows + {len(placeholder_rows)} empty placeholders")

    # Compute next bid id
    max_num = 0
    for r in existing_rows:
        m = re.search(r"BID-(\d{4,})", r["data"].get("Bid #", ""))
        if m:
            try: max_num = max(max_num, int(m.group(1)))
            except: pass

    def row_emails(row_data):
        """Return list of emails in a CRM 'Contact Email' cell.
        Some user cells contain multiple emails separated by whitespace/comma/semicolon."""
        v = (row_data.get("Contact Email") or "").strip().lower()
        if not v: return []
        # Split on common separators
        parts = re.split(r"[\s,;]+", v)
        return [p for p in parts if "@" in p]

    def project_contains_crm_name(sent_name, crm_name):
        """True if every word (>=4 chars) in the CRM name appears in the sent
        subject. Used as a 'CRM is a subset of subject' check, e.g.
        'Heartland Dental Wesley Chapel' contains 'Heartland Dental'."""
        crm_norm = normalize_project(crm_name)
        sent_norm = normalize_project(sent_name)
        crm_tokens = [t for t in crm_norm.split() if len(t) >= 4]
        if not crm_tokens: return False
        sent_set = set(sent_norm.split())
        return all(t in sent_set for t in crm_tokens)

    # For each unique pair, check CRM
    covered = []
    missing = []
    for key, pair in pairs.items():
        project_name = pair["project_name"]
        recipient = pair["recipient"]
        # Match priority (email is a strong signal, so we accept low name scores):
        # (1) Contact Email exact match + (project ≥0.30 OR CRM-name-is-subset-of-subject)
        # (2) Project Name ≥0.65 + GC name approx match (row has no email)
        match = None
        for e in existing_rows:
            emails_in_row = row_emails(e["data"])
            if not emails_in_row or recipient not in emails_in_row:
                continue
            crm_name = e["data"].get("Project Name") or ""
            score = match_score(project_name, crm_name)
            if score >= 0.30 or project_contains_crm_name(project_name, crm_name):
                match = (e, score)
                break
        if not match:
            for e in existing_rows:
                if row_emails(e["data"]): continue  # row HAS email but didn't match
                row_gc = e["data"].get("GC / Client") or ""
                score = match_score(project_name, e["data"].get("Project Name") or "")
                if score >= 0.65:
                    gc_name, _, _ = _gc_info_for_email(recipient)
                    if gc_name and difflib.SequenceMatcher(None, gc_name.lower(), row_gc.lower()).ratio() >= 0.55:
                        match = (e, score)
                        break
        if match:
            covered.append((pair, match[0]))
        else:
            missing.append(pair)

    print()
    print(f"[audit] covered (already in CRM): {len(covered)}")
    print(f"[audit] missing from CRM: {len(missing)}")

    if args.show_covered:
        print()
        print("=== COVERED ===")
        for pair, row in sorted(covered, key=lambda x: x[0]["sent_date"] or date.max):
            print(f"  ok: {row['data'].get('Bid #','?'):<10} {pair['project_name'][:45]:<45} -> {pair['recipient'][:35]}")

    if missing:
        print()
        print("=== MISSING (will create one row per pair) ===")
        # Sort missing by date (earliest first)
        missing = sorted(missing, key=lambda p: p["sent_date"] or date.max)
        for pair in missing:
            gc_name, _, _ = _gc_info_for_email(pair["recipient"])
            print(f"  + {pair['project_name'][:45]:<45} -> {gc_name[:25]:<25} "
                  f"({pair['recipient'][:32]:<32}) {str(pair['sent_date'] or '?'):<12} {pair['amount'] or '?'}")

    if not args.apply or not missing:
        if missing and not args.apply:
            print()
            print("[audit] dry-run. Re-run with --apply to insert missing rows.")
        return

    # === Apply: insert missing rows ===
    print()
    print(f"[audit] inserting {len(missing)} rows...")
    from crm_lib import batch_update_rows, append_rows

    SOURCE_DEFAULT = "Invitation (GC)"

    # Facility type detector
    def infer_facility(name):
        n = (name or "").lower()
        for kw, ft in [
            ("food lion","Grocery Store"), ("grocery","Grocery Store"),
            ("target","Retail / Big Box"), ("walmart","Retail / Big Box"),
            ("dollar","Retail / Big Box"), ("sally beauty","Retail / Big Box"),
            ("savers","Retail / Big Box"), ("victoria","Retail / Big Box"),
            ("hospital","Medical / Hospital"), ("medical","Medical / Hospital"),
            ("clinic","Medical / Hospital"), ("dental","Medical / Hospital"),
            ("hotel","Hotel"), ("suites","Hotel"), ("hyatt","Hotel"),
            ("hilton","Hotel"), ("marriott","Hotel"), ("hampton inn","Hotel"),
            ("school","School / Education"), ("university","School / Education"),
            ("carvana","Commercial Auto"), ("adesa","Commercial Auto"),
        ]:
            if kw in n: return ft
        return ""

    # Extract City, State from project name
    def infer_city_state(name):
        if not name: return ("", "")
        # Look for ", XX" 2-letter state at end
        m = re.search(r",\s*([A-Z]{2})\b", name)
        state = m.group(1) if m else ""
        # City: word(s) just before the state, after any leading store number
        city = ""
        if state:
            before = name[:m.start()]
            # take last 1-3 word phrase
            tail = re.findall(r"[A-Za-z]+(?:\s+[A-Za-z]+){0,2}", before)
            if tail:
                city = tail[-1].strip()
                # filter out boilerplate
                if city.lower() in ("food lion","painting","store"):
                    city = ""
        return (city, state)

    placeholder_queue = list(placeholder_rows)
    next_num = max_num + 1

    updates = []        # for placeholder fills: (row_idx, col, val)
    appended_rows = []  # for end-of-sheet appends
    inserted = []

    # Build lookup of gc_crm primary email per GC name so we can detect when
    # the actual recipient differs from the GC's stored primary contact.
    primary_email_per_gc = {}
    gc_path = BASE / "data" / "memory" / "gc_crm.json"
    if gc_path.exists():
        try:
            gc_data = json.loads(gc_path.read_text(encoding="utf-8"))
            for gn, info in gc_data.items():
                em = (info.get("email") or "").strip().lower()
                if em:
                    primary_email_per_gc[gn.lower()] = em
        except Exception: pass

    for pair in missing:
        # Pick Bid # + target row
        if placeholder_queue:
            ph = placeholder_queue.pop(0)
            bid_id = ph["bid_id"]
            target = ph["row_idx"]
            placement = f"fill r{target}"
        else:
            bid_id = f"BID-{next_num:04d}"
            next_num += 1
            target = None
            placement = "append"

        recipient = pair["recipient"]
        gc_name, primary_contact_name, primary_phone = _gc_info_for_email(recipient)

        # Determine the actual contact name & phone for THIS recipient.
        # If the recipient email matches the GC's stored primary, use primary info.
        # Otherwise, the recipient is a DIFFERENT contact at the same GC — pull
        # the display name from the email header (best) or derive from email
        # prefix (fallback). Clear phone (we don't know this specific person's).
        primary_em = primary_email_per_gc.get((gc_name or "").lower(), "")
        if recipient == primary_em:
            contact_name = primary_contact_name
            phone = primary_phone
        else:
            contact_name = (parse_display_name(pair["to_field"], recipient)
                            or parse_display_name(pair["cc_field"], recipient)
                            or name_from_email_prefix(recipient))
            phone = ""

        # Same-run dedupe (sibling of the crm_writeback guard): one row per
        # (project-core, GC domain) per run — a second inbox at the same GC
        # (e.g. bids@ vs a named contact at the same domain) must not spawn a duplicate row.
        _core = re.sub(r"[^a-z0-9]", "", pair["project_name"].lower())[:24]
        _dom = (recipient.split("@", 1)[1].lower() if "@" in (recipient or "") else "")
        _runkey = (_core, _dom)
        if _runkey in _audit_run_created:
            print(f"  SKIP-DUP(run) {pair['project_name'][:38]} -> {recipient[:28]}")
            continue
        _audit_run_created.add(_runkey)

        city, state = infer_city_state(pair["project_name"])
        ft = infer_facility(pair["project_name"])
        sd = pair["sent_date"].strftime("%m/%d/%Y") if pair["sent_date"] else ""

        row_data = {
            "Bid #": bid_id,
            "Project Name": pair["project_name"],
            "City": city,
            "State": state,
            "Facility Type": _canon("Facility Type", ft) or ft,
            "GC / Client": gc_name,
            "Contact Name": contact_name,
            "Contact Email": recipient,
            "Contact Phone": phone or "",
            "Bid Source": _canon("Bid Source", SOURCE_DEFAULT) or SOURCE_DEFAULT,
            "Bid Submitted Date": sd,
            "Bid Amount ($)": pair["amount"] or "",
            "Status": _canon("Status", "Bid Submitted") or "Bid Submitted",
            "Win/Loss": "PENDING",   # not a dropdown column, free text
            # Loss Reason intentionally blank - user fills via dropdown
        }

        print(f"  + {bid_id} {pair['project_name'][:38]:<38} -> {gc_name[:22]:<22} ({recipient[:28]}) | {placement}")

        if target is not None:
            for col, v in row_data.items():
                if v:
                    updates.append((target, col, v))
        else:
            appended_rows.append({h: row_data.get(h, "") for h in headers})
        inserted.append((bid_id, pair))

    # Strip Bid# from placeholder writes — preserves the user's auto-numbering
    # formula =BID-&TEXT(ROW()-1,"0000") in col A.
    updates = [(r, c, v) for (r, c, v) in updates if c != "Bid #"]
    if updates:
        print(f"[audit] writing {len(updates)} placeholder cells (Bid# formula preserved)...")
        batch_update_rows("Bid Log", updates)
    if appended_rows:
        # Bid # stays the STATIC id minted above — never the row-position
        # formula (it renumbered the whole CRM on sort; frozen 2026-06-10).
        print(f"[audit] appending {len(appended_rows)} rows at end (static Bid#)...")
        append_rows("Bid Log", appended_rows)

    print(f"\n[audit] inserted {len(inserted)} rows. CRM is now in sync with Sent folder.")


if __name__ == "__main__":
    main()
