"""GC (General Contractor) directory + email-to-GC resolution.

Single source of truth replacing the `_KNOWN_GC_BY_DOMAIN` and
`_gc_info_for_email` copies scattered across crm_writeback, chase scripts.
"""
from __future__ import annotations
import json
import re
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent

# Curated domain → (GC name, primary contact, phone) map.
# The directory holds third-party contact PII, so it is NOT committed — it lives
# in data/config/gc_directory.json (gitignored). This module ships the resolution
# LOGIC; the data loads at runtime. Absent file → empty map (the heuristic
# fallback in info_for_email still derives a company name from the domain).
_GC_DIRECTORY_FILE = BASE / "data" / "config" / "gc_directory.json"


def _load_gc_directory() -> dict[str, tuple[str, str, str]]:
    out: dict[str, tuple[str, str, str]] = {}
    try:
        data = json.loads(_GC_DIRECTORY_FILE.read_text(encoding="utf-8"))
        for dom, row in (data.get("domains") or {}).items():
            row = list(row) + ["", "", ""]
            out[dom.lower()] = (row[0], row[1], row[2])
    except Exception:
        pass
    return out


KNOWN_GC_BY_DOMAIN: dict[str, tuple[str, str, str]] = _load_gc_directory()


_GC_BY_EMAIL_CACHE: dict[str, tuple[str, str, str]] | None = None


def _load_crm_gcs() -> dict[str, tuple[str, str, str]]:
    """Load gc_crm.json if present — extra GCs the user has curated."""
    cache: dict[str, tuple[str, str, str]] = {}
    p = BASE / "data" / "memory" / "gc_crm.json"
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            for gc_name, info in data.items():
                em = (info.get("email") or "").strip().lower()
                if em:
                    cache[em] = (
                        gc_name,
                        info.get("primary_contact", ""),
                        info.get("phone", ""),
                    )
        except Exception:
            pass
    return cache


def domain_of(addr: str | None) -> str:
    """Extract lowercase domain from an email address. Empty string if none."""
    addr = (addr or "").strip().lower()
    if "@" not in addr:
        return ""
    return addr.split("@", 1)[1].split(">")[0].split()[0].split(",")[0].strip()


def info_for_email(email_addr: str | None) -> tuple[str, str, str]:
    """Return (gc_name, contact_name, phone) for a recipient email.

    Lookup priority:
      1. Exact-email match in gc_crm.json
      2. Domain match in KNOWN_GC_BY_DOMAIN
      3. Heuristic: CamelCase the second-level domain
    """
    global _GC_BY_EMAIL_CACHE
    if _GC_BY_EMAIL_CACHE is None:
        _GC_BY_EMAIL_CACHE = _load_crm_gcs()
    em = (email_addr or "").strip().lower()
    if em in _GC_BY_EMAIL_CACHE:
        return _GC_BY_EMAIL_CACHE[em]
    dom = domain_of(em)
    if dom in KNOWN_GC_BY_DOMAIN:
        return KNOWN_GC_BY_DOMAIN[dom]
    if dom:
        sld = dom.split(".")[0]
        parts = re.findall(r"[A-Za-z][a-z]+|\d+", sld.replace("-", " "))
        return (" ".join(p.title() for p in parts) if parts else sld.title(), "", "")
    return ("", "", "")
