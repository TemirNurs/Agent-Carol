#!/usr/bin/env python3
r"""
backfill_contacts.py — Fill in missing Contact Email / Contact Phone in the CRM.

Sources, in priority order (most authoritative first):
  1. Cowork session transcripts at mempalace/wings/cowork/*.transcripts.json
     — Nursultan often pastes "GC contact: Name <email>, (phone)" in chats
  2. Gmail Sent + Inbox — extract sender info + signature phone numbers
     from threads matching the project name

For each bid in CRM Bid Log:
  - If Contact Email is blank → search the above for an inbound sender, pick most recent
  - If Contact Phone is blank → search the above for any phone number in
    related emails (preferring those near "c:", "cell:", "phone:" prefixes)
  - Updates CRM. Never overwrites existing values.

Usage:
  python scripts/backfill_contacts.py                 # all rows missing data
  python scripts/backfill_contacts.py --bid BID-0008  # one bid
  python scripts/backfill_contacts.py --status "Awaiting Decision"
  python scripts/backfill_contacts.py --dry-run
  python scripts/backfill_contacts.py --quiet
"""

import argparse
import email as email_lib
import imaplib
import json
import os
import re
import sys
from datetime import datetime
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

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

COWORK_DIR = ROOT / "mempalace" / "wings" / "cowork"
LOG_FILE   = ROOT / "data" / "logs" / "backfill_contacts.log"

GMAIL_USER = os.environ.get("GMAIL_USER", "estimates@carolinacommercialfinishes.com")
GMAIL_PASS = os.environ.get("GMAIL_APP_PASSWORD", "")

# Phone regex — covers common US formats
PHONE_PATTERNS = [
    r"\(\s*\d{3}\s*\)\s*\d{3}\s*[-.\s]?\s*\d{4}",   # (980) 348-1827
    r"\b\d{3}\s*[-.\s]\s*\d{3}\s*[-.\s]\s*\d{4}\b",  # 980-348-1827, 980.348.1827
    r"\+1\s*[-.\s]?\s*\d{3}\s*[-.\s]?\s*\d{3}\s*[-.\s]?\s*\d{4}",  # +1-980-348-1827
]
PHONE_RE = re.compile("|".join(PHONE_PATTERNS))

# Phone signal words (priority context — phone after these is more likely real)
PHONE_HINT_RE = re.compile(
    r"(?:^|[\s|])(?:c|cell|phone|mobile|tel|office|p|m|direct)\s*[:.]?\s*"
    + "(?:" + "|".join(PHONE_PATTERNS) + r")",
    re.IGNORECASE | re.MULTILINE,
)

# Phones to ignore (our own + common false positives)
IGNORE_PHONES = {
    "9803481827",  # CCF main
    "8003411267",  # likely a fax / 800#
}


def normalize_phone(s: str) -> str:
    """Strip everything except digits."""
    return re.sub(r"\D", "", s)


def format_phone(digits: str) -> str:
    """Format 10/11 digits as (xxx) xxx-xxxx."""
    d = re.sub(r"\D", "", digits)
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    if len(d) == 10:
        return f"({d[:3]}) {d[3:6]}-{d[6:]}"
    return digits


def extract_phones(text: str) -> list[tuple[str, bool]]:
    """Return [(formatted_phone, is_signal_word_context)] from text."""
    if not text: return []
    found = []
    seen = set()
    # Signal-word matches (high quality)
    for m in PHONE_HINT_RE.finditer(text):
        phone = m.group(0)
        nm = normalize_phone(phone)
        if len(nm) >= 10:
            nm = nm[-10:]
            if nm in IGNORE_PHONES or nm in seen: continue
            seen.add(nm)
            found.append((format_phone(nm), True))
    # Fallback: any phone-shaped string
    for m in PHONE_RE.finditer(text):
        nm = normalize_phone(m.group(0))
        if len(nm) >= 10:
            nm = nm[-10:]
            if nm in IGNORE_PHONES or nm in seen: continue
            seen.add(nm)
            found.append((format_phone(nm), False))
    return found


def keyword_for_search(name: str) -> str | None:
    """Pick the most distinctive single keyword from a project name."""
    if not name: return None
    STOP = {"the", "for", "and", "of", "store", "project", "building", "renovation",
            "upfit", "construction", "company", "phase", "facility", "office",
            "north", "south", "east", "west", "new", "the", "nc", "sc", "ga", "fl"}
    # Numbers (store numbers) are ideal anchors
    nums = re.findall(r"\b\d{3,}\b", name)
    if nums: return nums[0]
    words = [w for w in re.findall(r"[A-Za-z0-9]+", name)
             if w.lower() not in STOP and len(w) >= 5]
    return words[0] if words else None


def search_cowork_transcripts(project_name: str, gc_name: str) -> dict:
    """Scan cowork transcripts for contact info mentioned by Nursultan."""
    out = {"emails": [], "phones": []}
    if not COWORK_DIR.exists(): return out
    keyword = keyword_for_search(project_name)
    if not keyword: return out
    kw_lower = keyword.lower()
    gc_first = (gc_name or "").split()[0].lower() if gc_name else ""

    # Scan every transcript JSON in cowork
    for jp in COWORK_DIR.glob("*.transcripts.json"):
        try:
            convs = json.loads(jp.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(convs, list): continue
        for conv in convs:
            messages = conv.get("messages") or conv.get("chat_messages") or []
            for m in messages:
                # message text can be in various keys
                text = ""
                if isinstance(m, dict):
                    if isinstance(m.get("content"), list):
                        for blk in m["content"]:
                            if isinstance(blk, dict) and blk.get("type") == "text":
                                text += blk.get("text", "") + " "
                    text += str(m.get("text", "")) + " " + str(m.get("content", "") if isinstance(m.get("content"), str) else "")
                if not text: continue
                tl = text.lower()
                # Only consider messages that mention the project keyword
                if kw_lower not in tl and (not gc_first or gc_first not in tl):
                    continue
                # Extract emails near the mention
                for em in re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", text):
                    if "carolinacommercialfinishes" in em.lower(): continue
                    if em not in out["emails"]:
                        out["emails"].append(em)
                # Extract phones
                for phone, is_signal in extract_phones(text):
                    entry = {"phone": phone, "signal": is_signal, "source": "cowork"}
                    if entry not in out["phones"]:
                        out["phones"].append(entry)
    return out


def search_gmail(project_name: str, gc_name: str, contact_email: str = "",
                 contact_name: str = "") -> dict:
    """Scan Gmail Inbox + Sent for matching emails. Pull contact email + phones from signatures.

    Search strategy (in priority order):
      1. If we have Contact Email → search FROM that exact address (most direct route to their signature)
      2. If we have Contact Email → search FROM their email domain (catches their colleagues too)
      3. Project keyword (fallback — may pull forwarded internal emails)
    """
    out = {"emails": [], "phones": []}
    keyword = keyword_for_search(project_name)
    queries = []
    if contact_email and "@" in contact_email:
        queries.append(("from-direct", f"from:{contact_email}"))
        domain = contact_email.split("@")[-1]
        if domain and "." in domain:
            queries.append(("from-domain", f"from:@{domain}"))
    if keyword:
        queries.append(("project-kw", keyword))
    if not queries: return out
    try:
        M = imaplib.IMAP4_SSL("imap.gmail.com")
        M.login(GMAIL_USER, GMAIL_PASS)
    except Exception:
        return out
    seen_emails = set()
    seen_message_ids = set()
    for folder in ("INBOX", '"[Gmail]/Sent Mail"'):
        try:
            M.select(folder)
        except Exception:
            continue
        # Aggregate message IDs from each query
        all_ids = []
        for q_name, q in queries:
            try:
                typ, data = M.search(None, "X-GM-RAW", q)
            except Exception:
                continue
            if typ != "OK" or not data[0]: continue
            for mid in data[0].split():
                if mid in seen_message_ids: continue
                seen_message_ids.add(mid)
                all_ids.append(mid)
        if not all_ids:
            continue
        # Process this folder's IDs
        data = [b" ".join(all_ids)]
        # Look at most recent 20 matching messages per folder
        ids = data[0].split()[-20:]
        for mid in reversed(ids):
            typ, raw = M.fetch(mid, '(BODY.PEEK[])')
            if typ != "OK" or not raw or not raw[0]: continue
            msg = email_lib.message_from_bytes(raw[0][1])
            fr = msg.get("From", "") or ""
            fl = fr.lower()
            # Skip our own / noise
            if any(s in fl for s in ("carolinacommercialfinishes",
                                      "mailer-daemon", "noreply", "donotreply",
                                      "isqftmail", "buildingconnected.com",
                                      "constructconnect")):
                continue
            # Pull email address from From header
            mE = re.search(r"<([^>]+)>", fr)
            addr = mE.group(1).strip() if mE else fr.strip()
            if "@" in addr and addr not in seen_emails:
                seen_emails.add(addr)
                if folder == "INBOX":
                    out["emails"].append(addr)
            # Extract body text
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        payload = part.get_payload(decode=True)
                        if payload:
                            try:
                                body = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                            except (LookupError, AttributeError):
                                body = payload.decode("utf-8", errors="replace")
                            break
            else:
                payload = msg.get_payload(decode=True)
                if payload:
                    try:
                        body = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
                    except (LookupError, AttributeError):
                        body = payload.decode("utf-8", errors="replace")
            if not body: continue
            # Phones — focus on signature area (last ~600 chars)
            tail = body[-800:] if len(body) > 800 else body
            for phone, is_signal in extract_phones(tail):
                entry = {"phone": phone, "signal": is_signal,
                         "from": addr, "source": "gmail/" + ("inbox" if folder == "INBOX" else "sent")}
                if entry not in out["phones"]:
                    out["phones"].append(entry)
    try: M.logout()
    except Exception: pass
    return out


def pick_best(results: dict, current_email: str, current_phone: str) -> dict:
    """Choose best email + phone from combined results."""
    out = {"email": None, "phone": None, "email_source": "", "phone_source": ""}
    if not current_email and results.get("emails"):
        # Prefer an email whose domain matches the GC's company
        out["email"] = results["emails"][0]
        out["email_source"] = "first found"
    if not current_phone and results.get("phones"):
        # Prefer signal-word matches over loose ones
        sorted_ph = sorted(results["phones"], key=lambda p: (not p.get("signal"), 0))
        out["phone"] = sorted_ph[0]["phone"]
        out["phone_source"] = sorted_ph[0].get("source", "")
    return out


def log(msg: str, quiet: bool = False):
    if not quiet: print(msg)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now().isoformat(timespec='seconds')}  {msg}\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bid", default=None)
    ap.add_argument("--status", default=None,
                    help="Only process rows with this Status (e.g. 'Awaiting Decision')")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    from crm_lib import get_sheet
    from gspread.utils import rowcol_to_a1
    ws = get_sheet("Bid Log")
    headers = ws.row_values(1)
    bid_col = headers.index("Bid #") + 1
    name_col = headers.index("Contact Name") + 1
    email_col = headers.index("Contact Email") + 1
    phone_col = headers.index("Contact Phone") + 1 if "Contact Phone" in headers else None
    if not phone_col:
        log("[backfill] No 'Contact Phone' column in Bid Log — aborting", args.quiet)
        return 1

    recs = ws.get_all_records()
    bid_ids_col = ws.col_values(bid_col)

    # Filter rows to process
    rows = []
    for r in recs:
        if args.bid and r.get("Bid #") != args.bid:
            continue
        if args.status and (r.get("Status") or "").strip() != args.status:
            continue
        # Skip rows that already have BOTH email + phone
        cur_email = (r.get("Contact Email") or "").strip()
        cur_phone = (r.get("Contact Phone") or "").strip()
        if cur_email and cur_phone:
            continue
        # Skip empty trailing rows (no project name = empty CRM row)
        if not (r.get("Project Name") or "").strip():
            continue
        rows.append(r)

    log(f"[backfill] {len(rows)} bid(s) need contact info", args.quiet)
    if not rows:
        return 0

    cell_updates = []
    found_count = 0
    for r in rows:
        bid_id = r.get("Bid #", "")
        proj = r.get("Project Name", "")
        gc = r.get("GC / Client", "")
        cur_email = (r.get("Contact Email") or "").strip()
        cur_phone = (r.get("Contact Phone") or "").strip()
        log(f"\n  {bid_id}  {proj[:45]}  ({gc[:25]})", args.quiet)
        log(f"    current: email={cur_email or '(blank)'}  phone={cur_phone or '(blank)'}", args.quiet)

        # 1. Cowork transcripts (preferred — Nursultan's hand-curated)
        cw = search_cowork_transcripts(proj, gc)
        # 2. Gmail — pass Contact Email + Name to enable from: searches
        gm = search_gmail(proj, gc,
                          contact_email=cur_email,
                          contact_name=(r.get("Contact Name") or "").strip())
        combined = {
            "emails": cw["emails"] + gm["emails"],
            "phones": cw["phones"] + gm["phones"],
        }
        best = pick_best(combined, cur_email, cur_phone)
        log(f"    cowork found: {len(cw['emails'])} email(s), {len(cw['phones'])} phone(s)", args.quiet)
        log(f"    gmail found:  {len(gm['emails'])} email(s), {len(gm['phones'])} phone(s)", args.quiet)

        try:
            row_idx = bid_ids_col.index(bid_id) + 1
        except ValueError:
            continue

        any_change = False
        if best["email"] and not cur_email:
            log(f"    SET Contact Email = {best['email']}  (source: {best['email_source']})", args.quiet)
            cell_updates.append({"range": rowcol_to_a1(row_idx, email_col),
                                 "values": [[best["email"]]]})
            any_change = True
        if best["phone"] and not cur_phone:
            log(f"    SET Contact Phone = {best['phone']}  (source: {best['phone_source']})", args.quiet)
            cell_updates.append({"range": rowcol_to_a1(row_idx, phone_col),
                                 "values": [[best["phone"]]]})
            any_change = True
        if any_change:
            found_count += 1
        else:
            log(f"    nothing found", args.quiet)

    if cell_updates and not args.dry_run:
        ws.batch_update(cell_updates, value_input_option="USER_ENTERED")
        log(f"\n[backfill] {found_count} bid(s) updated, {len(cell_updates)} cells changed", args.quiet)
    elif args.dry_run:
        log(f"\n[backfill] DRY RUN — would update {found_count} bid(s), {len(cell_updates)} cells", args.quiet)
    else:
        log(f"\n[backfill] no changes", args.quiet)

    return 0


if __name__ == "__main__":
    sys.exit(main())
