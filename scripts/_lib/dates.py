"""Dates — flexible parsing for the many formats CRM dates arrive in."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

# Formats observed in the CRM Sheet over time
_FORMATS = (
    "%a, %d %b %Y",   # "Tue, 21 Apr 2026"  ← canonical Sheet format
    "%m/%d/%Y",       # "04/21/2026"
    "%Y-%m-%d",       # "2026-04-21"
    "%d %b %Y",       # "21 Apr 2026"
    "%B %d, %Y",      # "April 21, 2026"
    "%m-%d-%Y",
)


def parse(s) -> date | None:
    """Parse a date string, trying every known format. Returns None if all fail.

    Also accepts a datetime/date and returns its date component.
    """
    if s is None or s == "":
        return None
    if isinstance(s, datetime):
        return s.date()
    if isinstance(s, date):
        return s
    s = str(s).strip()
    for fmt in _FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    # Last-resort: pluck mm/dd/yyyy out of a longer string
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
        except (ValueError, TypeError):
            return None
    return None


def days_since(s) -> int | None:
    """Days from the given date to today. None if unparseable."""
    d = parse(s)
    return (date.today() - d).days if d else None


def days_between(a, b) -> int | None:
    """Days from a to b (positive if b > a)."""
    da, db = parse(a), parse(b)
    if not (da and db):
        return None
    return (db - da).days


def fmt(d: date | datetime | None, style: str = "long") -> str:
    """Format a date for display.

    style options:
      'long'      → "Tue, 21 Apr 2026"  (matches CRM)
      'short'     → "04/21/2026"
      'iso'       → "2026-04-21"
      'mmdd'      → "04/21"
      'human'     → "April 21, 2026"
    """
    if d is None:
        return ""
    if isinstance(d, datetime):
        d = d.date()
    if not isinstance(d, date):
        return str(d)
    return {
        "long":  d.strftime("%a, %d %b %Y"),
        "short": d.strftime("%m/%d/%Y"),
        "iso":   d.isoformat(),
        "mmdd":  d.strftime("%m/%d"),
        "human": d.strftime("%B %d, %Y"),
    }.get(style, d.isoformat())


def today_iso() -> str:
    return date.today().isoformat()


def today_mmdd() -> str:
    return date.today().strftime("%m/%d")
