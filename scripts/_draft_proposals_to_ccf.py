#!/usr/bin/env python3
"""One-off: drop 6 proposal drafts directly into the Drafts folder of
estimates@carolinacommercialfinishes.com (NOT Hyperscale) via IMAP APPEND.
"""
import imaplib, os, sys, time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formatdate, make_msgid

try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

USER = os.environ.get("GMAIL_USER", "estimates@carolinacommercialfinishes.com")
PASS = os.environ.get("GMAIL_APP_PASSWORD", "")
CC   = "cs@carolinacommercialfinishes.com"

SIG = (
    "\n\nNursultan Temirbaev\n"
    "Estimator | Carolina Commercial Finishes / Budget Painting and Wallcovering LLC\n"
    "3308 Chancellor Lane, Monroe NC 28110\n"
    "(980) 348-1827 · cs@carolinacommercialfinishes.com\n"
)

BODY_2118B = (
    "Hi {name},\n\n"
    "Please find attached Carolina Commercial Finishes' painting proposal for "
    "the Food Lion #2118-B Remodel at 13103 Boydton Plank Road, Dinwiddie VA.\n\n"
    "  TOTAL BID PRICE: $32,126.00\n"
    "  Bid Date: May 21, 2026, 2:00 PM (PMWeb submission)\n\n"
    "Scope covers interior + exterior painting per Div 09 91 13 and the 5/6/26 "
    "walk notes — sales floor walls/columns, restrooms, all HM doors and frames, "
    "front facade EIFS + brick repaint (Popular Gray / Eider White / Food Lion "
    "Blue), bollards, rooftop steel, anti-rodent coating below dock doors, and "
    "exposed structure at sales floor. All Sherwin-Williams, National Account "
    "#C137.\n\n"
    "{closing}"
)

BODY_2671B = (
    "Hi {name},\n\n"
    "Please find attached Carolina Commercial Finishes' painting proposal for "
    "the Food Lion #2671-B Remodel at 2120 S. Crater Road, Petersburg VA.\n\n"
    "  TOTAL BID PRICE: $31,729.00\n"
    "  Bid Date: May 21, 2026, 2:00 PM (PMWeb submission)\n\n"
    "Scope covers interior + exterior painting per Div 09 91 13 and the 5/6/26 "
    "walk notes — sales floor walls/columns, prep room cleanable walls "
    "(Macropoxy 646), backroom + rodent stripe, all HM doors and frames, "
    "marlite repaint, exterior metals + bollards, and exposed structure at "
    "sales floor. All Sherwin-Williams, National Account #C137.\n\n"
    "{closing}"
)

DRAFTS = [
    # 2118B (3 GCs)
    {"to": "jimmy@windlecc.com",                "name": "Jimmy",   "tpl": BODY_2118B,
     "subj": "CCF Painting Proposal — Food Lion #2118-B Remodel, Dinwiddie VA — $32,126",
     "closing": "Happy to walk through any line item. Please let me know if you have any questions or want clarifications before bid time.\n\nThank you for the opportunity to bid."},
    {"to": "estimating@wedconstruction.com",    "name": "Mitchel", "tpl": BODY_2118B,
     "subj": "CCF Painting Proposal — Food Lion #2118-B Remodel, Dinwiddie VA — $32,126",
     "closing": "Happy to clarify any line item before bid time. Thank you for inviting us."},
    {"to": "anthonypoland@rickshipman.com",     "name": "Anthony", "tpl": BODY_2118B,
     "subj": "CCF Painting Proposal — Food Lion #2118-B Remodel, Dinwiddie VA — $32,126",
     "closing": "Happy to walk through any line item before bid time. Thank you for the opportunity."},
    # 2671B (3 GCs)
    {"to": "jimmy@windlecc.com",                "name": "Jimmy",   "tpl": BODY_2671B,
     "subj": "CCF Painting Proposal — Food Lion #2671-B Remodel, Petersburg VA — $31,729",
     "closing": "Happy to clarify any item before bid time. Thank you for the opportunity to bid."},
    {"to": "estimating@wedconstruction.com",    "name": "Mitchel", "tpl": BODY_2671B,
     "subj": "CCF Painting Proposal — Food Lion #2671-B Remodel, Petersburg VA — $31,729",
     "closing": "Happy to clarify any item before bid time. Thank you for inviting us."},
    {"to": "anthonypoland@rickshipman.com",     "name": "Anthony", "tpl": BODY_2671B,
     "subj": "CCF Painting Proposal — Food Lion #2671-B Remodel, Petersburg VA — $31,729",
     "closing": "Happy to walk through any line item before bid time. Thank you for the opportunity to bid."},
]


def build(d):
    msg = MIMEMultipart()
    msg["From"] = USER
    msg["To"] = d["to"]
    msg["Cc"] = CC
    msg["Subject"] = d["subj"]
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="carolinacommercialfinishes.com")
    body = d["tpl"].format(name=d["name"], closing=d["closing"]) + SIG
    msg.attach(MIMEText(body, "plain", "utf-8"))
    return msg


def main():
    M = imaplib.IMAP4_SSL("imap.gmail.com")
    M.login(USER, PASS)
    # Gmail's drafts folder
    DRAFTS_FOLDER = '"[Gmail]/Drafts"'
    placed = 0
    for d in DRAFTS:
        msg = build(d)
        flags = "(\\Draft)"
        date = imaplib.Time2Internaldate(time.time())
        st, resp = M.append(DRAFTS_FOLDER, flags, date, msg.as_bytes())
        if st == "OK":
            placed += 1
            print(f"  ✓ draft → {d['to']:<35}  {d['subj'][:60]}")
        else:
            print(f"  ✗ FAIL  → {d['to']}  resp={resp!r}")
    M.logout()
    print(f"\n[done] placed {placed}/{len(DRAFTS)} drafts in {USER} Drafts")


if __name__ == "__main__":
    main()
