#!/usr/bin/env python3
"""Scan Gmail Sent on May 19, 2026 — what proposals went out that
track_submissions or crm_writeback missed?"""
import imaplib, email, os, re, sys
from email.header import decode_header
from email.utils import parsedate_to_datetime
from datetime import date

try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

USER = os.environ.get("GMAIL_USER", "estimates@carolinacommercialfinishes.com")
PASS = os.environ.get("GMAIL_APP_PASSWORD", "")

def decode_h(s):
    out = ""
    for p, e in decode_header(s or ""):
        out += p.decode(e or "utf-8", errors="replace") if isinstance(p, bytes) else p
    return out

PROP_RE = re.compile(
    r"(proposal|painting|bid|estimate|finishes|wallcover|coatings|sow|"
    r"food\s*lion|midtown|tmsa|chewy|vika|sampson|cook\s*cdc|box\s*lunch|"
    r"foreman|food\s*lion|kindercare|carvana|target|hyatt)",
    re.I,
)

def body(msg):
    if msg.is_multipart():
        for p in msg.walk():
            if p.get_content_type() in ("text/plain","text/html"):
                try: return p.get_payload(decode=True).decode("utf-8", errors="replace")
                except: pass
    else:
        try: return msg.get_payload(decode=True).decode("utf-8", errors="replace")
        except: pass
    return ""

M = imaplib.IMAP4_SSL("imap.gmail.com")
M.login(USER, PASS)
M.select('"[Gmail]/Sent Mail"')

# Sent on May 19, 2026
st, ids = M.search(None, '(SENTSINCE "19-May-2026" SENTBEFORE "20-May-2026")')
all_ids = ids[0].split() if ids[0] else []
print(f"Sent on May 19, 2026: {len(all_ids)} messages")

for mid in all_ids:
    st, data = M.fetch(mid, '(BODY.PEEK[])')
    if st != "OK": continue
    msg = email.message_from_bytes(data[0][1])
    subj = decode_h(msg.get("Subject",""))
    to = decode_h(msg.get("To",""))
    cc = decode_h(msg.get("Cc",""))
    date_h = msg.get("Date","")
    try:
        dt = parsedate_to_datetime(date_h)
        tstamp = dt.strftime("%H:%M") if dt else ""
    except: tstamp = ""
    # Has attachment?
    has_attach = False
    if msg.is_multipart():
        for p in msg.walk():
            disp = p.get("Content-Disposition","") or ""
            if "attachment" in disp.lower() or p.get_filename():
                has_attach = True
                break
    flag = "📎" if has_attach else "  "
    looks_proposal = bool(PROP_RE.search(subj))
    tag = "[PROP] " if looks_proposal else "       "
    print(f"  {tstamp}  {flag} {tag}{subj[:70]}")
    print(f"           → TO: {to[:80]}")
    if cc.strip(): print(f"           → CC: {cc[:80]}")

M.logout()
