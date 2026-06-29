"""Money — parse messy currency strings, format consistently."""

from __future__ import annotations

import re


def parse(v) -> float:
    """Convert anything currency-shaped to a float. Returns 0 on failure."""
    if v is None or v == "":
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[^\d.\-]", "", str(v))
    try:
        return float(s) if s else 0.0
    except ValueError:
        return 0.0


def fmt(n: float, *, compact: bool = False) -> str:
    """Format a number as USD.

    compact=False (default): "$385,757"
    compact=True:            "$385K" / "$1.45M"  (for tight UI / Telegram)
    """
    n = float(n or 0)
    if compact:
        if n >= 1_000_000:
            return f"${n/1_000_000:.2f}M"
        if n >= 10_000:
            return f"${n/1000:.0f}K"
        return f"${n:,.0f}"
    return f"${n:,.0f}"


def fmt_safe(n: float) -> str:
    """Format for OUTBOUND emails — uses 'USD' prefix to avoid the harness $1
    backreference bug that strips leading digits.

    See: process_followup_replies.py + send_email.py mangling-dollar history.
    """
    n = float(n or 0)
    return f"USD {n:,.0f}"
