"""Gmail — IMAP wrapper used by all Carol email scripts.

Replaces duplicated IMAP / search / parse code in:
  - track_submissions.py
  - proposal_audit.py
  - process_followup_replies.py
  - backfill_contacts.py
  - loss_postmortem.py
  - import_cowork_export.py (read-only)

Usage:
    from scripts._lib import gmail
    with gmail.connect() as M:
        msgs = gmail.search(M, "INBOX", 'subject:"Follow-Up"')
        for hit in msgs:
            print(hit.subject, hit.from_, hit.body[:200])
"""

from __future__ import annotations

import email as email_lib
import imaplib
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from email.header import decode_header
from email.utils import parsedate_to_datetime
from typing import Iterator

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

GMAIL_USER = os.environ.get("GMAIL_USER", "estimates@carolinacommercialfinishes.com")
GMAIL_PASS = os.environ.get("GMAIL_APP_PASSWORD", "")

INBOX = "INBOX"
SENT  = '"[Gmail]/Sent Mail"'
ALL_MAIL = '"[Gmail]/All Mail"'

# Internal CCF + noise senders to skip when looking for "real" GC emails.
# Own-domain substrings come from CCF_OWN_DOMAINS (env) so the lib stays
# company-agnostic; the generic gmail-forward marker stays as a functional entry.
INTERNAL_SENDER_DOMAINS = tuple(
    d.strip() for d in os.environ.get(
        "CCF_OWN_DOMAINS", "carolinacommercialfinishes").split(",") if d.strip()
) + (
    "@gmail.com>",  # often internal-team forwards
)
NOISE_SENDERS = (
    "mailer-daemon", "noreply", "donotreply", "no-reply",
    "isqftmail.com", "buildingconnected.com", "constructconnect",
    "transmittals@", "DoNotReply@constructconnectmail",
)


@dataclass
class Message:
    """One Gmail message with parsed convenience fields."""
    uid: bytes
    subject: str
    from_: str
    to: str
    cc: str
    date: datetime | None
    date_str: str
    message_id: str
    body: str
    raw: bytes = field(repr=False, default=b"")

    @property
    def from_email(self) -> str:
        """Just the email address from From header."""
        m = re.search(r"<([^>]+)>", self.from_)
        return (m.group(1) if m else self.from_).strip()

    @property
    def from_domain(self) -> str:
        em = self.from_email
        if "@" in em:
            return em.split("@", 1)[-1].rstrip(">").strip()
        return ""

    @property
    def is_internal(self) -> bool:
        fl = self.from_.lower()
        return any(d in fl for d in INTERNAL_SENDER_DOMAINS)

    @property
    def is_noise(self) -> bool:
        fl = self.from_.lower()
        return any(s in fl for s in NOISE_SENDERS)


@contextmanager
def connect():
    """Yield an authenticated IMAP4_SSL connection. Auto-logout on exit."""
    M = imaplib.IMAP4_SSL("imap.gmail.com")
    M.login(GMAIL_USER, GMAIL_PASS)
    try:
        yield M
    finally:
        try:
            M.logout()
        except Exception:
            pass


def decode_field(raw) -> str:
    """Decode a possibly-MIME-encoded header value. Unicode-safe."""
    if not raw:
        return ""
    out = ""
    for p, e in decode_header(str(raw)):
        if isinstance(p, bytes):
            try:
                out += p.decode(e or "utf-8", errors="replace")
            except LookupError:
                out += p.decode("utf-8", errors="replace")
        else:
            out += p
    return re.sub(r"\s+", " ", out).strip()


def extract_body(msg: email_lib.message.Message, max_chars: int = 4000) -> str:
    """Pull plain-text body, strip quoted reply blocks. Truncated to max_chars."""
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    try:
                        body = payload.decode(
                            part.get_content_charset() or "utf-8", errors="replace")
                    except (LookupError, AttributeError):
                        body = payload.decode("utf-8", errors="replace")
                    break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            try:
                body = payload.decode(
                    msg.get_content_charset() or "utf-8", errors="replace")
            except (LookupError, AttributeError):
                body = payload.decode("utf-8", errors="replace")
    body = re.sub(r"\r\n?", "\n", body)
    # Strip quoted reply blocks (lines starting with >)
    keep = []
    for ln in body.split("\n"):
        if ln.lstrip().startswith(">"):
            continue
        if re.match(r"^On .+wrote:$", ln.strip()):
            break
        if "From:" in ln and os.environ.get(
                "GMAIL_USER", "estimates@carolinacommercialfinishes.com") in ln:
            break
        keep.append(ln)
    body = "\n".join(keep).strip()
    return body[:max_chars]


def _safe_query(q: str) -> str:
    """Sanitize a query so X-GM-RAW IMAP search doesn't choke."""
    safe = re.sub(r'["\\\x00-\x1f]', " ", q)
    return re.sub(r"\s+", " ", safe).strip()


def search(M: imaplib.IMAP4_SSL, folder: str, gmail_query: str,
           limit: int = 100) -> list[Message]:
    """Run an X-GM-RAW search, return parsed Messages.

    `folder` can be "INBOX", '"[Gmail]/Sent Mail"', etc.
    `gmail_query` uses Gmail's search syntax (from:, subject:, has:, etc.).
    """
    safe = _safe_query(gmail_query)
    if not safe:
        return []
    try:
        M.select(folder, readonly=True)
        typ, data = M.uid("SEARCH", "X-GM-RAW", f'"{safe}"')
    except imaplib.IMAP4.error:
        return []
    if typ != "OK" or not data or not data[0]:
        return []
    uids = data[0].split()[-limit:]
    return [_fetch_message(M, uid) for uid in uids if (parsed := _fetch_message(M, uid))]


def _fetch_message(M: imaplib.IMAP4_SSL, uid: bytes) -> Message | None:
    typ, raw = M.uid('FETCH', uid, '(BODY.PEEK[])')
    if typ != "OK" or not raw or not raw[0]:
        return None
    rawbytes = raw[0][1]
    msg = email_lib.message_from_bytes(rawbytes)
    try:
        dt = parsedate_to_datetime(msg.get("Date", "")) if msg.get("Date") else None
    except Exception:
        dt = None
    return Message(
        uid=uid,
        subject=decode_field(msg.get("Subject")),
        from_=decode_field(msg.get("From")),
        to=decode_field(msg.get("To")),
        cc=decode_field(msg.get("Cc")),
        date=dt,
        date_str=msg.get("Date", ""),
        message_id=(msg.get("Message-ID") or "").strip("<>").strip(),
        body=extract_body(msg),
        raw=b"",  # don't keep raw bytes by default — saves memory
    )


def add_label(M: imaplib.IMAP4_SSL, uids: list[bytes], label: str,
              chunk: int = 100) -> int:
    """Apply a Gmail label to a list of UIDs. Returns count applied."""
    if not uids:
        return 0
    M.select(ALL_MAIL)
    n = 0
    for i in range(0, len(uids), chunk):
        ids = b",".join(uids[i:i+chunk]).decode()
        try:
            M.uid("STORE", ids, "+X-GM-LABELS", f'("{label}")')
            n += min(chunk, len(uids) - i)
        except imaplib.IMAP4.error:
            pass
    return n


def remove_label(M: imaplib.IMAP4_SSL, uids: list[bytes], label: str,
                 chunk: int = 100) -> int:
    if not uids:
        return 0
    M.select(ALL_MAIL)
    n = 0
    for i in range(0, len(uids), chunk):
        ids = b",".join(uids[i:i+chunk]).decode()
        try:
            M.uid("STORE", ids, "-X-GM-LABELS", f'("{label}")')
            n += min(chunk, len(uids) - i)
        except imaplib.IMAP4.error:
            pass
    return n


# ---------- Phone & contact extraction ----------

PHONE_PATTERNS = [
    r"\(\s*\d{3}\s*\)\s*\d{3}\s*[-.\s]?\s*\d{4}",
    r"\b\d{3}\s*[-.\s]\s*\d{3}\s*[-.\s]\s*\d{4}\b",
    r"\+1\s*[-.\s]?\s*\d{3}\s*[-.\s]?\s*\d{3}\s*[-.\s]?\s*\d{4}",
]
PHONE_RE = re.compile("|".join(PHONE_PATTERNS))
PHONE_HINT_RE = re.compile(
    r"(?:^|[\s|])(?:c|cell|phone|mobile|tel|office|p|m|direct)\s*[:.]?\s*"
    + "(?:" + "|".join(PHONE_PATTERNS) + r")",
    re.IGNORECASE | re.MULTILINE,
)
# Phones to skip during extraction = the company's own number(s). Derive the
# 10-digit form from OWNER_PHONE (env); fall back to the company main line.
IGNORE_PHONES = {(re.sub(r"\D", "", os.environ.get("OWNER_PHONE", "")) or "9803481827")[-10:]}


def extract_phones(text: str) -> list[tuple[str, bool]]:
    """Return list of (formatted_phone, is_signal_word_context)."""
    if not text:
        return []
    found, seen = [], set()

    def normalize(p):
        return re.sub(r"\D", "", p)[-10:]

    def fmt(d):
        d = re.sub(r"\D", "", d)
        if len(d) == 11 and d.startswith("1"):
            d = d[1:]
        return f"({d[:3]}) {d[3:6]}-{d[6:]}" if len(d) == 10 else d

    for m in PHONE_HINT_RE.finditer(text):
        nm = normalize(m.group(0))
        if len(nm) >= 10 and nm not in IGNORE_PHONES and nm not in seen:
            seen.add(nm)
            found.append((fmt(nm), True))
    for m in PHONE_RE.finditer(text):
        nm = normalize(m.group(0))
        if len(nm) >= 10 and nm not in IGNORE_PHONES and nm not in seen:
            seen.add(nm)
            found.append((fmt(nm), False))
    return found
