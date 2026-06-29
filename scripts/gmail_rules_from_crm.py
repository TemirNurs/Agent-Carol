#!/usr/bin/env python3
"""
Generate Gmail labeling rules from the CRM workbook.

Reads CRM-Bid-Log.xlsx (Bid Log + Completed Projects + GC Directory) and produces
data/memory/gmail_rules.json — a self-maintaining rule set that gmail_organize.py
consumes. When the CRM updates (Won/Lost/On Hold), this script regenerates the
rules so Gmail labels follow CRM truth automatically.

Usage:
  python scripts/gmail_rules_from_crm.py                   # generate rules.json
  python scripts/gmail_rules_from_crm.py --dry-run         # preview
  python scripts/gmail_rules_from_crm.py --quiet           # daemon mode
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = Path(__file__).resolve().parent.parent
RULES_FILE   = BASE / "data" / "memory" / "gmail_rules.json"
DEFAULT_CRM  = Path.home() / "Downloads" / "CRM-Bid-Log (3).xlsx"

# Map CRM Status → Gmail label
STATUS_LABEL = {
    "Lost":              "Lost",
    "Won":               "Won",
    "Awarded":           "Won",
    "Bid Submitted":     "Follow-ups",
    "Awaiting Decision": "Follow-ups",
    "Pending Review":    "To respond",
    "Estimating":        "To respond",
    "ITB Received":      "Bid Invites",
    "Reviewing":         "Bid Invites",
    "On Hold":           "On Hold",
    "Hold":              "On Hold",
    "Withdrawn":         "On Hold",
    "No Bid":            "On Hold",
    "Declined":          "On Hold",
}

# Words too generic to use as a search token. Includes NC/SC/VA/GA city names
# and structural words — using "Greensboro" or "Buildings" as a match token
# made the labeler tag every unrelated email about any project in that city
# (the "labels what it wants" bug). Real distinctiveness = store numbers +
# proper nouns + (critically) GC-domain scoping added in build_query.
GENERIC = {
    "the","and","for","with","new","old","building","buildings","construction",
    "painting","wallcovering","wall","interior","exterior","store","center",
    "plaza","renovation","renovations","remodel","upfit","tenant","build",
    "out","project","phase","complete","scope","bid","proposal","carolina",
    "commercial","finishes","ccf","north","south","east","west","park","mall",
    "system","systems","inc","llc","corp","group","company","update","reply",
    "fwd","invitation","general","contractor","contractors","facility",
    "facilities","grandstands","concept","foods","health","dental","hospital",
    "addition","additions","install","installation","accessories","partitions",
    "greensboro","charlotte","raleigh","durham","winston","salem","mebane",
    "concord","fayetteville","wilmington","asheboro","randleman","chester",
    "chesterfield","quinton","monroe","matthews","huntersville","cornelius",
    "kannapolis","gastonia","salisbury","hickory","hudson","sanford","clayton",
    "angier","carthage","lincolnton","denham","springs","atlanta","kennesaw",
    "jacksonville","herriman","riviera","beach","dekalb","carowinds","midtown",
    "columbia","charleston","ooltewah","bowling","green","pittsburgh","pittsburg",
}


def read_crm(path=None):
    """Read CRM from Google Sheets (path ignored, kept for compat)."""
    sys.path.insert(0, str(BASE / "scripts"))
    from crm_lib import all_records
    bid_log = [r for r in all_records("Bid Log")
               if r.get("Bid #") and r.get("Project Name")]
    try:
        completed = [r for r in all_records("Completed Projects")
                     if r.get("Project Name")]
    except Exception:
        completed = []
    try:
        gcs = [r for r in all_records("GC Directory")
               if r.get("GC / Company")]
    except Exception:
        gcs = []
    return bid_log, completed, gcs


def distinctive_tokens(name):
    """Return tokens from project name useful for Gmail search.
    Prefers store numbers and proper-noun-like tokens; drops generic words.
    """
    if not name: return []
    tokens = []
    # Store/project numbers (3+ digits)
    nums = re.findall(r"#\s*(\d{3,6})|\b(\d{4,6})\b", name)
    for grp in nums:
        for n in grp:
            if n and n not in tokens:
                tokens.append(n)
    # Words 5+ chars that aren't generic
    words = re.findall(r"[A-Za-z][A-Za-z']{4,}", name)
    for w in words:
        wl = w.lower()
        if wl not in GENERIC and wl not in tokens:
            tokens.append(w)
    return tokens[:3]  # cap at 3 tokens per project


def build_query(name):
    """Build a Gmail X-GM-RAW query for one bid by name."""
    toks = distinctive_tokens(name)
    if not toks:
        return None
    parts = []
    for t in toks:
        if t.isdigit():
            parts.append(f"subject:{t}")
        else:
            # Multi-word phrase or single distinctive word
            parts.append(f'subject:"{t}"' if " " in t else f"subject:{t}")
    # OR them together
    return " OR ".join(parts)


def gc_domain(email):
    """Extract a usable domain from a contact email."""
    if not email: return None
    m = re.search(r"@([a-z0-9.-]+\.[a-z]{2,})", email.lower())
    if not m: return None
    dom = m.group(1)
    # Skip generic providers
    if dom in {"gmail.com","yahoo.com","outlook.com","hotmail.com","aol.com",
               "isqftmail.com","constructconnectmail.com","smartbidnet.com",
               "buildingconnected.com","procoretech.com"}:
        return None
    return dom


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=str(DEFAULT_CRM))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    def log(*a, **k):
        if not args.quiet: print(*a, **k)

    crm_path = Path(args.file)
    if not crm_path.exists():
        print(f"CRM not found: {crm_path}"); sys.exit(1)

    bid_log, completed, gcs = read_crm(crm_path)
    log(f"[rules] CRM has {len(bid_log)} Bid Log + {len(completed)} Completed + {len(gcs)} GCs")

    rules = []  # list of {label, query, desc, source}
    skipped = 0

    # ---- Per-bid rules from Bid Log ----
    # IMPORTANT: per-bid Follow-ups rules must EXCLUDE the proposal-attached
    # message itself (goes to "Proposals Sent") AND all invitation/addendum/
    # RFI/reminder messages — those are NOT follow-ups. Follow-ups means
    # CORRESPONDENCE ABOUT A BID WE ALREADY SUBMITTED — our outbound chase
    # emails + GC replies to them. Invitations and notifications from plan
    # rooms / GC estimating systems must be excluded even when the project
    # name keyword matches.
    FOLLOWUP_EXCLUSION = (
        ' -(has:attachment subject:proposal -subject:"follow-up" -subject:re)'
        ' -subject:"Invitation to Bid" -subject:"Invitation to bid"'
        ' -subject:"Bid Invite:" -subject:"Bid Invite "'
        ' -subject:"Bid invitation"'
        ' -subject:"Additional Bid Doc" -subject:"Additional Documents"'
        ' -subject:"RFI Response" -subject:"RFI Responses" -subject:"RFI responses"'
        ' -subject:"Bid Reminder" -subject:"Reminder To Bid"'
        ' -subject:"Reminder to bid" -subject:"Bid Due"'
        ' -subject:"Last Chance"'
        ' -subject:"Reminder to submit" -subject:"Project Update"'
        ' -subject:"Addendum" -subject:"Bid Documents"'
        ' -subject:"You have been invited" -subject:"has invited you to bid"'
        ' -subject:"New Project"'
        ' -from:DoNotReply@constructconnectmail.com'
        ' -from:Transmittals@isqftmail.com'
        ' -from:notifications@us02.procoretech.com'
        ' -from:notifications@com2.smartbidnet.com'
        ' -from:team@buildingconnected.com'
        ' -from:notifications@buildingconnected.com'
    )

    for b in bid_log:
        status = (b.get("Status") or "").strip()
        if not status:
            skipped += 1
            continue
        label = STATUS_LABEL.get(status)
        if not label:
            skipped += 1
            continue
        name = b.get("Project Name", "")
        query = build_query(name)
        if not query:
            skipped += 1
            continue
        bid_id = b.get("Bid #", "?")
        # SCOPE BY GC DOMAIN — the single most important precision fix. Without
        # this, a token like "Rural" matched ANY email; with it, the rule only
        # fires on mail actually to/from the GC we bid this project to.
        dom = gc_domain(b.get("Contact Email", ""))
        if dom:
            query = f"({query}) (from:{dom} OR to:{dom} OR cc:{dom})"
        else:
            # No usable GC domain: require the token AND a CCF bid context so a
            # bare city/word can't sweep unrelated mail.
            query = (f"({query}) (from:carolinacommercialfinishes.com "
                     f"OR to:carolinacommercialfinishes.com)")
        # For Follow-ups, exclude the proposal-send message
        # Follow-ups = OUR chase threads ONLY. Require the Follow-Up subject
        # marker so an inbound ITB/addendum about the same project can never
        # wear the label (6/11: MUSC "BID SUBMISSION" inbound was labeled
        # Follow-ups because the project's CRM status was Bid Submitted).
        if label == "Follow-ups":
            query = f'({query}) subject:"Follow-Up"{FOLLOWUP_EXCLUSION}'
        rules.append({
            "label": label,
            "query": query,
            "desc": f"{bid_id} {name[:60]}",
            "source": f"Bid Log:{bid_id} status={status}",
            "priority": 10 if label == "Lost" else 20,  # Lost first
        })

    # ---- Won rules from "Completed Projects": DISABLED (6/11 incident) ----
    # CORRECTED 6/12 (user): that tab IS CCF's real won/completed history
    # (CCF's completed-projects history — synced to data/memory/completed_projects.json,
    # used for pricing anchors / Similar Won Work in estimates). The disable
    # stands for a DIFFERENT reason: unscoped name rules built from it
    # (e.g. `subject:<project keyword>`) stamped "Won" on live chase threads
    # for NEW projects with similar names. Historical wins are DATA, never
    # label triggers. The ONLY Won label source is the Bid Log Status column
    # (GC-domain-scoped).
    if completed:
        log(f"[rules] ignoring {len(completed)} 'Completed Projects' rows for "
            f"label rules (real CCF won history — analytics only, "
            f"never name-match labeling)")

    # ---- "Known GC" catch-all from GC Directory ----
    gc_email_rules = []
    for g in gcs:
        domain = gc_domain(g.get("Email", ""))
        if not domain: continue
        gc_email_rules.append((g.get("GC / Company", "?"), domain))
    # Dedup
    seen_domains = set()
    for name, dom in gc_email_rules:
        if dom in seen_domains: continue
        seen_domains.add(dom)
        rules.append({
            "label": "Known GC",
            "query": f"from:{dom}",
            "desc": f"GC Directory: {name}",
            "source": "GC Directory",
            "priority": 90,  # apply LAST so it doesn't overwrite specific bid labels
        })

    # ---- Hardcoded catch-alls (auto-source labels) ----
    # Proposals Sent — STRICT: outbound from CCF + has attachment + initial proposal
    # subject (not "Follow-Up:" / "RE:" / "FW:"). Applied before per-bid Follow-ups
    # so follow-up rules can exclude these.
    PROPOSAL_RULE = (
        "(from:estimates@carolinacommercialfinishes.com OR from:cs@carolinacommercialfinishes.com) "
        "has:attachment "
        "(subject:proposal OR subject:bid OR subject:quote OR subject:estimate) "
        '-subject:"follow-up" -subject:followup -subject:"FU:" '
        "-subject:RE -subject:FW -subject:Re -subject:Fw -subject:fwd"
    )
    # Outbound follow-up from CCF (we sent a follow-up note)
    OUR_FOLLOWUP_RULE = (
        "(from:estimates@carolinacommercialfinishes.com OR from:cs@carolinacommercialfinishes.com) "
        '(subject:"follow-up" OR subject:followup OR subject:"FU:")'
    )

    # Internal team handles sourced from CCF_OWN_DOMAINS so no personal addresses
    # ship as code literals. Internal label is ONLY for the personal team handles,
    # NOT for CCF-domain traffic — so drop the company-domain substring.
    _internal_handles = [
        h.strip() for h in os.environ.get(
            "CCF_OWN_DOMAINS", "carolinacommercialfinishes").split(",")
        if h.strip() and "carolinacommercialfinishes" not in h
    ]

    hardcoded = [
        ("Bid Invites", "from:DoNotReply@constructconnectmail.com", "ConstructConnect notifications"),
        ("Bid Invites", "from:Transmittals@isqftmail.com",         "iSqFt"),
        ("Bid Invites", "from:notifications@us02.procoretech.com", "Procore"),
        ("Bid Invites", "from:notifications@com2.smartbidnet.com", "SmartBid"),
        ("Bid Invites", "from:team@buildingconnected.com OR from:notifications@buildingconnected.com", "BuildingConnected"),
        ("Proposals Sent",  PROPOSAL_RULE,    "STRICT: outbound CCF + attachment + proposal subject (not RE/follow-up)"),
        ("Follow-ups",      OUR_FOLLOWUP_RULE, "STRICT: outbound CCF follow-up (no attachment required)"),
        # User rule (5/27): Internal label is ONLY for the internal team.
        # NOT for CCF Daily Bid Reports, NOT for any CCF↔CCF traffic.
        *[("Internal", f"from:{h}", "internal team") for h in _internal_handles],
    ]
    for label, query, desc in hardcoded:
        rules.append({
            "label": label, "query": query, "desc": desc,
            "source": "hardcoded",
            "priority": 70,
        })

    # ---- Generic GC-reply catch-all (lowest priority) ----
    rules.append({
        "label": "Follow-ups",
        "query": "(subject:RE OR subject:Fwd) (subject:painting OR subject:proposal OR subject:bid) -label:Lost -label:Won -from:noreply -from:donotreply",
        "desc": "Generic GC-reply on bid threads",
        "source": "catchall",
        "priority": 80,
    })

    # ---- (REMOVED 5/27) Broader Internal catch-all was over-tagging
    # 1500+ emails as Internal. User rule: Internal is ONLY for the internal
    # team. Anything else (vendor confirmations, CCF-domain inbound,
    # daily reports) is NOT Internal.

    # ---- (REMOVED 5/27) Vendors/Financial/Personal/Travel/Marketing/Notifications ----
    # User explicitly limited the label set (2026-05-27) to ONLY these 10:
    #   Bid Invites, Follow-ups, Internal, Known GC, Lost, On Hold,
    #   Pending Review, Proposals Sent, To respond, Won
    # Any rule that produces a label outside that set is silently refused
    # by gmail_organize.py::apply_label (ALLOWED_LABELS check), so the old
    # Vendors/Financial/Personal/Travel/Marketing/Notifications rules were
    # never actually applying — they just polluted the rules file. Removed.

    # Sort by priority (lower = applied first)
    rules.sort(key=lambda r: r["priority"])

    # Stats
    by_label = {}
    for r in rules:
        by_label[r["label"]] = by_label.get(r["label"], 0) + 1
    log(f"\n[rules] Generated {len(rules)} rules")
    for L, n in sorted(by_label.items(), key=lambda x: -x[1]):
        log(f"  {L:<18} {n}")
    log(f"\n[rules] skipped {skipped} CRM rows (no status / unparseable name)")

    if args.dry_run:
        log("\n[dry-run] not writing")
        return

    RULES_FILE.parent.mkdir(parents=True, exist_ok=True)
    out = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_file": str(crm_path),
        "rule_count": len(rules),
        "rules": rules,
    }
    RULES_FILE.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

    summary = f"gmail_rules_from_crm {datetime.now().strftime('%H:%M:%S')}: rules={len(rules)} labels={len(by_label)} written"
    if args.quiet:
        print(summary)
    else:
        print(f"\n[rules] saved → {RULES_FILE.name}")


if __name__ == "__main__":
    main()
