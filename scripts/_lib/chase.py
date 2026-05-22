"""Chase / follow-up helpers — reply-awareness, CC list, intervals.

Single source of truth for chase email logic. AGENTS_LESSONS.md R3:
never chase a recipient who's already replied recently.
"""
from __future__ import annotations
import imaplib
import os
from datetime import date, timedelta

# AGENTS_LESSONS.md R4: every chase MUST CC Sviatlana + Sergei + cs@
CC_INTERNAL = "cs@carolinacommercialfinishes.com,wilsonsviatlana83@gmail.com,smayurov@gmail.com"

# Escalating cadence (hours since last chase). Attempt N → wait this many hours.
INTERVAL_HOURS: dict[int, int] = {
    2:  72,   # 3 days — polite check
    3:  24,
    4:  12,
    5:  8,
    6:  6,
    7:  6,
    8:  6,
    9:  6,
    10: 6,
    11: 6,
    12: 6,
}

MAX_ATTEMPTS = 12
DEFAULT_MAX_PER_RECIPIENT_PER_DAY = 3

# Inbox-flag tags that indicate a bid is no longer active for chasing
INACTIVE_TAGS = ("[BOUNCE]", "[NOT BIDDING]", "[WITHDRAWN]", "[ON HOLD]", "[NO BID]")


_REPLY_CACHE: dict[str, bool] = {}


def has_replied_recently(recip_email: str | None, days: int = 14) -> bool:
    """Return True if `recip_email` sent any message to our Gmail inbox in the
    last `days` days. Caches per-process to avoid hammering IMAP across a
    long chase queue.

    AGENTS_LESSONS.md R3: never chase a recipient who's already replied.
    Re-asking after they gave intel erodes the relationship — this was
    the exact bug on 2026-05-21 (13 of 22 chases wasted on replied contacts).
    """
    if not recip_email:
        return False
    key = recip_email.strip().lower()
    if key in _REPLY_CACHE:
        return _REPLY_CACHE[key]
    user = os.environ.get("GMAIL_USER", "estimates@carolinacommercialfinishes.com")
    pw = os.environ.get("GMAIL_APP_PASSWORD", "")
    since = (date.today() - timedelta(days=days)).strftime("%d-%b-%Y")
    try:
        M = imaplib.IMAP4_SSL("imap.gmail.com")
        M.login(user, pw)
        M.select("INBOX")
        st, ids = M.search(None, f'(FROM "{key}" SINCE "{since}")')
        n = len(ids[0].split()) if ids[0] else 0
        M.logout()
    except Exception:
        n = 0
    result = n > 0
    _REPLY_CACHE[key] = result
    return result


def is_chaseable_status(status: str | None) -> bool:
    """True if a CRM Status warrants chasing (active pipeline)."""
    return (status or "").strip() in ("Bid Submitted", "Awaiting Decision")


def has_inactive_flag(notes: str | None) -> bool:
    """True if Notes column contains any inactive flag."""
    if not notes:
        return False
    up = notes.upper()
    return any(tag in up for tag in INACTIVE_TAGS)
