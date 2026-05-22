#!/usr/bin/env python3
"""One-off: hunt Gmail for emails belonging to specific GCs.
Searches body+headers, extracts ALL emails found, filters to plausible GC contacts."""
import imaplib, email, os, re, sys
from collections import Counter
from email.header import decode_header

try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

USER = os.environ.get('GMAIL_USER', 'estimates@carolinacommercialfinishes.com')
PASS = os.environ.get('GMAIL_APP_PASSWORD', '')

EMAIL_RE = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')

SKIP_DOMAINS = (
    'gmail.com', 'carolinacommercialfinishes.com', 'isqftmail.com',
    'buildingconnected.com', 'constructconnectmail.com', 'constructconnect.com',
    'procoretech.com', 'smartbidnet.com', 'mail.smartbidnet.com', 'noreply',
    'no-reply', 'sherwin', 'benjaminmoore', 'ppgindustries', 'fastsigns',
    'anthropic.com',
)

def decode_h(s):
    out = ''
    for p, e in decode_header(s or ''):
        out += p.decode(e or 'utf-8', errors='replace') if isinstance(p, bytes) else p
    return out


def body_text(msg):
    out = []
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() in ('text/plain', 'text/html'):
                try:
                    out.append(part.get_payload(decode=True).decode('utf-8', errors='replace'))
                except Exception:
                    pass
    else:
        try:
            out.append(msg.get_payload(decode=True).decode('utf-8', errors='replace'))
        except Exception:
            pass
    return '\n'.join(out)


def hunt(needle_terms, label):
    M = imaplib.IMAP4_SSL('imap.gmail.com')
    M.login(USER, PASS)
    M.select('INBOX')
    candidates = Counter()
    contexts = {}
    for term in needle_terms:
        st, ids = M.search(None, f'(TEXT "{term}")')
        if not ids[0]: continue
        msg_ids = ids[0].split()[-30:]
        for mid in msg_ids:
            st, data = M.fetch(mid, '(BODY.PEEK[])')
            if st != 'OK': continue
            msg = email.message_from_bytes(data[0][1])
            from_h = decode_h(msg.get('From',''))
            to_h = decode_h(msg.get('To',''))
            subj = decode_h(msg.get('Subject',''))
            body = body_text(msg)
            blob = from_h + ' ' + to_h + ' ' + subj + ' ' + body
            for em in EMAIL_RE.findall(blob):
                e_low = em.lower()
                if any(skip in e_low for skip in SKIP_DOMAINS):
                    continue
                # Must look plausibly like the GC's actual address
                candidates[em.lower()] += 1
                if em.lower() not in contexts:
                    contexts[em.lower()] = (from_h[:50], subj[:70])
    M.logout()
    return candidates, contexts


for label, terms in [
    ("DENTMON",  ["Dentmon"]),
    ("RPM",      ["RPM"]),
]:
    print(f"\n===== {label} =====")
    cand, ctx = hunt(terms, label)
    # Sort by occurrences desc; show top 20
    for em, n in cand.most_common(20):
        frm, subj = ctx.get(em, ('',''))
        print(f"  {n:>3}× {em:<42}  FROM={frm}  SUBJ={subj}")
