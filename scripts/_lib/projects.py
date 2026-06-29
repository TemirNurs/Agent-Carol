"""Project name normalization & dedupe keys.

Single source of truth for "is this the same project?" decisions. Replaces
the copy-pasted `_project_core` and `slugify` functions that were drifting
in 6+ scripts and caused the 2026-05-21 duplicate-row bug.

AGENTS_LESSONS.md R10: same project named three ways MUST hash to the same key.
"""
from __future__ import annotations
import re


def slugify(name: str | None) -> str:
    """Filesystem/url-safe slug. Lowercase, alphanumeric + hyphens, max 80 chars.

    NOTE: position-sensitive. For dedupe use `project_core()` instead — slug
    treats 'Food Lion #2235 Quinton' and '2235 Food Lion Quinton' as
    DIFFERENT (different slugs), which spawned duplicate CRM rows.
    """
    if not name:
        return ""
    s = re.sub(r"[^a-z0-9\s-]", "", name.lower())
    s = re.sub(r"\s+", "-", s).strip("-")
    return re.sub(r"-+", "-", s)[:80]


def project_core(name: str | None) -> tuple[str, str]:
    """Canonical dedupe key for a project name. Position-agnostic.

    Returns (store_number, two_keyword_tokens) where:
      - store_number is the first 3-5 digit run (with optional letter suffix:
        2118B / 2671B / 2235A). Lstripped of leading zeros.
      - two_keyword_tokens is the first TWO alphabetic tokens (≥3 chars,
        excluding digit-only tokens) after stripping common stop-words.

    All four of these MUST produce the same key:
        "Food Lion #2235 Quinton, VA"
        "2235 Food Lion Quinton, VA"
        "Food Lion 2235 Quinton VA"
        "Follow-Up: Food Lion #2235 Quinton, VA (BID-0024)"
    → ("2235", "food lion")

    Bug history:
      - 2026-05-21: regex \\b\\d{3,5}\\b couldn't match "2118B" (letter suffix)
        → num came back "" → every Food Lion collapsed to same key.
      - 2026-05-22: digit-only tokens like "2235" were included in the
        keyword tokens, so token order mattered → different keys for the
        same project.
    Both fixed below.
    """
    s = (name or "").lower()
    # Number + optional letter suffix, using lookahead instead of \\b boundary
    m = re.search(r"#?\s*(\d{3,5})[a-z]?(?=\s|$|[^\d])", s)
    num = (m.group(1).lstrip("0") or "0") if m else ""
    base = re.sub(r"[^a-z0-9 ]+", " ", s)
    # Strip noise words so the "distinctive" tokens are project keywords
    base = re.sub(
        r"\b(revised|proposal|attached|follow|followup|re|fwd|bid|submission|"
        r"painting|ccf|carolina|commercial|finishes|the|inc|llc|corp|company|"
        r"va|nc|sc|ga|al|ut|tn|quinton|chester|mebane|greensboro|remodel|store|"
        r"building|bldg|grandstands|concept|foods|petersburg|dinwiddie|aylett|"
        r"randleman|salisbury|fayetteville|advance|charlotte|raleigh)\b",
        " ", base,
    )
    base = re.sub(r"\s+", " ", base).strip()
    # First 2 ALPHA tokens. Exclude purely-numeric tokens AND tokens like
    # "2118b" (digits + optional letter suffix — that's a store number
    # written without the "#"). The store number is already in `num`.
    def _is_keyword(t: str) -> bool:
        if len(t) < 3: return False
        if t.isdigit(): return False
        if re.fullmatch(r"\d+[a-z]?", t): return False   # 2118b, 2671b
        return True
    toks = [t for t in base.split() if _is_keyword(t)][:2]
    return (num, " ".join(toks))


def same_project(a: str | None, b: str | None) -> bool:
    """True if two project names refer to the same project."""
    return project_core(a) == project_core(b) and project_core(a) != ("", "")


def normalize_name(s: str | None) -> str:
    """Cleaned display name — strips RE:/FWD: prefixes, trailing CCF boilerplate,
    duplicate halves ('X - Y - X - Y' → 'X - Y'). For UI/CRM display, not dedupe."""
    s = (s or "").strip()
    if not s:
        return ""
    s = re.sub(r"^\s*(?:Re:|RE:|Fwd:|FWD:|FW:|Fw:|Following\s*up:?)\s*", "", s)
    for sep in (" - ", " | ", " / "):
        if sep in s:
            mid = len(s) // 2
            best_idx, best_dist = -1, 9e9
            i = 0
            while True:
                j = s.find(sep, i)
                if j < 0: break
                d = abs(j - mid)
                if d < best_dist:
                    best_dist, best_idx = d, j
                i = j + 1
            if best_idx > 0:
                left = s[:best_idx].strip()
                right = s[best_idx + len(sep):].strip()
                if left and (left.lower() == right.lower()
                             or right.lower().startswith(left.lower())):
                    s = left
                    break
    return s
