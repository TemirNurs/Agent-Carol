#!/usr/bin/env python3
"""
Ingest Gmail bid invitations into active_bids.json.

Parses invitation emails from Parkway (sreppond@pkwycon.com, smartbidnet),
iSqFt transmittals (isqftmail.com), Eaton Construction, direct GC emails, and
forwarded invitations from team gmail accounts. Extracts project name, GC, due
date, city/state, SF, and source URL. Writes unique entries to active_bids.json
with `source: "email"`.

Usage:
  python scripts/ingest_email_invites.py --days 21          # retro-ingest past 21 days
  python scripts/ingest_email_invites.py --days 7           # normal daemon cadence
  python scripts/ingest_email_invites.py --dry-run          # preview, don't write
  python scripts/ingest_email_invites.py --days 7 --mark    # add 'carol/ingested' label

Designed to be run by the daemon every 30 minutes alongside the BC/CC scrapers.
Dedupe: by normalized (project_name + GC). If already in active_bids.json, skip.
"""

import argparse
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
BIDS_FILE = BASE / "data" / "memory" / "active_bids.json"
LOG_FILE = BASE / "data" / "logs" / "ingest_email.log"

GMAIL_USER = os.environ.get("GMAIL_USER", "estimates@carolinacommercialfinishes.com")
GMAIL_PASS = os.environ.get("GMAIL_APP_PASSWORD", "")


# ---------------- Email retrieval ----------------

def fetch_invitations(days=7):
    """Fetch all bid-invitation-like emails from Gmail past N days."""
    since = (date.today() - timedelta(days=days)).strftime("%d-%b-%Y")
    queries = [
        f'(SINCE "{since}" SUBJECT "invitation to bid")',
        f'(SINCE "{since}" SUBJECT "ITB")',
        f'(SINCE "{since}" SUBJECT "bid request")',
        f'(SINCE "{since}" SUBJECT "RFP")',
        f'(SINCE "{since}" FROM "isqftmail.com")',
        f'(SINCE "{since}" FROM "smartbidnet.com")',
        f'(SINCE "{since}" FROM "pkwycon.com")',
        f'(SINCE "{since}" FROM "parkwayconstruction")',
    ]
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(GMAIL_USER, GMAIL_PASS)
    mail.select("inbox")
    seen = set()
    results = []
    for q in queries:
        status, ids = mail.search(None, q)
        if status != "OK":
            continue
        for mid in ids[0].split():
            if mid in seen:
                continue
            seen.add(mid)
            status, data = mail.fetch(mid, '(BODY.PEEK[])')
            if status != "OK":
                continue
            msg = email_lib.message_from_bytes(data[0][1])
            subj = ""
            for part, enc in decode_header(msg.get("Subject", "")):
                subj += part.decode(enc or "utf-8", errors="replace") if isinstance(part, bytes) else part
            body = ""
            if msg.is_multipart():
                for p in msg.walk():
                    if p.get_content_type() == "text/plain":
                        try:
                            body = p.get_payload(decode=True).decode("utf-8", errors="replace")
                            break
                        except Exception:
                            pass
            else:
                try:
                    body = msg.get_payload(decode=True).decode("utf-8", errors="replace")
                except Exception:
                    pass
            results.append({
                "msg_id": mid.decode() if isinstance(mid, bytes) else str(mid),
                "subject": subj.strip(),
                "from": msg.get("From", ""),
                "date": msg.get("Date", ""),
                "body": body,
            })
    mail.logout()
    return results


# ---------------- Parsers ----------------

ISQFT_RE = re.compile(r"Invitation to Bid\s*-\s*(.+?)(?:\s*:\s*.+)?$", re.I)
SMARTBID_RE = re.compile(r"(.+?)\s+Invitation to Bid", re.I)
PARKWAY_SUB_RE = re.compile(r"Invitation to Bid from Parkway Construction for\s+(.+?)(?:\s*$)", re.I | re.M)
GENERIC_ITB_RE = re.compile(r"\bITB\s*[:\-]\s*(.+?)(?:\s*$)", re.I)


def extract_project_name(subj, body):
    """Try several patterns to pull project name."""
    s = (subj or "").strip()
    # Normalize whitespace (subject lines get wrapped with newlines/tabs in MIME)
    s = re.sub(r"\s+", " ", s).strip()
    # Strip "Fwd: " / "RE: " / "FW: " (may be nested several levels)
    for _ in range(4):
        new = re.sub(r"^(Fwd:|RE:|FW:|Re:|Fw:)\s*", "", s, flags=re.I)
        if new == s:
            break
        s = new

    # Parkway
    m = PARKWAY_SUB_RE.search(s)
    if m:
        return m.group(1).strip().rstrip(".").strip()
    # iSqFt "Invitation to Bid - <name> : <trades>"
    m = ISQFT_RE.search(s)
    if m:
        return m.group(1).strip().rstrip(".").strip()
    # smartbidnet "<name> Invitation to Bid"
    m = SMARTBID_RE.search(s)
    if m:
        return m.group(1).strip().rstrip(".").strip()
    # Generic ITB
    m = GENERIC_ITB_RE.search(s)
    if m:
        return m.group(1).strip().rstrip(".").strip()
    # Body-based Parkway fallback
    m = re.search(r"invited to bid on a[n]?\s+(.+?)(?:\s+at\s+|\s+in\s+|\.)", body[:2000], re.I)
    if m:
        return m.group(1).strip().rstrip(".").strip()
    return None


GC_DOMAIN_MAP = {
    "pkwycon.com": "Parkway Construction",
    "parkwayconstruction": "Parkway Construction",
    "parkwayconstructionplans": "Parkway Construction",
    "eatonprojects.com": "Eaton Construction Services",
    "lfjennings.com": "LF Jennings Inc",
    "wimcocorp.com": "WIMCO Corp",
    "pkwy": "Parkway Construction",
    "valiantconstruct.com": "Valiant Construction",
    "metrolinabuilders.com": "Metrolina Builders",
    "windlecc.com": "Windle Construction",
    "csgcharleston.com": "Construction Services Group",
    "delauterinc.com": "DeLauter",
    "horizonretail.com": "Horizon Retail Construction",
    "pathcc.com": "Path Construction",
    "sempertekinc.com": "Semper Tek",
}


def extract_gc(frm, body, subject):
    """Pull GC name from sender domain, body signature, or subject 'from <GC>' text."""
    # Normalize
    subj_norm = re.sub(r"\s+", " ", subject or "")

    # 1) Subject says "from <GC> for <project>" — most reliable
    m = re.search(r"Invitation to Bid from\s+([A-Z][A-Za-z0-9\.& ,'-]{2,60}?)\s+for\s+", subj_norm, re.I)
    if m:
        cand = m.group(1).strip()
        if "parkway" in cand.lower():
            return "Parkway Construction"
        return cand

    # 2) Body contains forwarded email with real sender domain (common for forwards)
    for m in re.finditer(r"[Ff]rom[^@<]*<([^@>]+@([a-z0-9.-]+))>", body[:4000]):
        dom = m.group(2).lower()
        for d, gc in GC_DOMAIN_MAP.items():
            if d in dom:
                return gc
        # Common GC domains by name hint
        if dom.endswith(".com") and not dom.endswith(("gmail.com", "yahoo.com", "outlook.com", "hotmail.com",
                                                       "isqftmail.com", "smartbidnet.com", "constructconnectmail.com",
                                                       "buildingconnected.com")):
            # Derive GC name from domain (e.g. "pkwycon.com" -> "pkwycon" -> look up)
            base = dom.split(".")[0]
            # Skip personal
            continue

    # 3) Direct sender domain match on the From header
    frm_low = (frm or "").lower()
    for dom, gc in GC_DOMAIN_MAP.items():
        if dom in frm_low:
            return gc

    # 4) iSqFt: sender display name IS the GC, e.g. '"CMC Building, Inc" <Transmittals@isqftmail.com>'
    if "isqftmail.com" in frm_low or "smartbidnet.com" in frm_low:
        m = re.match(r'"?([^"<]{3,60}?)"?\s*<', frm)
        if m:
            cand = m.group(1).strip().strip('"').strip(",. ").strip()
            # Filter out non-GC senders
            if cand and not cand.lower().startswith(("transmittals", "noreply", "no-reply", "admin",
                                                     "support", "notifications", "donotreply")):
                # Some smartbidnet names are personal — filter by keyword
                if len(cand.split()) <= 4 and not any(c in cand for c in ("Inc", "LLC", "Corp", "Construction",
                                                                         "Builders", "Company", "Group", "Services")):
                    # Looks like a personal name, search body
                    pass
                else:
                    return cand

    # 5) Body: look for explicit "GC:" or "General Contractor:" markers
    m = re.search(r"(?:General\s+Contractor|GC|Contractor)[:\s]+([A-Z][A-Za-z0-9& ,'.-]{3,50})", body[:4000])
    if m:
        return m.group(1).strip(",. ").strip()

    # 6) iSqFt signature fallback: find "<GC Name> Inc/LLC/Construction" in body opening
    for line in body.split("\n")[:30]:
        line = line.strip()
        m = re.match(r"([A-Z][A-Za-z0-9& ,'.-]{3,50}?\s+(?:Inc\.?|LLC|Corp\.?|Construction|Builders|Company|Group|Contractors?))\b", line)
        if m:
            return m.group(1).strip(",. ").strip()

    # 7) Fall back to sender display name (may be personal — flag it)
    m = re.match(r'"?([^"<]{3,60}?)"?\s*<', frm)
    if m:
        cand = m.group(1).strip().strip('"').strip(",. ").strip()
        if cand and not cand.lower().startswith(("transmittals", "noreply", "no-reply", "admin",
                                                 "support", "notifications", "donotreply", "bids",
                                                 "estimating", "estimates")):
            return cand
    return None


DUE_DATE_PATTERNS = [
    re.compile(r"BID\s*DUE\s*DATE\s*[:\-]?\s*(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})", re.I),
    re.compile(r"(?:Bid|Proposals?|Submittals?|Tender)\s*(?:are\s*)?due\s*(?:by|on)?\s*[:\-]?\s*(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})", re.I),
    re.compile(r"Submit(?:\s+Proposals?)?\s+by\s*(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})", re.I),
    re.compile(r"Due\s*Date\s*[:\-]?\s*(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})", re.I),
    re.compile(r"(?:due|deadline)[^\n]{0,40}?(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})", re.I),
]


def extract_due_date(body):
    for pat in DUE_DATE_PATTERNS:
        m = pat.search(body[:8000])
        if m:
            raw = m.group(1).replace("-", "/")
            # Normalize to m/d/yyyy
            try:
                parts = raw.split("/")
                mo = int(parts[0]); da = int(parts[1]); yr = int(parts[2])
                if yr < 100:
                    yr += 2000
                return f"{mo}/{da}/{yr}"
            except Exception:
                return raw
    return None


def extract_location(body):
    """Pull city, state from body."""
    # "in <City>, <ST>" or "<City>, <ST> <zip>"
    for m in re.finditer(r"(?:in|at)\s+([A-Z][A-Za-z .]{2,30}),\s*([A-Z]{2})\b", body[:4000]):
        city = m.group(1).strip()
        state = m.group(2).strip()
        # Skip obvious false positives
        if city.lower() in ("this", "that", "a", "an", "the"):
            continue
        return city, state
    for m in re.finditer(r"([A-Z][A-Za-z .]{2,30}),\s*([A-Z]{2})\s+\d{5}", body[:4000]):
        return m.group(1).strip(), m.group(2).strip()
    return None, None


def extract_sf(body):
    """Pull approximate SF from body."""
    m = re.search(r"approximately\s+([\d,]+)\s*SF", body[:4000], re.I)
    if m:
        try:
            return int(m.group(1).replace(",", ""))
        except Exception:
            pass
    m = re.search(r"([\d,]+)\s*(?:SF|square\s*feet|sq\.?\s*ft\.?)", body[:4000], re.I)
    if m:
        try:
            n = int(m.group(1).replace(",", ""))
            if 500 <= n <= 5000000:
                return n
        except Exception:
            pass
    return None


def extract_access_url(body):
    m = re.search(r"https?://(?:www\.)?[a-z0-9.-]*?(?:constructionplans|smartbidnet|isqft|buildingconnected|constructconnect)[a-z0-9./?#&=_%-]*", body[:8000], re.I)
    return m.group(0) if m else None


# ---------------- Normalization + dedup ----------------

def normalize(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def make_bid_record(email_data):
    subj = email_data["subject"]
    body = email_data["body"]
    frm = email_data["from"]

    name = extract_project_name(subj, body)
    if not name:
        return None, "no_project_name"

    gc = extract_gc(frm, body, subj)
    due = extract_due_date(body)
    city, state = extract_location(body)
    sf = extract_sf(body)
    url = extract_access_url(body)

    return {
        "project_name": name,
        "gc": gc or "Unknown",
        "trade": "Painting",
        "due_date": due or "",
        "city": city or "",
        "state": state or "",
        "source": "email",
        "source_detail": frm[:60],
        "opportunity_id": f"em-{abs(hash(name + (gc or ''))) % 10**10}",
        "portal_url": url or "",
        "sf": sf,
        "email_date": email_data["date"][:30],
        "ingested_at": datetime.now().isoformat(timespec="seconds"),
    }, None


def dedupe(new, existing):
    """Return subset of `new` not already in `existing`.

    Dedup strategy: normalize project_name to a key, use first 25 chars.
    Different GC spellings of the same project (BC has "Monteith Construction Corp",
    email has "Monteith Construction Corporation") would otherwise create dupes.
    We prefer the existing (BC/CC) entry since its metadata is usually more complete.
    """
    keys = set()
    for b in existing:
        k = normalize(b.get("project_name"))[:30]
        if k:
            keys.add(k)
    added = []
    for b in new:
        k = normalize(b["project_name"])[:30]
        if k in keys:
            continue
        keys.add(k)
        added.append(b)
    return added


# ---------------- Main ----------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7, help="How many days of Gmail to scan (default 7)")
    ap.add_argument("--dry-run", action="store_true", help="Show what would be added without writing")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--skip-past-due", action="store_true", help="Drop entries whose due_date is before today")
    ap.add_argument("--only-known-gcs", action="store_true", help="Only ingest entries where GC matches a known domain")
    args = ap.parse_args()

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    print(f"[ingest] scanning last {args.days} days of Gmail for bid invitations...")
    emails = fetch_invitations(days=args.days)
    print(f"[ingest] found {len(emails)} candidate emails")

    candidates = []
    skipped_noparse = 0
    for e in emails:
        rec, err = make_bid_record(e)
        if rec is None:
            skipped_noparse += 1
            if args.verbose:
                print(f"  SKIP ({err}): {e['subject'][:70]}")
            continue
        candidates.append(rec)

    # Load existing
    existing = []
    if BIDS_FILE.exists():
        existing = json.loads(BIDS_FILE.read_text(encoding="utf-8"))

    added = dedupe(candidates, existing)

    # Optional filters
    if args.skip_past_due:
        today = date.today()
        kept = []
        dropped = 0
        for b in added:
            due = b.get("due_date", "")
            if due:
                try:
                    parts = due.split("/")
                    d = date(int(parts[2]), int(parts[0]), int(parts[1]))
                    if d < today:
                        dropped += 1
                        continue
                except Exception:
                    pass
            kept.append(b)
        print(f"[ingest] skipped {dropped} past-due entries")
        added = kept

    print(f"[ingest] parsed={len(candidates)} skipped_unparseable={skipped_noparse}")
    print(f"[ingest] after dedup vs active_bids.json: {len(added)} NEW to add")
    print()
    for b in added:
        print(f"  + {b['due_date'] or '?':10}  {b['project_name'][:50]:50}  GC={b['gc'][:25]:25}  src={b['source']}")

    if args.dry_run:
        print("\n[dry-run] no changes written.")
        return

    if added:
        merged = existing + added
        BIDS_FILE.write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\n[ingest] wrote {len(added)} new entries to {BIDS_FILE.name}")
        # Append to log
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')}  added={len(added)}\n")
            for b in added:
                f.write(f"  {b['due_date'] or '?'}  {b['project_name']}  (GC={b['gc']}, src=email)\n")
    else:
        print("\n[ingest] nothing new to add.")


if __name__ == "__main__":
    main()
