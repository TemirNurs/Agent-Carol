#!/usr/bin/env python3
"""
Push Carol's bid pipeline updates back into the CRM Google Sheet.

DESIGN PRINCIPLE: Preserve all user-controlled fields. Carol only writes:
  - New rows for bids in active_bids.json that have no CRM match
  - Auto-fields on existing matches: ITB Received Date, Bid Due Date,
    Status (only if blank), Bid Source, Bid Submitted Date

NEVER touched by Carol:
  - Loss Reason, Notes, Contact Phone, FU columns, Award Date, Win/Loss
  - Any column the user has already filled in

Workflow:
  1. Read existing rows from "Bid Log" tab in the Google Sheet
  2. Fuzzy-match Carol's active_bids → existing CRM rows
  3. For unmatched Carol bids, append new rows with auto-incremented Bid #
  4. For matched bids, fill blank auto-fields only (don't overwrite)
  5. Use gspread batch_update to apply all changes in one API call

Usage:
  python scripts/crm_writeback.py                       # dry-run (default)
  python scripts/crm_writeback.py --apply               # write changes
  python scripts/crm_writeback.py --quiet --apply       # daemon mode
  python scripts/crm_writeback.py --notify --apply      # send Telegram on changes
"""

import argparse
import difflib
import json
import os
import re
import sys
from datetime import datetime, date
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Always snap values for dropdown columns to valid options before writing
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from crm_lookups import canonicalize as _canon
except Exception:
    def _canon(col, val): return val

BASE = Path(__file__).resolve().parent.parent
ACTIVE_BIDS  = BASE / "data" / "memory" / "active_bids.json"
STATUS_FILE  = BASE / "data" / "memory" / "bid_status.json"

# Map Carol's lifecycle status → CRM Status text
STATUS_TO_CRM = {
    "invited":         "ITB Received",
    "accepted":        "ITB Received",
    "docs_pulled":     "Estimating",
    "sow_done":        "Estimating",
    "takeoff_done":    "Estimating",
    "estimate_done":   "Pending Review",
    "proposal_ready":  "Pending Review",
    "submitted":       "Bid Submitted",
    "won":             "Won",
    "lost":            "Lost",
    "no_bid":          "No Bid",
    "declined":        "Declined",
    "past_due":        "",
}

SOURCE_TO_CRM = {
    "buildingconnected": "Plan Room",
    "constructconnect":  "Plan Room",
    "email":             "Invitation (GC)",
}


def normalize(s):
    if not s: return ""
    s = re.sub(r"#\s*\d+", "", str(s).lower())
    s = re.sub(r"[(),\-/_]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


_STATE_ABBREV = {
    "alabama":"AL","alaska":"AK","arizona":"AZ","arkansas":"AR","california":"CA",
    "colorado":"CO","connecticut":"CT","delaware":"DE","florida":"FL","georgia":"GA",
    "hawaii":"HI","idaho":"ID","illinois":"IL","indiana":"IN","iowa":"IA","kansas":"KS",
    "kentucky":"KY","louisiana":"LA","maine":"ME","maryland":"MD","massachusetts":"MA",
    "michigan":"MI","minnesota":"MN","mississippi":"MS","missouri":"MO","montana":"MT",
    "nebraska":"NE","nevada":"NV","new hampshire":"NH","new jersey":"NJ","new mexico":"NM",
    "new york":"NY","north carolina":"NC","north dakota":"ND","ohio":"OH","oklahoma":"OK",
    "oregon":"OR","pennsylvania":"PA","rhode island":"RI","south carolina":"SC",
    "south dakota":"SD","tennessee":"TN","texas":"TX","utah":"UT","vermont":"VT",
    "virginia":"VA","washington":"WA","west virginia":"WV","wisconsin":"WI","wyoming":"WY",
}
def _canon_state(s):
    if not s: return ""
    s = str(s).strip().lower()
    return _STATE_ABBREV.get(s, s[:2].upper() if len(s) <= 3 else s.upper())


def match_score(a, b, gc_a=None, gc_b=None, state_a=None, state_b=None):
    """Fuzzy project-name match with optional GC + state boost.
    GC match alone is worth +0.20 (lots of CCF bids cluster under same GC).
    State match alone is worth +0.05. Distinctive 4+ digit numbers
    (store IDs like Food Lion 2235) match boost +0.15.

    GUARDS:
      - Boosts only apply when token_overlap >= 0.30 AND raw_ratio >= 0.40.
        Below that, a coincidental shared word like "Suites" with same-GC
        could falsely match two different projects.
      - State MISMATCH (canonicalized: "North Carolina" == "NC") subtracts
        0.30 — same-GC chain hotels in different states are different bids.
      - Different store IDs hard-cap at 0.20."""
    na, nb = normalize(a), normalize(b)
    if not na or not nb: return 0.0
    raw_ratio = difflib.SequenceMatcher(None, na, nb).ratio()
    base = raw_ratio
    tokens_a = {t for t in na.split() if len(t) >= 4}
    tokens_b = {t for t in nb.split() if len(t) >= 4}
    token_overlap = 0.0
    if tokens_a and tokens_b:
        token_overlap = len(tokens_a & tokens_b) / max(len(tokens_a), len(tokens_b))
        base = (base + 0.5 * token_overlap) / 1.5

    nums_a = set(re.findall(r"\d{3,}", str(a) if a else ""))
    nums_b = set(re.findall(r"\d{3,}", str(b) if b else ""))
    if nums_a and nums_b:
        if nums_a & nums_b:
            base += 0.15
        else:
            return min(base, 0.20)

    # State mismatch penalty — same-GC chain locations in different states
    # are different bids. Always applied (no guard).
    sa_c, sb_c = _canon_state(state_a), _canon_state(state_b)
    if sa_c and sb_c and sa_c != sb_c:
        base -= 0.30

    # Boost guard: require both meaningful token overlap AND a high-enough
    # raw similarity. Either alone isn't enough.
    can_boost = (token_overlap >= 0.30) and (raw_ratio >= 0.40)
    if can_boost:
        if gc_a and gc_b:
            gca, gcb = normalize(gc_a), normalize(gc_b)
            if gca and gcb:
                gc_sim = difflib.SequenceMatcher(None, gca, gcb).ratio()
                if gc_sim >= 0.7:
                    base += 0.20
                elif gc_sim < 0.3:
                    base -= 0.10
        if sa_c and sb_c and sa_c == sb_c:
            base += 0.05
    return min(max(base, 0.0), 1.0)


def parse_date(s):
    if not s: return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try: return datetime.strptime(str(s).strip(), fmt).date()
        except (ValueError, AttributeError): pass
    return None


def _parse_email_date(s):
    """Parse RFC-2822 email date strings like 'Thu, 23 Apr 2026 14:17:21 -0400'."""
    if not s: return None
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(str(s).strip())
        return dt.date() if dt else None
    except Exception:
        return None


def slugify(name):
    if not name: return ""
    s = re.sub(r"[^a-z0-9\s-]", "", name.lower())
    s = re.sub(r"\s+", "-", s).strip("-")
    return re.sub(r"-+", "-", s)[:80]


def _project_core(name):
    """Reduce a project name to a dedupe key: (store/bldg number, keyword stem).
    'Food Lion #2219 Quinton, VA' and '2219 Food Lion Quinton, VA:' and
    'Follow-Up: Food Lion #2219 ...' all collapse to the same core so we never
    create a second CRM row for a project we already track.

    NB: store numbers often have an alpha suffix ('Food Lion #2118B Dinwiddie',
    '#2671B Petersburg'). The number-capture must NOT require a word boundary
    after the digits or 2118B → "" (matching every other Food Lion as the
    same core, collapsing all rows in dedupe). Allow optional [a-z] suffix."""
    s = (name or "").lower()
    num = ""
    m = re.search(r"#?\s*(\d{3,5})[a-z]?(?=\s|$|[^\d])", s)
    if m:
        num = m.group(1).lstrip("0") or "0"
    base = re.sub(r"[^a-z0-9 ]+", " ", s)
    base = re.sub(
        r"\b(revised|proposal|attached|follow up|followup|re|fwd|bid|submission|"
        r"painting|ccf|carolina|commercial|finishes|the|inc|llc|corp|company|"
        r"va|nc|sc|ga|al|ut|tn|quinton|chester|mebane|greensboro|remodel|store|"
        r"building|bldg|grandstands|concept|foods)\b", " ", base)
    base = re.sub(r"\s+", " ", base).strip()
    # First 2 distinctive ALPHABETIC tokens (filter out digit-only tokens —
    # the store/project number is already in `num`, so including it in tokens
    # makes "2235 Food Lion" → ("2235","2235 food") and "Food Lion #2235" →
    # ("2235","food lion"), producing different keys for the same project.
    toks = [t for t in base.split() if len(t) >= 3 and not t.isdigit()][:2]
    return (num, " ".join(toks))


def _email_domain(addr):
    addr = (addr or "").strip().lower()
    if "@" not in addr:
        return ""
    return addr.split("@", 1)[1].split()[0].split(",")[0].strip()


# ---------------------------------------------------------------------------
# GC info lookup by email (for multi-GC submissions)
# ---------------------------------------------------------------------------
_GC_BY_EMAIL_CACHE = None

# Known domain -> proper GC name mapping. Add entries as new GCs appear.
# Curated because domain-based guessing produces "Fiicgc" / "Newco" / etc.
_KNOWN_GC_BY_DOMAIN = {
    "fiicgc.com":              ("Farris Interior Installation", "Tanner Barber", "706-974-5698"),
    "rcsconstruction.com":     ("RCS Construction",             "Kelly Odegard", "(651) 324-7388"),
    "salcoacontracting.com":   ("Salcoa Contracting",           "J Triplett",    "(704) 638-2357"),
    "vertexconstruction.com":  ("Vertex Construction",          "S Thurston",    ""),
    "newcoconstruction.com":   ("NewCo Construction",           "K Oliver",      ""),
    "pkwycon.com":             ("Parkway Construction",         "",              "(469) 968-4201"),
    "lfjennings.com":          ("LF Jennings",                  "Dan Ahles",     "919-830-6466"),
    "ljennings.com":           ("LF Jennings",                  "Dan Ahles",     "919-830-6466"),
    "valiantconstruct.com":    ("Valiant Construction",         "",              ""),
    "msquareus.com":           ("Msquare US",                   "",              ""),
    "cinderellapartners.com":  ("Cinderella Partners",          "",              ""),
    "drivencontractors.com":   ("Driven Contractors",           "",              ""),
    "wedconstruction.com":     ("WED Construction",             "",              ""),
    "horizonretail.com":       ("Horizon Retail Construction",  "",              "262-865-6160"),
    "wimcocorp.com":           ("WIMCO",                        "Susu",          "(502) 354-0387"),
    "windlecc.com":            ("Windle Construction",          "Jimmy Windle",  ""),
    "fiicgc.com":              ("Farris Interior Installation", "Tanner Barber", "706-974-5698"),
    "metrolinabuilders.com":   ("Metrolina Builders",           "Nathan Crowell","704-553-0834"),
    "pathcc.com":              ("Path Construction",            "Debbie Eaker",  "847-997-3028"),
    "flblum.com":              ("Blum Construction",            "Kim Lockwood",  "336-608-8633"),
    "cmcbuildinginc.com":      ("CMC Building",                 "Parin Bodiwala","919-295-2163"),
    "delauterinc.com":         ("Delauter INC",                 "Justin Hibbard",""),
    "diamondcontractors.com":  ("Diamond Contractors",          "Andrea Farley", ""),
    "valiantconstruct.com":    ("Valiant Construction",         "Yoanny, Noah",  ""),
    "csgcharleston.com":       ("CSG Charleston",               "Trevor",        ""),
    "integrity-cm.com":        ("Integrity Construction",       "Taylor Davis",  "(470) 380-4455"),
    "actionrcs.com":           ("Action Roof Construction Services","Zane Denton","(214) 989-7841"),
    "criticalpathsolutions.com":("Critical Path Solutions",     "Richard Tice",  "(910) 745-8112"),
    "mreconstructionllc.com":  ("MRE Construction LLC",         "Cavin Taylor",  "(817) 475-0759"),
    "horizonretail.com":       ("Horizon Retail Construction",  "Tanya Moore",   "262-865-6160"),
    "baytobayprop.com":        ("Bay to Bay",                   "Whitney Wilder","(727) 483-9512"),
    "fiicgc.com":              ("Farris Interior Installation", "Tanner Barber", "706-974-5698"),
}

def _gc_info_for_email(email_addr):
    """Look up GC name + contact name + phone for a recipient email.
    Returns (gc_name, contact_name, phone) — empty strings if unknown.
    Priority:
      1. Exact email match in gc_crm.json
      2. Known domain map (hand-curated above)
      3. Heuristic: CamelCase the domain second-level"""
    global _GC_BY_EMAIL_CACHE
    if _GC_BY_EMAIL_CACHE is None:
        _GC_BY_EMAIL_CACHE = {}
        gc_path = BASE / "data" / "memory" / "gc_crm.json"
        if gc_path.exists():
            try:
                gc_data = json.loads(gc_path.read_text(encoding="utf-8"))
                for gc_name, info in gc_data.items():
                    em = (info.get("email") or "").strip().lower()
                    if em:
                        _GC_BY_EMAIL_CACHE[em] = (
                            gc_name,
                            info.get("primary_contact", ""),
                            info.get("phone", ""),
                        )
            except Exception:
                pass
    em = (email_addr or "").strip().lower()
    if em in _GC_BY_EMAIL_CACHE:
        return _GC_BY_EMAIL_CACHE[em]
    if "@" in em:
        domain = em.split("@", 1)[1].lower()
        if domain in _KNOWN_GC_BY_DOMAIN:
            return _KNOWN_GC_BY_DOMAIN[domain]
        # Fallback: derive from second-level domain (e.g. acme-corp.com -> "Acme Corp")
        sld = domain.split(".")[0]
        if sld:
            parts = re.findall(r"[A-Za-z][a-z]+|\d+", sld.replace("-", " "))
            gc_guess = " ".join(p.title() for p in parts) if parts else sld.title()
            return (gc_guess, "", "")
    return ("", "", "")


def crm_status_for(override):
    if not override: return ""
    return STATUS_TO_CRM.get(override.get("status", ""), "")


def telegram_notify(text):
    try:
        import urllib.request, urllib.parse
        tok = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat = os.environ.get("USER_TELEGRAM_CHAT_ID", "")
        data = urllib.parse.urlencode({
            "chat_id": chat, "text": text, "parse_mode": "Markdown"
        }).encode("utf-8")
        urllib.request.urlopen(
            urllib.request.Request(
                f"https://api.telegram.org/bot{tok}/sendMessage",
                data=data, method="POST"),
            timeout=10)
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="Actually write changes (default is dry-run)")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--threshold", type=float, default=0.55,
                    help="Match threshold. 0.55 + GC-name boost (+0.20) "
                         "catches Carvana/Adesa (score ~0.73 with GC=Parkway). "
                         "Lowering this caused false positives — keep at 0.55.")
    ap.add_argument("--notify", action="store_true",
                    help="Send Telegram alert when CRM is updated")
    args = ap.parse_args()

    def log(*a, **k):
        if not args.quiet: print(*a, **k)

    sys.path.insert(0, str(BASE / "scripts"))
    from crm_lib import workbook, get_sheet, all_records, append_row, append_rows, batch_update_rows, next_bid_id

    # Read Bid Log + capture row indices for updates
    bid_sheet = get_sheet("Bid Log")
    headers = bid_sheet.row_values(1)
    all_rows = bid_sheet.get_all_values()
    existing = []
    for r_idx, row in enumerate(all_rows[1:], start=2):
        d = {h: (row[i] if i < len(row) else "") for i, h in enumerate(headers)}
        if not d.get("Bid #"): continue
        if not d.get("Project Name"): continue
        existing.append({"row_idx": r_idx, "data": d})
    log(f"[crm-wb] existing CRM rows: {len(existing)}")

    # Compute next bid ID
    max_num = 0
    for e in existing:
        m = re.search(r"BID-(\d{4,})", e["data"].get("Bid #", ""))
        if m:
            try: max_num = max(max_num, int(m.group(1)))
            except: pass
    log(f"[crm-wb] next available Bid #: BID-{max_num+1:04d}")

    # Load Carol's data
    bids = json.load(open(ACTIVE_BIDS, encoding="utf-8"))
    overrides = {}
    if STATUS_FILE.exists():
        d = json.load(open(STATUS_FILE, encoding="utf-8"))
        overrides = d.get("overrides", {})
    log(f"[crm-wb] Carol active bids: {len(bids)}")

    # === ORPHAN SYNTHESIS ===
    # A proposal we SENT may have no scraped bid-invitation in active_bids.json
    # (got it by phone/email, or the scraper named it differently). track_
    # submissions still records it in bid_status.json with a 'project_name' and
    # orphan=True. Without this block those proposals would never reach the CRM
    # because the matching loop below only iterates active_bids.
    #
    # CRITICAL: the existence check uses BOTH (a) project_core (number+keyword
    # tokens) AND (b) the CRM rows already in `existing`. The previous slugify-
    # based check missed format variants ("2235 Food Lion" vs "Food Lion #2235"
    # → different slugs → "orphan" created → duplicate row added even though
    # the project was already in CRM). user 2026-05-22: "why are you
    # duplicated them in a first place?"
    _existing_slugs = {slugify(b.get("project_name", "")) for b in bids}
    _existing_cores = {_project_core(b.get("project_name", "")) for b in bids}
    # Also include every CRM row's project_core so orphan can't spawn a row
    # that's already in the sheet under a different name format.
    for _e in existing:
        _pn = (_e["data"].get("Project Name") or "").strip()
        if _pn:
            _existing_cores.add(_project_core(_pn))
    _orphans_added = 0
    for slug, ovr in overrides.items():
        if ovr.get("status") not in ("submitted", "won", "lost", "no_bid"):
            continue
        proj = (ovr.get("project_name") or "").strip()
        if not proj:
            continue
        # Check by slug AND by project_core — either match = skip
        if slug in _existing_slugs:
            continue
        if _project_core(proj) in _existing_cores:
            continue
        # Derive GC + state from the first submission's recipient domain
        subs = ovr.get("submissions") or []
        recip = ""
        if subs:
            recip = (subs[0].get("to") or "").strip().lower()
        elif ovr.get("submitted_to"):
            recip = ovr["submitted_to"].strip().lower()
        domain = recip.split("@", 1)[1] if "@" in recip else ""
        DOMAIN_GC = {
            "pkwycon.com": "Parkway Construction",
            "wcconstructionco.com": "W.C. Construction Company",
            "fiicgc.com": "Farris Interior Installation",
            "newcoconstruction.com": "Newco Construction",
            "lfjennings.com": "LF Jennings",
            "wimcocorp.com": "WIMCO",
            "flblum.com": "Blum Construction",
            "monteithco.com": "Monteith Construction",
        }
        gc_name = DOMAIN_GC.get(domain, domain.split(".")[0].title() if domain else "")
        bids.append({
            "project_name": proj,
            "gc": gc_name,
            "city": "",
            "state": "",
            "source": "orphan-capture",
            "_orphan": True,
        })
        _orphans_added += 1
    if _orphans_added:
        log(f"[crm-wb] synthesized {_orphans_added} orphan bid(s) from bid_status "
            f"(sent proposals with no scraped invitation)")

    # Multi-GC aware matching:
    # Each Carol bid may have multiple submissions (one per GC the proposal
    # was emailed to). Each (bid, submission) pair maps to its own CRM row,
    # keyed by (project name match) + (contact email exact match).
    SUBMITTED_STATES = {"submitted", "won", "lost", "no_bid"}

    def _submissions_for(ab):
        """Return list of submission dicts for a Carol bid. Falls back to a
        single legacy submission record if the new array isn't populated yet."""
        slug = slugify(ab.get("project_name", ""))
        ovr = overrides.get(slug, {})
        subs = ovr.get("submissions") or []
        if subs:
            return subs
        if ovr.get("status") in SUBMITTED_STATES and ovr.get("submitted_to"):
            return [{
                "to": (ovr.get("submitted_to","") or "").strip().lower(),
                "at": ovr.get("submitted_at",""),
                "subject": ovr.get("submitted_subject",""),
                "amount": ovr.get("amount"),
            }]
        return []

    def _grouped_submissions(ab):
        """Collapse multiple submissions to the same recipient into one entry,
        keeping the latest date/amount (e.g. when a bid is REVISED and resent
        to the same GC). One CRM row per unique recipient — never per send."""
        groups = {}
        for s in _submissions_for(ab):
            r = (s.get("to") or "").strip().lower()
            if not r: continue
            existing_entry = groups.get(r)
            if existing_entry is None:
                groups[r] = dict(s)
                continue
            # Keep the entry with the LATER date (revision wins)
            cur_d = parse_date(existing_entry.get("at","")) or _parse_email_date(existing_entry.get("at",""))
            new_d = parse_date(s.get("at","")) or _parse_email_date(s.get("at",""))
            if new_d and (not cur_d or new_d >= cur_d):
                # Take the newer record but preserve to_display if missing
                merged = dict(s)
                if not merged.get("to_display") and existing_entry.get("to_display"):
                    merged["to_display"] = existing_entry["to_display"]
                groups[r] = merged
        return list(groups.values())

    # Pass 1: for each (Carol bid, submission) pair, try to find a CRM row
    # matching by (project name >= 0.50) AND (Contact Email exact).
    used_rows = set()
    matched_pairs = []      # list of (ab, sub_or_None, crm_row)
    unmatched_pairs = []    # list of (ab, sub_or_None) needing a new CRM row
    bids_with_subs = 0

    for ab in bids:
        subs = _grouped_submissions(ab)
        if subs:
            bids_with_subs += 1
            for sub in subs:
                recipient = (sub.get("to") or "").strip().lower()
                found = None
                for i, e in enumerate(existing):
                    if i in used_rows: continue
                    row_email = (e["data"].get("Contact Email") or "").strip().lower()
                    if recipient and row_email and row_email == recipient:
                        pn_score = match_score(
                            ab.get("project_name",""), e["data"].get("Project Name") or "",
                            gc_a=ab.get("gc",""), gc_b=e["data"].get("GC / Client",""),
                            state_a=ab.get("state",""), state_b=e["data"].get("State",""),
                        )
                        if pn_score >= 0.50:
                            found = (i, e, pn_score)
                            break
                if found is None:
                    # Fall back to project-name-only match (legacy rows without email)
                    best_idx, best_score = None, 0
                    for i, e in enumerate(existing):
                        if i in used_rows: continue
                        if (e["data"].get("Contact Email") or "").strip():
                            continue  # row HAS an email but it doesn't match — skip
                        s = match_score(
                            ab.get("project_name",""), e["data"].get("Project Name") or "",
                            gc_a=ab.get("gc",""), gc_b=e["data"].get("GC / Client",""),
                            state_a=ab.get("state",""), state_b=e["data"].get("State",""),
                        )
                        if s > best_score:
                            best_score, best_idx = s, i
                    if best_score >= args.threshold and best_idx is not None:
                        found = (best_idx, existing[best_idx], best_score)
                if found:
                    used_rows.add(found[0])
                    matched_pairs.append((ab, sub, found[1]))
                else:
                    unmatched_pairs.append((ab, sub))
        else:
            # No submissions yet — try a project-only match for due-date/source updates
            best_idx, best_score = None, 0
            for i, e in enumerate(existing):
                if i in used_rows: continue
                s = match_score(
                    ab.get("project_name",""), e["data"].get("Project Name") or "",
                    gc_a=ab.get("gc",""), gc_b=e["data"].get("GC / Client",""),
                    state_a=ab.get("state",""), state_b=e["data"].get("State",""),
                )
                if s > best_score:
                    best_score, best_idx = s, i
            if best_score >= args.threshold and best_idx is not None:
                used_rows.add(best_idx)
                matched_pairs.append((ab, None, existing[best_idx]))
            # No submission and no match => skip (CRM tracks SUBMITTED bids only
            # per owner's standing rule — un-bid invitations live in
            # active_bids.json and surface via bids_today.py / Telegram briefs,
            # not in the CRM Bid Log).

    log(f"[crm-wb] {bids_with_subs} bids have submission records | "
        f"matched pairs: {len(matched_pairs)} | new rows to create: {len(unmatched_pairs)}")

    # Backward-compat aliases for downstream code paths
    matched = [(ab, e, 1.0) for ab, sub, e in matched_pairs]

    # Build queue of placeholder rows (Bid# present, Project Name empty) for in-place fill.
    # IMPORTANT: skip any placeholder whose Bid# is ALSO claimed by a real-data row
    # elsewhere (e.g. row 52 has placeholder BID-0051 AND row 79 has real BID-0051 —
    # filling the placeholder would create duplicate Bid#s).
    in_use_bid_ids = {e["data"].get("Bid #", "").strip()
                      for e in existing
                      if e["data"].get("Bid #", "").strip()}
    placeholder_rows = []  # list of {"row_idx": int, "bid_id": str}
    skipped_placeholders = 0
    for r_idx, row in enumerate(all_rows[1:], start=2):
        d = {h: (row[i] if i < len(row) else "") for i, h in enumerate(headers)}
        bid_id = d.get("Bid #", "").strip()
        proj = d.get("Project Name", "").strip()
        if bid_id and not proj:
            if bid_id in in_use_bid_ids:
                skipped_placeholders += 1
                continue
            placeholder_rows.append({"row_idx": r_idx, "bid_id": bid_id})
    log(f"[crm-wb] empty placeholder rows available: {len(placeholder_rows)} "
        f"(skipped {skipped_placeholders} duplicate-Bid# placeholders)")

    # === Compute updates for matched (bid, submission, CRM row) triples ===
    updates = []  # (row_idx, col_name, value)
    update_count = 0
    for ab, sub, e in matched_pairs:
        slug = slugify(ab.get("project_name", ""))
        ovr = overrides.get(slug, {})
        row_idx = e["row_idx"]
        d = e["data"]
        changes = []
        # Bid Due Date — same for all GCs of a bid
        if "Bid Due Date" in headers and not str(d.get("Bid Due Date","")).strip():
            due = parse_date(ab.get("due_date", ""))
            if due:
                updates.append((row_idx, "Bid Due Date", due.strftime("%m/%d/%Y")))
                changes.append("due")
        # Bid Source (canonicalize to dropdown option)
        if "Bid Source" in headers and not str(d.get("Bid Source","")).strip():
            raw_src = SOURCE_TO_CRM.get(ab.get("source",""), "")
            src = _canon("Bid Source", raw_src) or raw_src
            if src:
                updates.append((row_idx, "Bid Source", src))
                changes.append("source")
        # Status (canonicalize) + Win/Loss mirror
        if "Status" in headers and not str(d.get("Status","")).strip():
            raw_s = crm_status_for(ovr)
            s = _canon("Status", raw_s) or raw_s
            if s:
                updates.append((row_idx, "Status", s))
                changes.append(f"status->{s}")
                if "Win/Loss" in headers and not str(d.get("Win/Loss","")).strip():
                    wl = {"Bid Submitted": "PENDING",
                          "Awaiting Decision": "PENDING",
                          "Won": "WIN",
                          "Lost": "LOSS"}.get(s)
                    if wl:
                        updates.append((row_idx, "Win/Loss", wl))
                        changes.append(f"winloss->{wl}")
        # Per-submission fields: date + amount (from the specific GC submission)
        submitted_at_raw = (sub or {}).get("at") or ovr.get("submitted_at", "")
        if "Bid Submitted Date" in headers and not str(d.get("Bid Submitted Date","")).strip():
            if submitted_at_raw:
                sub_date = parse_date(submitted_at_raw) or _parse_email_date(submitted_at_raw)
                if sub_date:
                    updates.append((row_idx, "Bid Submitted Date", sub_date.strftime("%m/%d/%Y")))
                    changes.append(f"submitted_date->{sub_date.strftime('%m/%d/%Y')}")
        if "Bid Amount ($)" in headers and not str(d.get("Bid Amount ($)","")).strip():
            amt = (sub or {}).get("amount")
            if amt:
                updates.append((row_idx, "Bid Amount ($)", amt))
                changes.append(f"amount->{amt}")
        if changes:
            update_count += 1
            sub_tag = f" [{(sub or {}).get('to','—')[:25]}]" if sub else ""
            log(f"  UPDATE {d.get('Bid #','?'):<10} {ab.get('project_name','')[:35]:<35}{sub_tag} -> {', '.join(changes)}")

    # === Compute new rows ===
    # CRITICAL: Only ADD rows for bids the user has ACTUALLY SUBMITTED.
    # The CRM Bid Log is the user's CURATED list — not a dump of every
    # invitation. Carol must NEVER add rows for projects we just got
    # invited to but haven't bid on. Only add rows when:
    #   (a) override status is "submitted" / "won" / "lost" (real engagement), OR
    #   (b) user explicitly tells Carol to track a project
    #
    # PLACEMENT: The user pre-allocates empty Bid# placeholder rows
    # (e.g. BID-0050..BID-0076 with col-A only) — we fill those IN-PLACE
    # using the placeholder's existing Bid#. Only when placeholders run out
    # do we append at the bottom with a freshly minted Bid#.
    new_rows = []           # rows to append at bottom (no placeholder available)
    placeholder_fills = []  # (row_idx, row_dict) for in-place writes
    placeholder_queue = list(placeholder_rows)  # FIFO by row order
    next_num = max_num + 1

    # Sort unmatched (bid, submission) pairs by submission date (earliest first)
    # so Bid# numbering follows chronological send order.
    def _sort_key_pair(pair):
        ab, sub = pair
        sub_date_raw = ""
        if sub:
            sub_date_raw = sub.get("at", "") or ""
        if not sub_date_raw:
            slug = slugify(ab.get("project_name", ""))
            ovr = overrides.get(slug, {})
            sub_date_raw = ovr.get("submitted_at", "") or ""
        d = parse_date(sub_date_raw) or _parse_email_date(sub_date_raw)
        return d or date.max
    unmatched_pairs = sorted(unmatched_pairs, key=_sort_key_pair)

    for ab, sub in unmatched_pairs:
        slug = slugify(ab.get("project_name", ""))
        ovr = overrides.get(slug, {})
        # Without a submission record, only create row when bid_status explicitly
        # marks the bid as submitted/won/lost (legacy single-GC fallback).
        if not sub and ovr.get("status") not in SUBMITTED_STATES:
            continue

        # === HARD DEDUPE GUARD ===
        # NEVER create a second CRM row for a project we already track with the
        # SAME GC. bids@fiicgc.com and tanner.barber@fiicgc.com are both Farris
        # — different inbox, same GC, same bid. The old (project + exact-email)
        # match treated them as distinct and spawned duplicate rows every time
        # the daemon re-captured a send to the alternate inbox. Dedupe on
        # (project-core + GC email-domain) instead.
        _new_recip = (sub or {}).get("to", "").strip().lower()
        _new_dom = _email_domain(_new_recip)
        _new_core = _project_core(ab.get("project_name", ""))
        # CRITICAL: when one project is submitted to multiple GCs, every
        # submission shares ab["gc"] (the source bid's invitation GC). Using
        # that for dedupe falsely collapses Windle / Dentmon / Rick Shipman
        # into "same GC". Compute the GC for THIS specific submission from
        # the recipient first, fall back to ab only if recipient unresolvable.
        _sub_gc, _, _ = _gc_info_for_email(_new_recip) if _new_recip else ("","","")
        if not _sub_gc:
            _sub_gc = ab.get("gc", "")
        _is_dup = False
        for _e in existing:
            _ed = _e["data"]
            if _project_core(_ed.get("Project Name", "")) != _new_core:
                continue
            _e_dom = _email_domain(_ed.get("Contact Email", ""))
            # same project-core AND (same GC domain OR same GC company name)
            if (_new_dom and _e_dom and _new_dom == _e_dom) or (
                _sub_gc and _ed.get("GC / Client", "")
                and _sub_gc.strip().lower()[:12] == _ed["GC / Client"].strip().lower()[:12]):
                _is_dup = True
                break
        if _is_dup:
            log(f"  SKIP-DUP {ab.get('project_name','')[:40]} -> {_new_recip[:30]} "
                f"(same project+GC already in CRM)")
            continue
        # Choose Bid#: use lowest-numbered empty placeholder if available
        if placeholder_queue:
            ph = placeholder_queue.pop(0)
            bid_id = ph["bid_id"]
            target_row_idx = ph["row_idx"]
            placement = f"fill row {target_row_idx}"
        else:
            bid_id = f"BID-{next_num:04d}"
            next_num += 1
            target_row_idx = None
            placement = "append"
        # Resolve GC + contact info for THIS specific submission
        recipient = (sub or {}).get("to","").strip().lower()
        gc_name, contact_name, contact_phone = _gc_info_for_email(recipient) if recipient else ("","","")
        if not gc_name:
            gc_name = ab.get("gc", "")
        if not contact_name and (sub or {}).get("to_display"):
            contact_name = sub["to_display"]
        # Infer Facility Type from project name (snap to dropdown)
        pname_lower = ab.get("project_name", "").lower()
        facility_type = ""
        for kw, ft in [
            ("food lion","Grocery Store"), ("grocery","Grocery Store"),
            ("target","Retail / Big Box"), ("walmart","Retail / Big Box"),
            ("dollar","Retail / Big Box"), ("sally beauty","Retail / Big Box"),
            ("savers","Retail / Big Box"), ("victoria","Retail / Big Box"),
            ("hospital","Medical / Hospital"), ("medical","Medical / Hospital"),
            ("clinic","Medical / Hospital"), ("dental","Medical / Hospital"),
            ("vamc","Medical / Hospital"),
            ("hotel","Hotel"), ("suites","Hotel"), ("hyatt","Hotel"),
            ("hilton","Hotel"), ("marriott","Hotel"), ("hampton inn","Hotel"),
            ("school","School / Education"), ("university","School / Education"),
            ("carvana","Car Retail / Dealership"), ("adesa","Car Retail / Dealership"),
            ("dealership","Car Retail / Dealership"),
        ]:
            if kw in pname_lower:
                facility_type = _canon("Facility Type", ft) or ft
                break
        row = {h: "" for h in headers}
        row["Bid #"] = bid_id
        row["Project Name"] = ab.get("project_name", "")
        row["City"] = ab.get("city", "")
        row["State"] = ab.get("state", "")
        if facility_type: row["Facility Type"] = facility_type
        row["GC / Client"] = gc_name
        if contact_name:    row["Contact Name"] = contact_name
        if recipient:       row["Contact Email"] = recipient
        if contact_phone:   row["Contact Phone"] = contact_phone
        raw_bs = SOURCE_TO_CRM.get(ab.get("source",""), "Invitation (GC)")
        row["Bid Source"] = _canon("Bid Source", raw_bs) or raw_bs
        due = parse_date(ab.get("due_date", ""))
        if due:
            row["Bid Due Date"] = due.strftime("%m/%d/%Y")
        # ITB Received Date — when the invitation arrived. Required for the
        # auto-sort to bubble newly-added bids to the top of the active list.
        # Prefer email_date (RFC-2822); fall back to ingested_at (ISO);
        # last resort = today (the row was added now).
        itb_raw = ab.get("email_date") or ab.get("ingested_at") or ""
        itb_d = _parse_email_date(itb_raw) or parse_date(itb_raw)
        if not itb_d:
            itb_d = date.today()
        row["ITB Received Date"] = itb_d.strftime("%m/%d/%Y")
        raw_s = crm_status_for(ovr)
        s = _canon("Status", raw_s) or raw_s
        if s:
            row["Status"] = s
            # Mirror user convention: Bid Submitted/Awaiting Decision -> PENDING
            if s in ("Bid Submitted", "Awaiting Decision"):
                row["Win/Loss"] = "PENDING"
            elif s == "Won":
                row["Win/Loss"] = "WIN"
            elif s == "Lost":
                row["Win/Loss"] = "LOSS"
        # Per-submission date + amount
        sub_at = (sub or {}).get("at") or ovr.get("submitted_at","")
        if sub_at:
            sd = parse_date(sub_at) or _parse_email_date(sub_at)
            row["Bid Submitted Date"] = sd.strftime("%m/%d/%Y") if sd else sub_at[:10]
        sub_amt = (sub or {}).get("amount")
        if sub_amt:
            row["Bid Amount ($)"] = sub_amt
        if target_row_idx is not None:
            placeholder_fills.append((target_row_idx, row))
        else:
            new_rows.append(row)
        log(f"  ADD {bid_id} {ab.get('project_name','')[:35]:<35} -> {gc_name[:22]:<22} ({recipient[:30]}) | {placement}")

    # === Apply ===
    if not args.apply:
        summary = (f"crm_writeback {datetime.now().strftime('%H:%M:%S')}: "
                   f"would_update={update_count} "
                   f"would_fill_placeholders={len(placeholder_fills)} "
                   f"would_append={len(new_rows)} (DRY-RUN)")
        if args.quiet: print(summary)
        else: log(f"\n[crm-wb] {summary}\n[crm-wb] use --apply to write")
        return

    if update_count == 0 and not new_rows and not placeholder_fills:
        summary = f"crm_writeback {datetime.now().strftime('%H:%M:%S')}: no changes, CRM in sync"
        if args.quiet: print(summary)
        else: log(f"\n[crm-wb] {summary}")
        return

    # Apply batch updates first (status/date fills on already-existing rows)
    if updates:
        try:
            n = batch_update_rows("Bid Log", updates)
            log(f"[crm-wb] batch update: {n} cells written")
        except Exception as e:
            log(f"[crm-wb] batch update FAILED: {e}")

    # Fill placeholder rows in-place (write row data to specific row indices).
    # Skip the 'Bid #' column — those cells contain the user's auto-numbering
    # formula =BID-&TEXT(ROW()-1,"0000"). Overwriting with text would
    # break sort ordering and create apparent duplicates.
    filled = 0
    if placeholder_fills:
        ph_updates = []
        for row_idx, row_dict in placeholder_fills:
            for col_name, value in row_dict.items():
                if col_name == "Bid #":   # never overwrite the formula
                    continue
                if value and value != "":
                    ph_updates.append((row_idx, col_name, value))
        try:
            n = batch_update_rows("Bid Log", ph_updates)
            filled = len(placeholder_fills)
            log(f"[crm-wb] placeholder fills: {filled} rows ({n} cells written)")
        except Exception as e:
            log(f"[crm-wb] placeholder fill FAILED: {e}")

    # Append remaining new rows (only when placeholders exhausted).
    # For appended rows, write the Bid# formula instead of hardcoded text so
    # the row's Bid# auto-computes from row position (matches user convention).
    appended = 0
    if new_rows:
        BID_FORMULA = '="BID-"&TEXT(ROW()-1,"0000")'
        for r in new_rows:
            r["Bid #"] = BID_FORMULA
        try:
            appended = append_rows("Bid Log", new_rows)
            log(f"[crm-wb] batch append: {appended} rows added (with auto Bid# formula)")
        except Exception as e:
            log(f"[crm-wb] batch append FAILED: {e}")

    summary = (f"crm_writeback {datetime.now().strftime('%H:%M:%S')}: "
               f"updated={update_count} placeholders_filled={filled} appended={appended}")
    if args.quiet: print(summary)
    else: log(f"\n[crm-wb] {summary}")

    # Telegram notification
    total_changes = update_count + filled + appended
    if args.notify and total_changes >= 1:
        text = (f"CRM auto-updated\n"
                f"  - Status fields filled: {update_count}\n"
                f"  - Placeholder rows filled: {filled}\n"
                f"  - New bid rows appended: {appended}\n"
                f"  - Sheet: CRM-Bid-Log")
        telegram_notify(text)

    # Activity log — only when something actually changed
    if total_changes >= 1:
        try:
            from log_activity import log_activity
            parts = []
            if update_count: parts.append(f"{update_count} field update(s)")
            if filled:       parts.append(f"{filled} placeholder row(s) filled")
            if appended:     parts.append(f"{appended} row(s) appended")
            log_activity(
                "📊 CRM writes",
                f"crm_writeback applied: {', '.join(parts)} to Bid Log"
            )
        except Exception:
            pass


if __name__ == "__main__":
    main()
