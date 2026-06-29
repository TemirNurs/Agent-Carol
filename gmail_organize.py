#!/usr/bin/env python3
"""
Organize Gmail inbox by applying labels using Gmail IMAP X-GM-LABELS extension.

Source of truth: the CRM bid log (a point-in-time snapshot of lost + active bids).
Order matters: LOST first, then FOLLOW-UPS (excludes already-Lost), then the rest.

Stdlib only. Two-phase: Phase 1 counts; Phase 2 applies.
"""

import imaplib
import os
import re
import sys
import time
from pathlib import Path

# Force UTF-8 stdout on Windows
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Auto-load .env so GMAIL_APP_PASSWORD is available when this is invoked
# as a daemon subprocess (5/26 bug: the daemon's env wasn't propagating).
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except Exception:
    pass

EMAIL = os.environ.get("GMAIL_USER", "estimates@carolinacommercialfinishes.com")
APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
HOST = "imap.gmail.com"
ALL_MAIL = '"[Gmail]/All Mail"'

# (label, x-gm-raw query, description)
# NOTE: This is a stale --no-crm FALLBACK only; at runtime the script prefers
# CRM-generated rules. Left empty in the public source so it ships no project
# roster; an entry looks like: ("Lost", 'subject:1234', "BID-0001 (lost bid)").
LOST_RULES = []

FOLLOWUPS_RULES_RAW = [
    # All follow-up rules now REQUIRE 'Follow-Up' in the subject. This catches:
    #   - our outbound:  "Follow-Up: Project Name (BID-NNNN)"
    #   - GC replies:    "RE: Follow-Up: Project Name (BID-NNNN)"
    # but NOT new invitations or unrelated threads about the same project.
    # We keep per-project narrowing so labels stay project-scoped.
    ("Follow-ups", 'subject:"Follow-Up"', "All follow-up correspondence (universal rule)"),
]
# Append " -label:Lost" so we don't re-label projects already in Lost
FOLLOWUPS_RULES = [(lbl, q + " -label:Lost", desc) for lbl, q, desc in FOLLOWUPS_RULES_RAW]

PROPOSALS_RULES = [
    ("Proposals Sent",
     '(from:estimates@carolinacommercialfinishes.com OR from:cs@carolinacommercialfinishes.com) '
     '(subject:proposal OR subject:bid OR subject:painting OR subject:wallcovering)',
     "outbound CCF proposals/bids"),
]

BID_INVITES_RULES = [
    ("Bid Invites", 'from:DoNotReply@constructconnectmail.com', "ConstructConnect"),
    ("Bid Invites", 'from:Transmittals@isqftmail.com', "iSqFt Transmittals"),
    ("Bid Invites", 'from:notifications@us02.procoretech.com', "Procore"),
    ("Bid Invites", 'from:notifications@com2.smartbidnet.com', "SmartBid"),
    ("Bid Invites",
     'from:team@buildingconnected.com OR from:notifications@buildingconnected.com',
     "BuildingConnected"),
    ("Bid Invites",
     'from:Estimating@lfjennings.com (subject:"Bid Reminder" OR subject:"Invitation to Bid" '
     'OR subject:Notice OR subject:LFJ)',
     "LFJ Estimating"),
]

# Internal team handles sourced from CCF_OWN_DOMAINS so no personal addresses
# ship as code literals. Internal label is ONLY for the personal team handles,
# NOT for CCF-domain traffic — so drop the company-domain substring.
_OWN_DOMAINS = [
    h.strip() for h in os.environ.get("CCF_OWN_DOMAINS", "carolinacommercialfinishes").split(",")
    if h.strip()
]
_INTERNAL_HANDLES = [h for h in _OWN_DOMAINS if "carolinacommercialfinishes" not in h]

INTERNAL_RULES = [
    # Per user (May 5, 2026): Internal label is ONLY for emails from the internal team.
    # Everything else (CCF daily reports, self-emails, proposal logs) should NOT be Internal.
    ("Internal", f'from:{h}', "internal team") for h in _INTERNAL_HANDLES
]


# ---------- helpers ----------

def search(M, query):
    """Run an X-GM-RAW search, return list of UIDs (strings).

    Gmail X-GM-RAW expects the query as a single IMAP-quoted string. imaplib
    won't quote spaces for us, so we wrap the query in double quotes and
    escape any internal double quotes with backslash.
    """
    # Escape internal quotes, wrap whole query in double-quotes so IMAP sees one arg.
    escaped = query.replace("\\", "\\\\").replace('"', '\\"')
    quoted = f'"{escaped}"'
    try:
        status, data = M.uid("SEARCH", "X-GM-RAW", quoted)
    except Exception as e:
        print(f"  [search err] {e}")
        return []
    if status != "OK" or not data or not data[0]:
        return []
    return data[0].split()


def chunked(lst, n=100):
    for i in range(0, len(lst), n):
        yield lst[i : i + n]


ALLOWED_LABELS = {
    "Bid Invites", "Follow-ups", "Internal", "Known GC", "Lost",
    "On Hold", "Pending Review", "Proposals Sent", "To respond", "Won",
}


def apply_label(M, uids, label):
    """Apply a Gmail label via X-GM-LABELS in chunks of 100.

    Refuses to apply any label not in the approved set (per user, May 5 2026).
    This prevents the script from accidentally creating new labels Carol or
    a future rule edit might invent.
    """
    if label not in ALLOWED_LABELS:
        print(f"  [REFUSED] label '{label}' not in approved set — skipping")
        return 0
    if not uids:
        return 0
    applied = 0
    label_arg = f'("{label}")'
    for chunk in chunked(uids, 100):
        ids = b",".join(chunk).decode() if isinstance(chunk[0], bytes) else ",".join(chunk)
        try:
            status, _ = M.uid("STORE", ids, "+X-GM-LABELS", label_arg)
            if status == "OK":
                applied += len(chunk)
        except Exception as e:
            print(f"  [store err] {e}")
    return applied


def remove_label(M, uids, label):
    """Remove a Gmail label via X-GM-LABELS - in chunks of 100."""
    if not uids:
        return 0
    removed = 0
    label_arg = f'("{label}")'
    for chunk in chunked(uids, 100):
        ids = b",".join(chunk).decode() if isinstance(chunk[0], bytes) else ",".join(chunk)
        try:
            status, _ = M.uid("STORE", ids, "-X-GM-LABELS", label_arg)
            if status == "OK":
                removed += len(chunk)
        except Exception as e:
            print(f"  [unstore err] {e}")
    return removed


def ensure_label(M, label):
    """Create label if it doesn't exist. Idempotent."""
    try:
        status, data = M.list()
    except Exception:
        return
    if status != "OK":
        return
    existing = []
    for line in data:
        if line is None:
            continue
        s = line.decode(errors="replace") if isinstance(line, bytes) else str(line)
        existing.append(s)
    if any(f'"{label}"' in line for line in existing):
        return
    try:
        M.create(f'"{label}"')
        print(f"  [create] '{label}'")
    except Exception as e:
        print(f"  [create-err] {e}")


# ---------- main ----------

def load_crm_rules():
    """Load CRM-generated rules. Returns list[(label, query, desc)] or None if missing."""
    import json
    from pathlib import Path
    rules_file = Path(__file__).resolve().parent / "data" / "memory" / "gmail_rules.json"
    if not rules_file.exists():
        return None
    try:
        data = json.load(open(rules_file, encoding="utf-8"))
        rules = data.get("rules", [])
        # Sort by priority then return triples
        rules.sort(key=lambda r: r.get("priority", 50))
        # Group by label-priority for better Phase logging
        return [(r["label"], r["query"], r.get("desc", r.get("source", "?")))
                for r in rules]
    except Exception as e:
        print(f"[rules-load err] {e}")
        return None


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true", help="Daemon mode: only print one summary line")
    ap.add_argument("--no-crm", action="store_true", help="Use hardcoded rules only, skip CRM-generated")
    cli = ap.parse_args()
    QUIET = cli.quiet

    # Redirect noisy phase 1/2 output when quiet, restore for final summary
    if QUIET:
        import os
        _real_stdout = sys.stdout
        sys.stdout = open(os.devnull, "w", encoding="utf-8")

    print(f"Connecting to {HOST}...")
    M = imaplib.IMAP4_SSL(HOST)
    try:
        M.login(EMAIL, APP_PASSWORD)
    except imaplib.IMAP4.error as e:
        print(f"LOGIN FAILED: {e}")
        sys.exit(1)
    print(f"Logged in as {EMAIL}")

    # Ensure all WHITELISTED labels exist (only the 10 the user approved).
    # Removed 5/27: Vendors, Financial, Personal, Travel, Marketing,
    # Notifications — `ensure_label` was recreating them every 65 min after
    # I deleted them. Keep this list IDENTICAL to ALLOWED_LABELS so the
    # ensure step never creates an unapproved label.
    for lbl in sorted(ALLOWED_LABELS):
        ensure_label(M, lbl)

    # Select All Mail (writable for STORE)
    status, _ = M.select(ALL_MAIL)
    if status != "OK":
        # Some clients see it under a localized name; try fallback
        status, _ = M.select('"[Gmail]/All Mail"')
    if status != "OK":
        print("Cannot select [Gmail]/All Mail")
        sys.exit(1)

    # ---------- PHASE 0: CLEANUP MISLABELED ----------
    print("\n" + "=" * 70)
    print("PHASE 0 — CLEANUP MISLABELED MESSAGES")
    print("=" * 70)
    cleanup_rules = [
        # === Remove "Proposals Sent" wrongly applied ===
        ('Proposals Sent',
         'label:"Proposals Sent" -has:attachment',
         'no attachment -> not a real proposal send'),
        ('Proposals Sent',
         'label:"Proposals Sent" (subject:"follow-up" OR subject:followup OR subject:"FU:")',
         'subject says follow-up'),
        ('Proposals Sent',
         'label:"Proposals Sent" (subject:RE OR subject:Re OR subject:FW OR subject:Fw OR subject:fwd)',
         'reply/forward, not original proposal send'),
        ('Proposals Sent',
         'label:"Proposals Sent" -from:estimates@carolinacommercialfinishes.com -from:cs@carolinacommercialfinishes.com',
         'inbound mail, not outbound from CCF'),

        # === Remove "Follow-ups" wrongly applied ===
        # Aggressive: ANY message with both labels — remove Follow-ups (Proposals Sent is the specific one)
        ('Follow-ups',
         'label:"Follow-ups" label:"Proposals Sent"',
         'cross-labeled with Proposals Sent -> keep only Proposals Sent'),
        # Outbound CCF + attachment + "proposal" subject + NOT a follow-up = real proposal send
        ('Follow-ups',
         '(from:estimates@carolinacommercialfinishes.com OR from:cs@carolinacommercialfinishes.com) '
         'has:attachment subject:proposal -subject:"follow-up" -subject:RE -subject:FW '
         'label:"Follow-ups"',
         'real proposal send -> Follow-ups label was wrong'),
        # Outbound CCF + attachment + initial bid subject (not RE/follow-up) — even broader catch
        ('Follow-ups',
         'label:"Follow-ups" has:attachment from:carolinacommercialfinishes.com '
         '-subject:"follow-up" -subject:followup -subject:RE -subject:FW',
         'CCF outbound w/ attachment, no follow-up keyword -> not a follow-up'),
    ]
    cleanup_summary = {}
    for label, query, why in cleanup_rules:
        uids = search(M, query)
        if uids:
            n = remove_label(M, uids, label)
            cleanup_summary.setdefault(label, 0)
            cleanup_summary[label] += n
            print(f"  REMOVE {label}: {n} msgs ({why})")
    if not cleanup_summary:
        print("  (nothing to clean up)")

    # ---------- PHASE 1: COUNT PREVIEW ----------
    print("\n" + "=" * 70)
    print("PHASE 1 — COUNT PREVIEW (no labels applied yet)")
    print("=" * 70)

    # Prefer CRM-generated rules; fall back to hardcoded if --no-crm or missing
    crm_rules = None if cli.no_crm else load_crm_rules()
    if crm_rules:
        # Group by label for cleaner phase logs
        from collections import OrderedDict
        groups = OrderedDict()
        for label, query, desc in crm_rules:
            groups.setdefault(label, []).append((label, query, desc))
        rule_groups = [(L.upper(), rules) for L, rules in groups.items()]
        print(f"[rules] using {len(crm_rules)} CRM-generated rules across {len(groups)} labels")
    else:
        rule_groups = [
            ("LOST", LOST_RULES),
            ("FOLLOW-UPS", FOLLOWUPS_RULES),
            ("PROPOSALS SENT", PROPOSALS_RULES),
            ("BID INVITES", BID_INVITES_RULES),
            ("INTERNAL", INTERNAL_RULES),
        ]
        print(f"[rules] using hardcoded rules (no CRM rules file found)")
    preview = {}
    for group_name, rules in rule_groups:
        print(f"\n[{group_name}]")
        for label, query, desc in rules:
            uids = search(M, query)
            preview.setdefault(label, set()).update(uids)
            print(f"  {len(uids):>5}  {desc[:50]:<50}  query: {query[:50]}")
    print()
    for label in ["Lost", "Follow-ups", "Proposals Sent", "Bid Invites", "Internal"]:
        n = len(preview.get(label, set()))
        print(f"  PREVIEW total unique UIDs for '{label}': {n}")

    # ---------- PHASE 2: APPLY LABELS ----------
    print("\n" + "=" * 70)
    print("PHASE 2 — APPLYING LABELS")
    print("=" * 70)

    applied_summary = {}
    for group_name, rules in rule_groups:
        for label, query, desc in rules:
            uids = search(M, query)
            if not uids:
                continue
            n = apply_label(M, uids, label)
            applied_summary.setdefault(label, 0)
            applied_summary[label] += n
            print(f"  {label}: applied to {n} msgs ({desc[:40]})")
            time.sleep(0.05)  # gentle on the server

    # ---------- PHASE 3: POST-APPLY CLEANUP ----------
    # Per-bid Follow-ups rules can re-apply Follow-ups to messages that already
    # got Proposals Sent. Run one more pass to enforce exclusivity.
    print("\n" + "=" * 70)
    print("PHASE 3 — POST-APPLY CLEANUP (enforce label exclusivity)")
    print("=" * 70)
    final_cleanup = [
        ('Follow-ups', 'label:"Follow-ups" label:"Proposals Sent"',
         'still cross-labeled with Proposals Sent -> remove Follow-ups'),
        ('Follow-ups', 'label:"Follow-ups" label:"Lost"',
         'final outcome Lost -> remove Follow-ups'),
        ('Follow-ups', 'label:"Follow-ups" label:"Won"',
         'final outcome Won -> remove Follow-ups'),
        # --- Invitation messages MUST NOT be Follow-ups ---
        # Follow-ups = correspondence about a bid we ALREADY SUBMITTED.
        # Invitations, addenda, RFI notices, and platform reminders are NOT
        # follow-ups even if they share a project/GC keyword with a submitted
        # bid. Cleanup these patterns aggressively after labeling.
        ('Follow-ups', 'label:"Follow-ups" label:"Bid Invites"',
         'also Bid Invites -> never a Follow-up'),
        ('Follow-ups', 'label:"Follow-ups" subject:"Invitation to Bid"',
         'subject says Invitation to Bid -> not a Follow-up'),
        ('Follow-ups', 'label:"Follow-ups" subject:"Invitation to bid"',
         'subject says Invitation to bid -> not a Follow-up'),
        ('Follow-ups', 'label:"Follow-ups" subject:"Bid Invite:"',
         'subject "Bid Invite:" -> invitation, not Follow-up'),
        ('Follow-ups', 'label:"Follow-ups" subject:"Bid Invite "',
         'subject starts "Bid Invite " -> invitation'),
        ('Follow-ups', 'label:"Follow-ups" subject:"Bid invitation"',
         'subject says Bid invitation -> not a Follow-up'),
        ('Follow-ups', 'label:"Follow-ups" subject:"Additional Bid Doc"',
         'subject says Additional Bid Doc -> invitation addendum'),
        ('Follow-ups', 'label:"Follow-ups" subject:"Additional Documents"',
         'addenda from plan rooms are not Follow-ups'),
        ('Follow-ups', 'label:"Follow-ups" subject:"RFI Response"',
         'RFI response notifications are not Follow-ups'),
        ('Follow-ups', 'label:"Follow-ups" subject:"RFI Responses"',
         'RFI responses (plural) -> not a Follow-up'),
        ('Follow-ups', 'label:"Follow-ups" subject:"RFI responses"',
         'lowercase RFI responses -> not a Follow-up'),
        ('Follow-ups', 'label:"Follow-ups" subject:"You have been invited"',
         'platform "You have been invited" -> invitation'),
        ('Follow-ups', 'label:"Follow-ups" subject:"has invited you to bid"',
         '"has invited you to bid" -> invitation'),
        ('Follow-ups', 'label:"Follow-ups" subject:"New Project"',
         'New Project notification -> invitation'),
        ('Follow-ups', 'label:"Follow-ups" subject:"Bid Reminder"',
         'Bid Reminder from platform -> not a Follow-up'),
        ('Follow-ups', 'label:"Follow-ups" subject:"Reminder to submit"',
         'platform reminder to submit a bid -> not a Follow-up'),
        ('Follow-ups', 'label:"Follow-ups" subject:"Reminder To Bid"',
         '"Reminder To Bid" from platform -> not a Follow-up'),
        ('Follow-ups', 'label:"Follow-ups" subject:"Reminder to bid"',
         'lowercase "Reminder to bid" -> not a Follow-up'),
        ('Follow-ups', 'label:"Follow-ups" subject:"Bid Due"',
         '"Bid Due" reminder -> not a Follow-up'),
        ('Follow-ups', 'label:"Follow-ups" subject:"Last Chance"',
         '"Last Chance to Bid" -> invitation reminder'),
        ('Follow-ups', 'label:"Follow-ups" subject:"Project Update"',
         'project update from plan room -> not a Follow-up'),
        ('Follow-ups', 'label:"Follow-ups" subject:"Addendum"',
         'addenda are not Follow-ups'),
        ('Follow-ups', 'label:"Follow-ups" subject:"Bid Documents"',
         'Bid Documents notifications are not Follow-ups'),
        # Plan-room platforms NEVER send Follow-ups — they send invitations
        # and reminders. Any Follow-ups label on these is wrong.
        ('Follow-ups',
         'label:"Follow-ups" (from:DoNotReply@constructconnectmail.com OR '
         'from:Transmittals@isqftmail.com OR '
         'from:notifications@us02.procoretech.com OR '
         'from:notifications@com2.smartbidnet.com OR '
         'from:team@buildingconnected.com OR '
         'from:notifications@buildingconnected.com)',
         'platform-sent invitation -> never a Follow-up'),
    ]
    # User rule (5/27): "Internal" label is ONLY for the internal team.
    # Any other sender labeled Internal is wrong — strip it. Only run this
    # cleanup when we actually know the team handles (else it would strip
    # every Internal message).
    if _INTERNAL_HANDLES:
        final_cleanup.append((
            'Internal',
            'label:"Internal" ' + ' '.join(f'-from:{h}' for h in _INTERNAL_HANDLES),
            'not from the internal team -> not Internal'))
    for label, query, why in final_cleanup:
        uids = search(M, query)
        if uids:
            n = remove_label(M, uids, label)
            print(f"  REMOVE {label}: {n} msgs ({why})")
            cleanup_summary.setdefault(label, 0)
            cleanup_summary[label] += n

    # ---------- PHASE 4: STALE TERMINAL-LABEL RECONCILIATION ----------
    # The core fix for "Midtown East stuck as Lost". gmail_organize was
    # ADD-only: once "Lost"/"Won" stuck, it never came off even after the CRM
    # changed. Now: an email may only keep "Lost"/"Won" if a CURRENT CRM rule
    # for that label still matches it. Compute the set difference and strip the
    # stale ones. Safe because per-bid rules are now GC-domain-scoped (precise).
    print("\n" + "=" * 70)
    print("PHASE 4 — STALE TERMINAL-LABEL RECONCILIATION")
    print("=" * 70)
    if crm_rules:
        for term_label in ("Lost", "Won"):
            should = set()
            for lbl, q, desc in crm_rules:
                if lbl != term_label:
                    continue
                for u in search(M, q):
                    should.add(u if isinstance(u, str) else u.decode())
            currently = set(
                (u if isinstance(u, str) else u.decode())
                for u in search(M, f'label:"{term_label}"')
            )
            stale = currently - should
            if stale:
                n = remove_label(M, [s.encode() for s in stale], term_label)
                print(f"  REMOVE {term_label}: {n} msgs no longer match any "
                      f"CRM '{term_label}' rule (status changed)")
                cleanup_summary.setdefault(term_label, 0)
                cleanup_summary[term_label] += n
            else:
                print(f"  {term_label}: 0 stale — all match current CRM")
    else:
        print("  (skipped — no CRM rules loaded; refusing to reconcile blind)")

    # ---------- SUMMARY ----------
    print("\n" + "=" * 70)
    print("FINAL SUMMARY")
    print("=" * 70)
    total = 0
    for label, n in applied_summary.items():
        print(f"  {label:<20}  {n:>6} STORE ops")
        total += n
    print(f"  {'-'*30}")
    print(f"  {'TOTAL':<20}  {total:>6} STORE ops")
    print(f"\n  Note: STORE ops include re-applying existing labels (idempotent — Gmail dedups).")

    # Restore stdout for one-line summary in quiet mode
    if QUIET:
        sys.stdout.close()
        sys.stdout = _real_stdout
        from datetime import datetime
        summary = (
            f"gmail_organize {datetime.now().strftime('%H:%M:%S')}: "
            f"Lost={applied_summary.get('Lost',0)} "
            f"Follow-ups={applied_summary.get('Follow-ups',0)} "
            f"Proposals={applied_summary.get('Proposals Sent',0)} "
            f"BidInvites={applied_summary.get('Bid Invites',0)} "
            f"Internal={applied_summary.get('Internal',0)} "
            f"total={total}"
        )
        print(summary)

    try:
        M.logout()
    except Exception:
        pass


if __name__ == "__main__":
    main()
