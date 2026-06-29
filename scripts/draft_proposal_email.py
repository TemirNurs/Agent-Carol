#!/usr/bin/env python3
"""
draft_proposal_email.py — create a Gmail DRAFT in estimates@ with attachments.

The Gmail MCP connector can't do attachments and targets the wrong mailbox;
this appends a fully-formed MIME draft to [Gmail]/Drafts over IMAP so the
user reviews and presses Send himself (proposals are always human-sent).

Usage:
  python scripts/draft_proposal_email.py --to a@b.com [--cc x@y.com ...]
      --subject "..." --body-file body.txt --attach file.pdf [--attach ...]

CC defaults to the standing policy set (cs@ + accountant + owner).
Signature appended from data/templates/email_signature.txt.
"""
import argparse
import imaplib
import os
import sys
import time
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    pass
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from _lib.gmail import GMAIL_USER, GMAIL_PASS  # noqa: E402

# Standing CC policy: the team estimating inbox plus any internal aliases
# (accountant / owner) configured in the gitignored .env. Falls back to just
# the public team inbox when no internal aliases are set.
POLICY_CC = [os.environ.get("CCF_INTERNAL_CC", "cs@carolinacommercialfinishes.com")]
POLICY_CC += [a.strip() for a in os.environ.get("OWNER_ALIAS_EMAILS", "").split(",")
              if a.strip()]

# 6/12 incident: drafts addressed to platform RELAY robots (BC's team@,
# iSqFt transmittals@) instead of the human. Fresh emails to relays route
# nowhere reliable. Always the person's direct address (invite body / CRM /
# GC directory).
RELAY_BLOCKLIST = ("team@buildingconnected.com", "transmittals@isqftmail",
                   "donotreply", "noreply", "no-reply")
SIG_TXT = ROOT / "data" / "templates" / "email_signature.txt"
DRAFTS = '"[Gmail]/Drafts"'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", required=True, nargs="+")
    ap.add_argument("--cc", nargs="*", default=None)
    ap.add_argument("--subject", required=True)
    ap.add_argument("--body-file", required=True)
    ap.add_argument("--attach", action="append", default=[])
    ap.add_argument("--allow-relay", action="store_true",
                    help="override the relay-address block (rare)")
    args = ap.parse_args()

    if not args.allow_relay:
        for addr in args.to:
            if any(r in addr.lower() for r in RELAY_BLOCKLIST):
                print(f"BLOCKED: {addr} is a platform relay robot, not a "
                      "person. Find the contact's DIRECT email (invite body, "
                      "CRM, gc-contacts.csv) or pass --allow-relay.")
                return 2

    body = Path(args.body_file).read_text(encoding="utf-8")

    # plain-text part (fallback clients) — canonical text signature
    text_body = body
    if SIG_TXT.exists():
        text_body = body.rstrip() + "\n\n" + SIG_TXT.read_text(encoding="utf-8")

    # HTML part — HIS REAL signature (C:/Cowork/_Core/signature.html, logo
    # embedded as base64 data URI). 6/12 user: "use the signature I already
    # have in my email" — never a plain-text imitation again.
    import html as _html
    paras = [p.strip() for p in body.split("\n\n") if p.strip()]
    html_paras = "".join(
        f'<p style="font-family:Arial,Helvetica,sans-serif;font-size:13.5px;'
        f'color:#222;line-height:1.5;margin:0 0 12px;">'
        + _html.escape(p).replace("\n", "<br>") + "</p>"
        for p in paras)
    SIG_REAL = Path("C:/Cowork/_Core/signature.html")
    sig_html = SIG_REAL.read_text(encoding="utf-8") if SIG_REAL.exists() else \
        (Path(ROOT / "data/templates/email_signature.html").read_text(encoding="utf-8")
         if (ROOT / "data/templates/email_signature.html").exists() else "")
    # 6/12 forensics — Gmail kills every embedded-logo route on the DRAFT
    # path: data: URIs blocked on receive, CID images stripped/rewritten by
    # the compose window into private mail.google.com URLs. Only a PUBLIC
    # hosted URL survives drafts. If signature_logo_url is configured (in
    # company_config.yaml), use it; otherwise drop the <img> entirely — a
    # clean text signature beats a broken-image icon. (SMTP sends via
    # send_email.py still carry the CID logo perfectly — chase emails.)
    import re as _re
    logo_url = None
    try:
        import yaml
        cfg = yaml.safe_load((ROOT / "data/config/company_config.yaml")
                             .read_text(encoding="utf-8")) or {}
        logo_url = (cfg.get("email") or {}).get("signature_logo_url")
    except Exception:
        pass
    if logo_url:
        sig_html = _re.sub(r'src="data:image/[^"]+"',
                           f'src="{logo_url}"', sig_html)
    else:
        sig_html = _re.sub(r'<img[^>]+src="data:image/[^"]+"[^>]*/?>', "",
                           sig_html)
    html_body = f"<html><body>{html_paras}{sig_html}</body></html>"
    logo_path = None   # CID path disabled for drafts (compose strips it)

    msg = EmailMessage()
    msg["From"] = f"CCF Estimates <{GMAIL_USER}>"
    msg["To"] = ", ".join(args.to)
    cc = POLICY_CC if args.cc is None else args.cc
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = args.subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    if logo_path and "cid:ccf_logo" in html_body:
        html_part = msg.get_payload()[-1]
        ext = logo_path.suffix.lstrip(".").lower().replace("jpg", "jpeg")
        html_part.add_related(logo_path.read_bytes(), maintype="image",
                              subtype=ext, cid="<ccf_logo>",
                              filename=f"ccf_logo.{logo_path.suffix.lstrip('.')}")

    for a in args.attach:
        p = Path(a)
        data = p.read_bytes()
        ext = p.suffix.lower()
        mt = {".pdf": ("application", "pdf"),
              ".docx": ("application",
                        "vnd.openxmlformats-officedocument.wordprocessingml.document"),
              ".xlsx": ("application",
                        "vnd.openxmlformats-officedocument.spreadsheetml.sheet")}.get(
            ext, ("application", "octet-stream"))
        msg.add_attachment(data, maintype=mt[0], subtype=mt[1], filename=p.name)

    M = imaplib.IMAP4_SSL("imap.gmail.com")
    M.login(GMAIL_USER, GMAIL_PASS)
    try:
        typ, _ = M.append(DRAFTS, r"(\Draft)",
                          imaplib.Time2Internaldate(time.time()),
                          msg.as_bytes())
        print("DRAFT APPEND:", typ, "| to:", msg["To"], "| cc:", msg.get("Cc", ""),
              "| attachments:", len(args.attach))
    finally:
        M.logout()
    return 0 if typ == "OK" else 1


if __name__ == "__main__":
    sys.exit(main())
