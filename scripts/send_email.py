#!/usr/bin/env python3
"""
CCF Email Sender
Sends emails from estimates@carolinacommercialfinishes.com via Gmail SMTP.
Carol calls this from WhatsApp/Telegram when asked to send reports, proposals, etc.

Usage:
  python send_email.py --to "someone@email.com" --subject "Daily Bid Report" --body "Report text here"
  python send_email.py --to "someone@email.com" --subject "Proposal" --html "<h1>Proposal</h1>"
  python send_email.py --to "someone@email.com" --subject "Docs" --body "See attached" --attach "file.pdf"
"""

import argparse
import json
import smtplib
import sys
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Gmail SMTP config
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SENDER_EMAIL = "estimates@carolinacommercialfinishes.com"
APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
SIG_DIR = Path(__file__).resolve().parent.parent / "data" / "templates"
SIG_TXT = SIG_DIR / "email_signature.txt"
SIG_HTML = SIG_DIR / "email_signature.html"


def _has_signature(text: str) -> bool:
    """Detect if signature is already in the body (avoid double-signing)."""
    if not text: return False
    markers = ("3308 Chancellor", "(980) 348-1827", "Nursultan Temirbaev | Manager")
    return any(m in text for m in markers)


def append_signature(body=None, html=None, skip_signature=False):
    """Append the canonical CCF signature unless already present or skipped."""
    if skip_signature:
        return body, html
    if body is not None and not _has_signature(body) and SIG_TXT.exists():
        body = body.rstrip() + "\n" + SIG_TXT.read_text(encoding="utf-8")
    if html is not None and not _has_signature(html) and SIG_HTML.exists():
        html = html.rstrip() + "\n" + SIG_HTML.read_text(encoding="utf-8")
    return body, html


def _check_recipient_domain(to: str) -> list:
    """Catch obviously-truncated/invalid recipient domains before sending."""
    if not to: return ["empty recipient"]
    import re as _re
    issues = []
    for addr in [a.strip() for a in to.split(",") if a.strip()]:
        # Strip "Name <email>" form
        m = _re.search(r"<([^>]+)>", addr)
        clean = m.group(1) if m else addr
        if "@" not in clean:
            issues.append(f"no @ in '{clean}'")
            continue
        local, domain = clean.rsplit("@", 1)
        # Domain must have a dot (TLD)
        if "." not in domain:
            issues.append(f"domain '{domain}' has no TLD — likely truncated")
            continue
        # Final TLD label must be 2+ chars (.com, .net, .co — not just dot)
        tld = domain.rsplit(".", 1)[-1]
        if len(tld) < 2:
            issues.append(f"TLD '.{tld}' is too short — likely truncated")
        # Main domain segment too short (likely truncated)
        domain_main = domain.rsplit(".", 1)[0]
        if len(domain_main) < 2:
            issues.append(f"domain '{domain}' looks suspiciously short")
    return issues


def _check_for_mangled_dollars(text: str) -> list:
    """Detect telltale signs that $107,773 got mangled to ,773 by harness regex.

    Returns list of suspicious patterns found.
    """
    if not text: return []
    import re as _re
    issues = []
    # Pattern: "our ,NNN" / "the ,NNN" / "our $,NNN" — clear mangling signs
    for m in _re.finditer(r"\b(our|the|a|of|for)\s+\$?,(\d{3,})\b", text, _re.IGNORECASE):
        issues.append(f"mangled-amount near '{m.group(0)}'")
    # Pattern: "$,NNN" with comma right after dollar sign
    for m in _re.finditer(r"\$,\d{3,}", text):
        issues.append(f"mangled-amount '{m.group(0)}'")
    return issues


def send_email(to, subject, body=None, html=None, attachments=None, cc=None, bcc=None,
               skip_signature=False, force=False):
    """Send an email via Gmail SMTP.

    Refuses to send if body contains telltale mangled-dollar patterns
    (e.g. "our ,773 proposal" — the leading "$107" got stripped by harness).
    Override with force=True if the user explicitly wants the suspicious body.
    """
    # Defensive check 1: recipient address looks truncated / fake
    addr_issues = _check_recipient_domain(to)
    if addr_issues and not force:
        msg_back = (
            f"REFUSED — recipient address '{to}' looks invalid: {addr_issues[:3]}. "
            f"Pull the correct address from the CRM 'Contact Email' column "
            f"(via crm_lib.get_sheet('Bid Log').get_all_records()) "
            f"or from the postmortem's GC reply emails. "
            f"DO NOT generate the address from the GC name. Pass --force to override."
        )
        print(msg_back)
        return {"status": "refused", "reason": "bad_recipient", "issues": addr_issues}

    # Defensive check 2: catch the $107,773 → ,773 mangling bug before send
    issues = _check_for_mangled_dollars(body) + _check_for_mangled_dollars(html)
    if issues and not force:
        msg_back = (
            f"REFUSED — body looks like it has mangled dollar amounts: {issues[:3]}. "
            f"This is the known harness bug where $107,773 gets stripped to ,773. "
            f"Fix the body (use 'USD 107,773' or '$ 107,773' with space) and retry, "
            f"or pass --force to send anyway."
        )
        print(msg_back)
        return {"status": "refused", "reason": "mangled_dollars", "issues": issues}

    # Auto-append CCF signature unless caller opted out (e.g. for proposals
    # that already have signature embedded by template)
    body, html = append_signature(body, html, skip_signature=skip_signature)

    msg = MIMEMultipart("alternative")
    msg["From"] = f"Carol - CCF Estimating <{SENDER_EMAIL}>"
    msg["To"] = to
    msg["Subject"] = subject
    if cc:
        msg["Cc"] = cc

    # Body
    if body:
        msg.attach(MIMEText(body, "plain"))
    if html:
        msg.attach(MIMEText(html, "html"))
    if not body and not html:
        msg.attach(MIMEText("", "plain"))

    # Attachments
    if attachments:
        for filepath in attachments:
            fp = Path(filepath)
            if fp.exists():
                with open(fp, "rb") as f:
                    part = MIMEBase("application", "octet-stream")
                    part.set_payload(f.read())
                encoders.encode_base64(part)
                part.add_header("Content-Disposition", f"attachment; filename={fp.name}")
                msg.attach(part)
                print(f"  Attached: {fp.name} ({fp.stat().st_size / 1024:.0f} KB)")

    # Build recipient list
    recipients = [r.strip() for r in to.split(",")]
    if cc:
        recipients.extend([r.strip() for r in cc.split(",")])
    if bcc:
        recipients.extend([r.strip() for r in bcc.split(",")])

    # Send
    try:
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(SENDER_EMAIL, APP_PASSWORD)
        server.sendmail(SENDER_EMAIL, recipients, msg.as_string())
        server.quit()
        print(f"  Email sent to {to}")
        return {"status": "sent", "to": to, "subject": subject}
    except Exception as e:
        print(f"  ERROR: {e}")
        return {"status": "error", "error": str(e)}


def main():
    parser = argparse.ArgumentParser(description="CCF Email Sender")
    parser.add_argument("--to", required=True, help="Recipient email(s), comma-separated")
    parser.add_argument("--subject", required=True, help="Email subject")
    parser.add_argument("--body", default=None, help="Plain text body")
    parser.add_argument("--html", default=None, help="HTML body")
    parser.add_argument("--attach", default=None, help="Comma-separated file paths to attach")
    parser.add_argument("--cc", default=None, help="CC recipients")
    parser.add_argument("--bcc", default=None, help="BCC recipients")
    parser.add_argument("--no-signature", action="store_true",
                        help="Skip auto-append of CCF signature (e.g. for proposals where it's already embedded)")
    parser.add_argument("--force", action="store_true",
                        help="Override safety checks (e.g. mangled-dollar refusal)")
    args = parser.parse_args()

    attachments = [f.strip() for f in args.attach.split(",")] if args.attach else None

    result = send_email(
        to=args.to,
        subject=args.subject,
        body=args.body,
        html=args.html,
        attachments=attachments,
        cc=args.cc,
        bcc=args.bcc,
        skip_signature=args.no_signature,
        force=args.force,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
