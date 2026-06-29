#!/usr/bin/env python3
"""Find every proposal sent in the last N days from Gmail Sent that has
NO matching row in CRM. Identifies the missing bids the user is asking about."""
import imaplib, email, os, re, sys
from email.header import decode_header
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta, date

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
except Exception: pass

USER = os.environ.get("GMAIL_USER", "estimates@carolinacommercialfinishes.com")
PASS = os.environ.get("GMAIL_APP_PASSWORD", "")

PROP_KW = re.compile(
    r"(proposal|painting|paint\s*&|paint\s+and|bid\s+(?:for|submission)|"
    r"estimate\s+for|finishes\s+(?:proposal|estimate)|"
    r"carolina\s+commercial)",
    re.I,
)

# We want emails that are HONEST proposals — not chase/follow-up emails
# (those are tracked separately).
SKIP_KW = re.compile(
    r"^\s*(?:Re:|Fwd:|FW:|Following\s*up|Follow[-\s]up|Just\s+checking|"
    r"Hi\s+\w+,?\s+just\s+|Chasing|Last\s+email|Final\s+email|"
    r"Close[-\s]out|RFI\s+|Reminder)",
    re.I,
)

def decode_h(s):
    out = ""
    for p, e in decode_header(s or ""):
        out += p.decode(e or "utf-8", errors="replace") if isinstance(p, bytes) else p
    return out


def main():
    days_back = 7
    since_dt = (date.today() - timedelta(days=days_back))
    print(f"Scanning Gmail Sent from {since_dt} → today")

    M = imaplib.IMAP4_SSL("imap.gmail.com")
    M.login(USER, PASS)
    M.select('"[Gmail]/Sent Mail"')
    st, ids = M.search(None, f'(SENTSINCE "{since_dt.strftime("%d-%b-%Y")}")')
    all_ids = ids[0].split() if ids[0] else []
    print(f"  {len(all_ids)} total messages")

    proposals = []   # (date, time, subject, recipients, has_attach)
    for mid in all_ids:
        st, data = M.fetch(mid, '(BODY.PEEK[])')
        if st != "OK": continue
        msg = email.message_from_bytes(data[0][1])
        subj = decode_h(msg.get("Subject",""))
        to = decode_h(msg.get("To",""))
        date_h = msg.get("Date","")
        try: dt = parsedate_to_datetime(date_h)
        except Exception: dt = None
        # Filter to proposal subjects, skip follow-ups
        if not PROP_KW.search(subj): continue
        if SKIP_KW.search(subj): continue
        # Has PDF attachment? (proposals always have a PDF)
        has_attach = False
        attach_name = ""
        if msg.is_multipart():
            for p in msg.walk():
                disp = (p.get("Content-Disposition") or "").lower()
                fn = p.get_filename() or ""
                if "attachment" in disp or fn:
                    if fn.lower().endswith((".pdf", ".docx", ".doc")):
                        has_attach = True
                        attach_name = fn
                        break
        proposals.append({
            "dt": dt,
            "subj": subj,
            "to": to,
            "has_attach": has_attach,
            "attach_name": attach_name,
        })
    M.logout()

    # Sort newest first
    proposals.sort(key=lambda p: p["dt"] or datetime.min, reverse=True)
    print(f"\n  → {len(proposals)} look like real proposals (PDF attached or strong subject keyword)\n")

    # Group by project-name-extracted from subject (rough)
    def proj_key(subj):
        s = re.sub(r"^\s*(?:CCF\s+)?(?:Painting\s+)?Proposal\s*[—\-–:|]\s*", "", subj, flags=re.I)
        s = re.sub(r"\s*[—\-–|]\s*(?:Painting|Proposal|Bid|Estimate|Wallcovering|CCF|Carolina).*$", "", s, flags=re.I)
        s = re.sub(r"^\s*CCF\s*[—\-–:|]\s*", "", s, flags=re.I)
        # Strip leading FROM CCF / "Painting Proposal" prefix
        s = re.sub(r"^\s*\w+\s+(Painting|Bid|Proposal)\s+", "", s, flags=re.I)
        return s.strip(" -—–|:")[:60]

    by_proj = {}
    for p in proposals:
        k = proj_key(p["subj"])
        by_proj.setdefault(k, []).append(p)

    for k, lst in by_proj.items():
        dates = sorted({p["dt"].strftime("%m/%d") if p["dt"] else "?" for p in lst}, reverse=True)
        gcs = sorted({re.search(r"[\w.+-]+@[\w.-]+", p["to"]).group(0) for p in lst if "@" in p["to"]})
        attaches = sum(1 for p in lst if p["has_attach"])
        print(f"  {k[:55]:<55}  sent={','.join(dates)}  to={len(gcs)} GCs  📎={attaches}/{len(lst)}")
        for p in lst[:3]:
            ts = p["dt"].strftime("%m/%d %H:%M") if p["dt"] else "?"
            print(f"     [{ts}] → {p['to'][:60]:<60}  {'📎' if p['has_attach'] else '  '}  {p['subj'][:60]}")

    # Now compare to CRM
    print("\n" + "="*88 + "\nCROSS-CHECK against CRM:\n" + "="*88)
    sys.path.insert(0, r"C:/Agent Carol/scripts")
    from crm_lib import all_records
    bidlog = all_records("Bid Log")

    def norm_proj(s):
        return re.sub(r"[^a-z0-9]", "", str(s).lower())[:30]

    crm_projs = set()
    for row in bidlog:
        p = row.get("Project Name", "")
        crm_projs.add(norm_proj(p))

    missing = []
    for k, lst in by_proj.items():
        nk = norm_proj(k)
        # Also try the numeric core (e.g. "1336quinton")
        if not nk or len(nk) < 6: continue
        # Look for any CRM row whose normalized name overlaps significantly
        match = False
        for cp in crm_projs:
            if nk in cp or cp in nk:
                if len(nk) >= 8 or len(cp) >= 8:
                    match = True
                    break
        if not match:
            missing.append((k, lst))

    if not missing:
        print("All proposals found in CRM.")
    else:
        print(f"\n⚠️ MISSING from CRM: {len(missing)} project(s)\n")
        for k, lst in missing:
            ts = sorted({p['dt'].strftime('%m/%d') if p['dt'] else '?' for p in lst}, reverse=True)
            print(f"  • {k[:55]}  sent={','.join(ts)}  to {len(lst)} email(s)")
            for p in lst:
                tstr = p['dt'].strftime('%m/%d %H:%M') if p['dt'] else '?'
                print(f"      [{tstr}] {p['to'][:55]:<55}  '{p['subj'][:70]}'")


if __name__ == "__main__":
    main()
