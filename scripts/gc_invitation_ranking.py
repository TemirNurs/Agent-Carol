#!/usr/bin/env python3
"""GC invitation ranking — pull all bid invitations, resolve real GC behind portal forwards."""

from __future__ import annotations

import email as elib
import imaplib
import re
import sys
from collections import Counter, defaultdict
from email.header import decode_header
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

GMAIL_USER = "estimates@carolinacommercialfinishes.com"
GMAIL_PASS = os.environ.get("GMAIL_APP_PASSWORD", "")
# Map known GC email domains to canonical company names
DOMAIN_TO_GC = {
    "wimcocorp.com": "WIMCO General Contracting",
    "buch.us.com": "Buch Construction",
    "walshgroup.com": "The Walsh Group",
    "pkwycon.com": "Parkway Construction",
    "fiicgc.com": "Farris Construction (FII)",
    "windlecc.com": "Windle Construction",
    "lfjennings.com": "LF Jennings",
    "hmkern.com": "H. M. Kern Corporation",
    "pathcc.com": "Path Construction",
    "horizonretail.com": "Horizon Retail Construction",
    "criticalpathsolutions.com": "Critical Path Solutions",
    "cmcbuildinginc.com": "CMC Building",
    "integrity-cm.com": "Integrity Construction Management",
    "metrolinabuilders.com": "Metrolina Builders",
    "pointercg.com": "Pointer Construction Group",
    "valiantconstruct.com": "Valiant Construction",
    "flblum.com": "Blum Construction",
    "mreconstructionllc.com": "MRE Construction",
    "baytobayproperties.com": "Bay to Bay Properties",
    "csgcharleston.com": "CSG Charleston",
    "diamondcontractors.com": "Diamond Contractors",
    "actionrcs.com": "Action RCS / Retail Construction",
    "eatonprojects.com": "Eaton Construction Services",
    "speedwellconstruction.com": "Speedwell Construction",
    "summitgc.net": "Summit GC",
    "sempertekinc.com": "Semper Tek",
    "delauterinc.com": "Delauter Inc",
    "dlpconstruction.com": "DLP Construction",
    "abgbuilds.com": "ABG Builds",
    "aeilersconstruction.com": "A Eilers Construction",
    "thrivecc.com": "Thrive Construction LLC",
    "bench-mark.com": "Benchmark Building",
    "singletoncc.com": "Singleton Construction",
    "rectenwald.com": "Rectenwald Brothers Construction",
    "mcclureassoc.com": "McClure & Associates",
    "rwallen.com": "R.W. Allen, LLC",
    "monteithconstruction.com": "Monteith Construction Corp.",
    "groupiii.com": "Group III Management",
    "emjcorp.com": "EMJ Construction",
    "gen-con.com": "Gen-Con Group LLC",
    "barconstructionco.com": "Bar Construction Company",
    "harrod-and-assoc.com": "Harrod & Associates Constructors",
    "wedentmonconstruction.com": "W.E. Dentmon Construction",
    "coopertacia.com": "Cooper Tacia General Contractors",
    "ccpsols.com": "Critical Path Solutions",
}

INVITATION_PATTERNS = (
    "invitation to bid", "bid invite", "invited to bid",
    "reminder to bid", "bid reminder", "rfq", "rfp",
)
SKIP_SUBJECT = (
    "daily leads", "daily project update", "daily bid report",
    "newsletter", "submitted bids", "sent proposals", "follow-up:",
    "set your procore password", "your procore password",
)
SKIP_INTERNAL = (
    "estimates@carolinacommercialfinishes",
    "cs@carolinacommercialfinishes",
    "wilsonsviatlana83", "smayurov@gmail",
)
PORTAL_DOMAINS = (
    "isqftmail", "smartbidnet", "smartbid.co",
    "procoretech", "procore.com", "buildingconnected",
)


def get_html_body(msg) -> str:
    if msg.is_multipart():
        for p in msg.walk():
            if p.get_content_type() == "text/html":
                pl = p.get_payload(decode=True)
                if pl:
                    try:
                        return pl.decode(p.get_content_charset() or "utf-8", errors="replace")
                    except Exception:
                        return pl.decode("utf-8", errors="replace")
    elif msg.get_content_type() == "text/html":
        pl = msg.get_payload(decode=True)
        if pl:
            return pl.decode("utf-8", errors="replace")
    return ""


def clean(html: str) -> str:
    h = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html,
               flags=re.DOTALL | re.IGNORECASE)
    t = re.sub(r"<[^>]+>", " ", h)
    t = re.sub(r"&nbsp;", " ", t)
    t = re.sub(r"&amp;", "&", t)
    t = re.sub(r"&#\d+;", "", t)
    return re.sub(r"\s+", " ", t).strip()


def decode_header_value(s: str) -> str:
    if not s:
        return ""
    out = ""
    for p, enc in decode_header(s):
        if isinstance(p, bytes):
            try:
                out += p.decode(enc or "utf-8", errors="replace")
            except LookupError:
                out += p.decode("utf-8", errors="replace")
        else:
            out += p
    return re.sub(r"\s+", " ", out).strip()


def is_invitation(subject: str, body_text: str) -> bool:
    s = (subject or "").lower()
    b = (body_text or "")[:1500].lower()
    if any(p in s for p in SKIP_SUBJECT):
        return False
    if any(p in s for p in INVITATION_PATTERNS):
        return True
    if "bids due" in b or "invitation to bid" in b:
        return True
    return False


def project_key(subject: str) -> str:
    """Normalize a subject down to a canonical project identifier.

    'Reminder to submit your Bid for Food Lion 0440A - Greensboro NC'
    'Food Lion 0440A - Greensboro: Invitation to bid on Food Lion 0440A'
    'Invitation to Bid - Food Lion 0440A - Greensboro NC : Main Trades'
       → all collapse to: 'food lion 0440a greensboro'
    """
    if not subject:
        return ""
    s = subject.strip()
    # Strip common reminder/invite prefixes
    s = re.sub(r"^(?:Re:|Fwd:|RE:|Fw:|FW:)\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(
        r"^(?:Reminder\s+to\s+(?:submit\s+your\s+Bid\s+for|Bid)|"
        r"Invitation\s+to\s+Bid(?:\s*[-–:]\s*)|"
        r"Bid\s+Invite(?:\s*[-–:]\s*)|"
        r"Invited\s+to\s+Bid(?:\s*[-–:]\s*))\s*",
        "", s, flags=re.IGNORECASE,
    )
    # Strip "Bid Reminder Notice" suffix
    s = re.sub(r"\s*[-–:]\s*Bid\s+Reminder\s+Notice.*$", "", s, flags=re.IGNORECASE)
    # Strip ": Invitation to bid on ..." trailing
    s = re.sub(r"\s*:\s*Invitation\s+to\s+bid.*$", "", s, flags=re.IGNORECASE)
    # Strip ": Main Trades" / ": All Trades" trailing
    s = re.sub(r"\s*:\s*(Main|All|Multiple|Sub)\s+Trades?.*$", "", s, flags=re.IGNORECASE)
    # Strip trailing "(Carolinas)" / "(NC)" or addenda markers
    s = re.sub(r"\s*\(?(?:Addendum|Addenda)\b.*$", "", s, flags=re.IGNORECASE)
    # Collapse whitespace + lowercase
    s = re.sub(r"\s+", " ", s).strip().lower()
    # Strip trailing punctuation
    s = s.rstrip(":-– .,")
    return s


def find_gc_in_body(body_html: str) -> str | None:
    """Look in cleaned body for: an 'issued by X' phrase OR a non-portal email domain."""
    text = clean(body_html)
    # 'issued by X Construction' / 'on behalf of X' patterns
    for m in re.finditer(
        r"(?:issued by|on behalf of|sent by|from)\s+"
        r"([A-Z][A-Za-z .&,\-]+?(?:Construction|Contracting|Builders|Building|Group|Inc|LLC|Corp|Company))",
        text, re.IGNORECASE,
    ):
        return m.group(1).strip().rstrip(",.")
    # Email domain fallback — first non-portal/internal/free domain
    for m in re.finditer(r"[\w.+\-]+@([\w\-]+\.[\w.\-]+)", text):
        d = m.group(1).lower()
        if any(s in d for s in PORTAL_DOMAINS):
            continue
        if any(s in d for s in ("gmail", "yahoo", "outlook", "hotmail",
                                  "carolinacommercialfinishes",
                                  "constructconnect")):
            continue
        if d in DOMAIN_TO_GC:
            return DOMAIN_TO_GC[d]
        return d  # raw domain — better than nothing
    return None


def canonicalize_gc(name: str) -> str:
    """Normalize GC name variants into one canonical label.

    Handles: 'W. E Dentmon' vs 'W.E. Dentmon', 'Eaton Construction' vs 'Eaton
    Construction Services', domain-form vs full-name, etc.
    """
    if not name:
        return name
    n = name.strip().rstrip(".").strip()
    nl = n.lower()
    # Domain-form fallback → check both DOMAIN_TO_GC and aliases dict
    if "." in nl and " " not in nl:
        nl_clean = nl.rstrip(".")
        if nl_clean in DOMAIN_TO_GC:
            return DOMAIN_TO_GC[nl_clean]
        # fall through to aliases lookup below
    # Manual aliases — observed variants in the wild
    aliases = {
        "w. e dentmon construction": "W.E. Dentmon Construction",
        "w.e. dentmon construction": "W.E. Dentmon Construction",
        "wedentmonconstruction.com": "W.E. Dentmon Construction",
        "eaton construction": "Eaton Construction Services",
        "eaton construction services": "Eaton Construction Services",
        "eatonprojects.com": "Eaton Construction Services",
        "monteith construction": "Monteith Construction Corp.",
        "monteithco.com": "Monteith Construction Corp.",
        "monteithconstruction.com": "Monteith Construction Corp.",
        "central builders": "Central Builders, Inc. of Mebane",
        "centralbuildersinc.com": "Central Builders, Inc. of Mebane",
        "harrodandassoc.com": "Harrod & Associates Constructors",
        "groupiiimgt.com": "Group III Management",
        "danddcc.com": "Daniels and Daniels Construction",
        "elderjones.com": "Elder-Jones, Inc.",
        "mcknightconstructionco.com": "McKnight Construction Company",
        "salcoacontracting.com": "Salcoa Contracting Inc",
        "tysoncon.com": "Tyson & Associates",
        "hickory-construction.com": "Hickory Construction Company",
        "thriveconstructionllc.com": "Thrive Construction LLC",
        "thrivecc.com": "Thrive Construction LLC",
        "wimco": "WIMCO General Contracting",
        "wimcocorp.com": "WIMCO General Contracting",
        "lf jennings": "LF Jennings",
        "l.f. jennings": "LF Jennings",
        "lfjennings.com": "LF Jennings",
        "h. m. kern corporation": "H. M. Kern Corporation",
        "h.m. kern": "H. M. Kern Corporation",
        "hmkern.com": "H. M. Kern Corporation",
        "cooper tacia general contractors": "Cooper Tacia General Contractors",
        "coopertacia.com": "Cooper Tacia General Contractors",
        "cmc building": "CMC Building, Inc",
        "cmc building, inc": "CMC Building, Inc",
        "cmcbuildinginc.com": "CMC Building, Inc",
        "critical path solutions": "Critical Path Solutions",
        "criticalpathsolutions.com": "Critical Path Solutions",
        "ccpsols.com": "Critical Path Solutions",
        "farris construction": "Farris Construction (FII)",
        "fiicgc.com": "Farris Construction (FII)",
        "ib builders": "IB Builders Inc",
        "tyndall air force base to langley air force base. const": "Tyndall→Langley AFB Construction (project name)",
        "budget painting and wallc": "(internal — should not be ranked)",
    }
    return aliases.get(nl, n)


def resolve_gc(from_field: str, html_body: str) -> str:
    fr = (from_field or "").strip()
    m = re.match(r'"?([^"<]+?)"?\s*<([^>]+)>', fr)
    display = (m.group(1).strip() if m else fr).strip()
    email = (m.group(2).strip() if m else fr).strip().rstrip(">")
    domain = email.split("@")[-1].lower() if "@" in email else ""

    # Direct domain match
    if domain in DOMAIN_TO_GC:
        return DOMAIN_TO_GC[domain]

    # Portal-forwarded — look in body
    if any(p in domain for p in PORTAL_DOMAINS):
        gc = find_gc_in_body(html_body)
        if gc:
            return gc
        # iSqFt: display name IS the GC
        if "isqftmail" in domain:
            return display
        # BC: display has "(GC)" pattern
        if "buildingconnected" in domain:
            mb = re.search(r"\(([^)]+)\)", display)
            if mb:
                return mb.group(1).strip()
        return f"{domain} (unattributed)"

    # Free email — return display name
    if domain in ("gmail.com", "yahoo.com", "outlook.com", "hotmail.com"):
        return display

    # Anything else — try domain mapping or return raw domain
    return DOMAIN_TO_GC.get(domain, domain or display or "Unknown")


def main():
    print("Re-scanning Gmail (resolving real GC behind portal forwards)...\n")
    M = imaplib.IMAP4_SSL("imap.gmail.com")
    M.login(GMAIL_USER, GMAIL_PASS)
    M.select("INBOX")

    queries = (
        "Invitation to Bid newer_than:365d",
        "Bid Invite newer_than:365d",
        "invited to bid newer_than:365d",
        "Reminder to Bid newer_than:365d",
        "from:smartbidnet newer_than:365d",
    )

    seen = set()
    # gc_unique_projects[gc] = set of normalized project keys
    gc_unique_projects = defaultdict(set)
    gc_total_emails = Counter()  # raw email count for context
    for q in queries:
        # X-GM-RAW expects a single quoted argument; strip any internal quotes
        safe = q.replace('"', " ").strip()
        typ, data = M.uid("SEARCH", "X-GM-RAW", f'"{safe}"')
        if typ != "OK" or not data[0]:
            continue
        for uid in data[0].split():
            if uid in seen:
                continue
            seen.add(uid)
            typ, raw = M.uid('FETCH', uid, '(BODY.PEEK[])')
            if typ != "OK" or not raw or not raw[0]:
                continue
            msg = elib.message_from_bytes(raw[0][1])
            fr = msg.get("From", "") or ""
            if any(s in fr.lower() for s in SKIP_INTERNAL):
                continue
            subj = decode_header_value(msg.get("Subject", ""))
            html = get_html_body(msg)
            if not is_invitation(subj, clean(html)):
                continue
            gc = resolve_gc(fr, html)
            gc = canonicalize_gc(gc)
            if gc and len(gc) >= 2 and "internal" not in gc.lower():
                pk = project_key(subj)
                if pk:
                    gc_unique_projects[gc].add(pk)
                gc_total_emails[gc] += 1
    M.logout()

    # Build per-GC counts: unique projects + total emails (incl. reminders)
    gc_unique_count = Counter({gc: len(projs) for gc, projs in gc_unique_projects.items()})
    total_unique = sum(gc_unique_count.values())
    total_emails = sum(gc_total_emails.values())
    print(f"{total_unique} UNIQUE projects from {len(gc_unique_count)} GCs")
    print(f"({total_emails} total emails — incl. {total_emails - total_unique} reminders/dupes)\n")
    print(f"{'#':>3}  {'Unique':>6}  {'Emails':>6}  GC")
    print("=" * 70)
    for i, (gc, n) in enumerate(gc_unique_count.most_common(40), 1):
        emails = gc_total_emails.get(gc, n)
        print(f"{i:>3}  {n:>6}  {emails:>6}  {gc[:55]}")


if __name__ == "__main__":
    main()
