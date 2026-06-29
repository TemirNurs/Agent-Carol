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
import os
import smtplib
import sys
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Auto-load .env so GMAIL_APP_PASSWORD etc. are available when send_email.py
# is invoked as a subprocess (the parent may not export env vars to children).
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:
    pass

# Gmail SMTP config
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SENDER_EMAIL = os.environ.get("GMAIL_USER", "estimates@carolinacommercialfinishes.com")
APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
SIG_DIR = Path(__file__).resolve().parent.parent / "data" / "templates"
SIG_TXT = SIG_DIR / "email_signature.txt"
SIG_HTML = SIG_DIR / "email_signature.html"


def _has_signature(text: str) -> bool:
    """Detect if signature is already in the body (avoid double-signing).

    Require 2+ markers — a single marker (e.g. just the phone number)
    is a false positive. The 5/27 incident: chase bodies that said
    'Happy to jump on a call: (980) 348-1827' were treated as already-signed
    and sent WITHOUT the actual canonical signature, looking unprofessional.
    """
    if not text: return False
    markers = (
        "3308 Chancellor",
        "(980) 348-1827",
        "Nursultan Temirbaev | Manager",
        "estimates@carolinacommercialfinishes.com | CarolinaCommercialFinishes.com",
        "Monroe, NC 28110",
    )
    return sum(1 for m in markers if m in text) >= 2


def append_signature(body=None, html=None, skip_signature=False):
    """Append the canonical CCF signature unless already present or skipped."""
    if skip_signature:
        return body, html
    if body is not None and not _has_signature(body) and SIG_TXT.exists():
        body = body.rstrip() + "\n" + SIG_TXT.read_text(encoding="utf-8")
    if html is not None and not _has_signature(html) and SIG_HTML.exists():
        html = html.rstrip() + "\n" + SIG_HTML.read_text(encoding="utf-8")
    return body, html


def _known_domains_from_history() -> set:
    """Build the set of recipient domains we've successfully delivered to before.
    Reads from data/memory/known_recipient_domains.json (auto-maintained) plus
    a small hardcoded list of obviously-legit CCF correspondent domains."""
    import json as _json
    from pathlib import Path as _Path
    cache_file = _Path(__file__).resolve().parent.parent / "data" / "memory" / "known_recipient_domains.json"
    known = {
        # CCF internal — always-known
        "carolinacommercialfinishes.com", "gmail.com",
    }
    if cache_file.exists():
        try:
            d = _json.loads(cache_file.read_text(encoding="utf-8"))
            known.update(d.get("domains", []))
        except Exception:
            pass
    return known


def _check_recipient_domain(to: str) -> list:
    """Catch obviously-truncated/invalid/typo'd recipient domains before sending."""
    if not to: return ["empty recipient"]
    import re as _re
    issues = []
    known = _known_domains_from_history()
    for addr in [a.strip() for a in to.split(",") if a.strip()]:
        # Strip "Name <email>" form
        m = _re.search(r"<([^>]+)>", addr)
        clean = m.group(1) if m else addr
        if "@" not in clean:
            issues.append(f"no @ in '{clean}'")
            continue
        local, domain = clean.rsplit("@", 1)
        domain = domain.lower()
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
        # Smart check: if domain is unknown to us, see if the same base name
        # exists with a different TLD in our known set — likely typo.
        # Example: amorgan@newcoconstruction.co → we know newcoconstruction.com
        if domain not in known:
            base = domain.rsplit(".", 1)[0]   # "newcoconstruction"
            siblings = [d for d in known if d.startswith(base + ".")]
            if siblings:
                issues.append(
                    f"recipient domain '{domain}' looks like a typo — we've "
                    f"corresponded with {', '.join(siblings)} but not '{domain}'. "
                    f"Did you mean one of those? (Override with --force if intentional)"
                )
    return issues


def _check_for_mangled_dollars(text: str) -> list:
    """Detect telltale signs that $123,456 got mangled to ,456 by harness regex.

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
               skip_signature=False, force=False, internal_id=None):
    """Send an email via Gmail SMTP.

    Refuses to send if body contains telltale mangled-dollar patterns
    (e.g. "our ,456 proposal" — the leading "$107" got stripped by harness).
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

    # Defensive check 2: catch the $123,456 → ,456 mangling bug before send
    issues = _check_for_mangled_dollars(body) + _check_for_mangled_dollars(html)
    if issues and not force:
        msg_back = (
            f"REFUSED — body looks like it has mangled dollar amounts: {issues[:3]}. "
            f"This is the known harness bug where $123,456 gets stripped to ,456. "
            f"Fix the body (use 'USD 123,456' or '$ 123,456' with space) and retry, "
            f"or pass --force to send anyway."
        )
        print(msg_back)
        return {"status": "refused", "reason": "mangled_dollars", "issues": issues}

    # If caller passed plain `--body` only, also build an HTML version so
    # the formatted CCF signature renders properly (5/27 incident: signed
    # plain-text version landed as undecorated dry text). Synthesize HTML by
    # converting newlines + escaping minimal HTML chars; the canonical
    # styled signature is then appended via append_signature.
    auto_html_from_body = False
    if body and not html:
        import html as _html_lib
        body_escaped = _html_lib.escape(body).replace("\n", "<br>\n")
        html = body_escaped
        auto_html_from_body = True

    # Auto-append CCF signature unless caller opted out (e.g. for proposals
    # that already have signature embedded by template)
    body, html = append_signature(body, html, skip_signature=skip_signature)

    # Logo embedding: look for the CCF logo in data/templates/assets/ in
    # several common filenames + extensions (case-insensitive on Windows).
    logo_dir = Path(__file__).resolve().parent.parent / "data" / "templates" / "assets"
    logo_path = None
    if logo_dir.exists():
        for name in ("ccf_logo.png", "ccf_logo.jpg", "CCF_logo.png",
                     "CCF_logo.jpg", "ccf-logo.png", "ccf-logo.jpg"):
            p = logo_dir / name
            if p.exists():
                logo_path = p
                break
    has_logo_cid = html and "cid:ccf_logo" in html and logo_path is not None

    # Container — multipart/related when we have inline images, otherwise alternative
    if has_logo_cid:
        from email.mime.multipart import MIMEMultipart as _MM
        from email.mime.image import MIMEImage
        outer = _MM("related")
        alt = _MM("alternative")
        outer.attach(alt)
        msg = outer
        if body:
            alt.attach(MIMEText(body, "plain"))
        if html:
            alt.attach(MIMEText(html, "html"))
        # Attach the logo as a related part. MIMEImage picks subtype from
        # the file bytes; if we want to be explicit pass _subtype based on ext.
        try:
            ext = logo_path.suffix.lower().lstrip(".")
            subtype = "jpeg" if ext in ("jpg", "jpeg") else (ext or "png")
            with open(logo_path, "rb") as lf:
                img = MIMEImage(lf.read(), _subtype=subtype)
                img.add_header("Content-ID", "<ccf_logo>")
                img.add_header("Content-Disposition", "inline",
                                filename=f"ccf_logo.{ext or 'png'}")
                msg.attach(img)
        except Exception as _ex:
            print(f"  [warn] logo attach failed: {_ex}")
    else:
        msg = MIMEMultipart("alternative")
        if body:
            msg.attach(MIMEText(body, "plain"))
        if html:
            msg.attach(MIMEText(html, "html"))
        if not body and not html:
            msg.attach(MIMEText("", "plain"))

    msg["From"] = f"Carol - CCF Estimating <{SENDER_EMAIL}>"
    msg["To"] = to
    msg["Subject"] = subject
    # Bid ID rides in a HIDDEN header for reply-attribution — NEVER in the
    # GC-visible subject (6/17: the "[ID:xxxxxxxx]" subject tag leaked our internal
    # tracking to the GC). Present on our Sent copy; invisible to the recipient.
    if internal_id:
        msg["X-CCF-Bid"] = internal_id.strip().replace("-", "")[:8]
    if cc:
        msg["Cc"] = cc

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
    parser.add_argument("--internal-id", default=None,
                        help="Bid Internal ID (UUID). If provided, appends [ID:xxxxxxxx] "
                             "to the subject so the reply processor can match the reply "
                             "to the correct bid even after CRM sorts shift Bid# values.")
    args = parser.parse_args()

    attachments = [f.strip() for f in args.attach.split(",")] if args.attach else None

    # 6/17: the bid ID now rides in a HIDDEN header (X-CCF-Bid), NOT the
    # GC-visible subject. The old "[ID:xxxxxxxx]" subject tag leaked our internal
    # tracking code to the GC (the Comfort Inn chase). Keep the subject clean; the
    # ID is attached as a header inside send_email().
    subject = args.subject

    result = send_email(
        to=args.to,
        subject=subject,
        body=args.body,
        html=args.html,
        attachments=attachments,
        cc=args.cc,
        bcc=args.bcc,
        skip_signature=args.no_signature,
        force=args.force,
        internal_id=args.internal_id,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
