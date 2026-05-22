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
# Add entries as new GCs appear. Hand-curated because raw domain guessing
# produces ugly names like "Fiicgc" or "Newco".
KNOWN_GC_BY_DOMAIN: dict[str, tuple[str, str, str]] = {
    "fiicgc.com":              ("Farris Interior Installation", "Tanner Barber", "706-974-5698"),
    "rcsconstruction.com":     ("RCS Construction",             "Kelly Odegard", "(651) 324-7388"),
    "salcoacontracting.com":   ("Salcoa Contracting",           "J Triplett",    "(704) 638-2357"),
    "vertexconstruction.com":  ("Vertex Construction",          "S Thurston",    ""),
    "newcoconstruction.com":   ("NewCo Construction",           "K Oliver",      ""),
    "pkwycon.com":             ("Parkway Construction",         "",              "(469) 968-4201"),
    "lfjennings.com":          ("LF Jennings",                  "Dan Ahles",     "919-830-6466"),
    "ljennings.com":           ("LF Jennings",                  "Dan Ahles",     "919-830-6466"),
    "valiantconstruct.com":    ("Valiant Construction",         "Yoanny, Noah",  ""),
    "msquareus.com":           ("Msquare US",                   "",              ""),
    "cinderellapartners.com":  ("Cinderella Partners",          "",              ""),
    "drivencontractors.com":   ("Driven Contractors",           "",              ""),
    "wedconstruction.com":     ("WED Construction",             "Mitchel Anderson",""),
    "wedentmonconst.com":      ("W.E. Dentmon Construction",    "Mitchel Anderson",""),
    "horizonretail.com":       ("Horizon Retail Construction",  "Tanya Moore",   "262-865-6160"),
    "wimcocorp.com":           ("WIMCO",                        "Susu",          "(502) 354-0387"),
    "windlecc.com":            ("Windle Construction",          "Jimmy Windle",  "434-528-8570"),
    "rickshipman.com":         ("Rick Shipman Construction",    "Anthony Poland","573-624-5065"),
    "metrolinabuilders.com":   ("Metrolina Builders",           "Nathan Crowell","704-553-0834"),
    "pathcc.com":              ("Path Construction",            "Debbie Eaker",  "847-997-3028"),
    "flblum.com":              ("Blum Construction",            "Kim Lockwood",  "336-608-8633"),
    "cmcbuildinginc.com":      ("CMC Building",                 "Parin Bodiwala","919-295-2163"),
    "delauterinc.com":         ("Delauter INC",                 "Justin Hibbard",""),
    "diamondcontractors.com":  ("Diamond Contractors",          "Andrea Farley", ""),
    "csgcharleston.com":       ("CSG Charleston",               "Trevor",        ""),
    "integrity-cm.com":        ("Integrity Construction",       "Taylor Davis",  "(470) 380-4455"),
    "actionrcs.com":           ("Action Roof Construction Services","Zane Denton","(214) 989-7841"),
    "criticalpathsolutions.com":("Critical Path Solutions",     "Richard Tice",  "(910) 745-8112"),
    "mreconstructionllc.com":  ("MRE Construction LLC",         "Cavin Taylor",  "(817) 475-0759"),
    "baytobayprop.com":        ("Bay to Bay",                   "Whitney Wilder","(727) 483-9512"),
    "baytobayproperties.com":  ("Bay to Bay",                   "Whitney Wilder","(727) 483-9512"),
}


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
