#!/usr/bin/env python3
"""Lessons audit — verify Carol's code complies with the rules in
AGENTS_LESSONS.md. Catches regressions WHEN they're introduced, not when
the user notices and complains.

Each rule from AGENTS_LESSONS.md gets a check here. CI/daemon run this
nightly. Output is "PASS" or "FAIL: <rule>: <what's wrong>" — fail = ping.

Usage:
  python scripts/_lessons_audit.py             # human-readable
  python scripts/_lessons_audit.py --quiet     # exit 1 on any failure
"""
from __future__ import annotations
import argparse, json, os, re, sys
from pathlib import Path

try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

ROOT = Path(r"C:/Agent Carol")
SCRIPTS = ROOT / "scripts"
# Files in _legacy/ are deprecated and excluded from audit (they're kept for
# historical reference but no longer run from heartbeat or imported anywhere).
LEGACY = SCRIPTS / "_legacy"

failures = []
passes = []

def check(name, ok, detail=""):
    if ok:
        passes.append(name)
    else:
        failures.append((name, detail))


# Rule 1: No script uses Bid# as a primary lookup key (only Internal ID)
# Skip scripts where Bid# is just displayed/logged, not used as a dict key.
LOOKUP_AS_BID = re.compile(r"""\b(by_bid_id|bid_to_row|bid_id_to_data|"Bid #":\s*[a-z]\w*\s*=)""", re.I)
for sf in [p for p in SCRIPTS.rglob("*.py") if LEGACY not in p.parents]:
    if sf.name.startswith("_"): continue
    try:
        txt = sf.read_text(encoding="utf-8")
    except Exception: continue
    if LOOKUP_AS_BID.search(txt):
        check(f"R1 bid#-as-key in {sf.name}", False,
              f"file appears to use Bid# as a dict key — should be Internal ID")


# Rule 2: Every IMAP fetch uses BODY.PEEK[ (not BODY[ or RFC822)
PEEK_VIOL = re.compile(r"\.fetch\(\s*[^,]+,\s*['\"]\(RFC822\)|\.fetch\(\s*[^,]+,\s*['\"]\(BODY\[", re.I)
for sf in [p for p in SCRIPTS.rglob("*.py") if LEGACY not in p.parents]:
    if sf.name.startswith("_patch_"): continue
    try:
        txt = sf.read_text(encoding="utf-8")
    except Exception: continue
    if PEEK_VIOL.search(txt):
        check(f"R2 non-PEEK fetch in {sf.name}", False,
              "uses RFC822 or BODY[ — must use BODY.PEEK[]")


# Rule 3: chase / followup scripts MUST have a has_replied_recently or
# has_replied_since check
CHASE_FILES = ["chase_silent_followups.py", "chase_consolidated.py",
               "send_followups_throttled.py", "_chase_today.py",
               "silent_followups.py", "followup_batch.py"]
for fname in CHASE_FILES:
    p = SCRIPTS / fname
    if not p.exists(): continue
    txt = p.read_text(encoding="utf-8")
    if "has_replied" not in txt:
        check(f"R3 reply-awareness in {fname}", False,
              f"chase script must call has_replied_recently or has_replied_since")


# Rule 4: External-send scripts MUST CC the internal recipients (accounting + owner).
# A file passes if:
#   (a) it contains the literal CC local-parts, OR
#   (b) it sources the internal CC list from env / company config, OR
#   (c) it imports CC_INTERNAL / CC from another file that contains them
SEND_PATTERNS = ["chase_silent_followups", "chase_consolidated", "send_followups_throttled",
                 "_chase_today", "silent_followups", "followup_batch"]
# Local-parts of the internal CC recipients, sourced from the same env var the
# chase scripts read (OWNER_ALIAS_EMAILS) — no personal addresses hardcoded here.
EXPECTED_CC = [a.split("@", 1)[0] for a in os.environ.get("OWNER_ALIAS_EMAILS", "").split(",") if a.strip()]
# Tokens proving the file wires the internal CC list from env / company config.
CC_ENV_TOKENS = ("CCF_INTERNAL_CC", "internal_cc_str", "internal_cc_list", "OWNER_ALIAS_EMAILS")

def _file_or_imported_has_cc(target_file: Path, _seen=None) -> bool:
    if _seen is None: _seen = set()
    if target_file in _seen: return False
    _seen.add(target_file)
    txt = target_file.read_text(encoding="utf-8")
    if EXPECTED_CC and all(cc in txt for cc in EXPECTED_CC):
        return True
    if any(tok in txt for tok in CC_ENV_TOKENS):
        return True
    # Match BOTH single-line and parenthesized multi-line imports.
    # Pattern: `from <module> import (<anything including newlines>)` OR
    #          `from <module> import <names on one line>`
    for m in re.finditer(
        r"from\s+([\w.]+)\s+import\s+(?:\(([^)]*)\)|([^\n]+))",
        txt, re.DOTALL,
    ):
        mod = m.group(1).split(".")[-1]
        imported = (m.group(2) or m.group(3) or "")
        if "CC_INTERNAL" not in imported:
            continue
        candidate = SCRIPTS / f"{mod}.py"
        if candidate.exists() and candidate != target_file:
            if _file_or_imported_has_cc(candidate, _seen):
                return True
    return False

for fname in [f + ".py" for f in SEND_PATTERNS]:
    p = SCRIPTS / fname
    if not p.exists(): continue
    if not _file_or_imported_has_cc(p):
        check(f"R4 CC list in {fname}", False,
              "missing the internal CC recipients — every chase must CC them "
              "(or import CC_INTERNAL / source the list from env / company config)")


# Rule 5: crm_writeback never adds rows for un-submitted bids
# Verify the "ITB Received" auto-creation path is NOT enabled.
wb = SCRIPTS / "crm_writeback.py"
if wb.exists():
    txt = wb.read_text(encoding="utf-8")
    if '_invitation_only' in txt and 'row["Status"] = s' in txt:
        # If the invitation-only-status assignment is active...
        if 'if not s and ovr.get("_invitation_only"):' in txt:
            if 's = "ITB Received"' in txt:
                check("R5 CRM = submitted only", False,
                      "crm_writeback would auto-add ITB Received rows — user says CRM = submitted only")


# Rule 6: Project_core regex allows letter suffix (Food Lion 2118B)
# Look for the older bare \b\d{3,5}\b pattern
PC_BUGGY = re.compile(r'\\b\\d\{3,5\}\\b')
PC_GOOD = re.compile(r"\(\\d\{3,5\}\)\[a-z\]\?")
if wb.exists():
    txt = wb.read_text(encoding="utf-8")
    if PC_BUGGY.search(txt):
        check("R6 project_core regex", False,
              "uses old \\b\\d{3,5}\\b — must allow [a-z]? suffix for 2118B-style")
    elif not PC_GOOD.search(txt):
        check("R6 project_core regex", False, "expected [a-z]? optional suffix not found")


# Rule 7: gmail_organize.py removes Follow-ups from Bid Invites
go = ROOT / "gmail_organize.py"
if go.exists():
    txt = go.read_text(encoding="utf-8")
    if 'label:"Follow-ups" label:"Bid Invites"' not in txt:
        check("R7 Follow-ups + Bid Invites cleanup", False,
              "missing cross-label cleanup")


# Rule 8: process_followup_replies filters UNCLEAR/OUT_OF_OFFICE from Telegram
fr = SCRIPTS / "process_followup_replies.py"
if fr.exists():
    txt = fr.read_text(encoding="utf-8")
    # We expect a filter that skips UNCLEAR / OUT_OF_OFFICE from the Telegram body
    if 'actionable_lines' not in txt or '"UNCLEAR", "OUT_OF_OFFICE"' not in txt:
        check("R8 Telegram noise filter", False,
              "UNCLEAR/OUT_OF_OFFICE not filtered from Telegram pings")


# Rule 9: loss_postmortem dedups by Internal ID (sidecar.internal_id)
lp = SCRIPTS / "loss_postmortem.py"
if lp.exists():
    txt = lp.read_text(encoding="utf-8")
    if 'already_iid' not in txt or '"internal_id"' not in txt:
        check("R9 loss_postmortem dedup by Internal ID", False,
              "still using Bid#/filename dedup — must use internal_id")


# Rule 10: project_core must produce same key for all name formats.
# Live test the function from crm_writeback.
try:
    sys.path.insert(0, str(SCRIPTS))
    from crm_writeback import _project_core as _pc
    variants = [
        ("Food Lion #2235 Quinton, VA", "2235 Food Lion Quinton, VA",
         "Food Lion 2235 Quinton VA"),
        ("Food Lion #1602 Chesterfield, VA", "1602 Food Lion Chesterfield, VA",
         "Food Lion 1602 Chesterfield"),
    ]
    for vset in variants:
        cores = {_pc(v) for v in vset}
        if len(cores) > 1:
            check("R10 project_core format-agnostic", False,
                  f"variants produce DIFFERENT keys: {dict(zip(vset, [_pc(v) for v in vset]))}")
            break
except Exception as e:
    check("R10 project_core importable", False, f"crm_writeback._project_core import failed: {e}")


# Rule 11: orphan synthesis must check CRM existing rows AND use project_core
wb = SCRIPTS / "crm_writeback.py"
if wb.exists():
    txt = wb.read_text(encoding="utf-8")
    if "_existing_cores" not in txt or "_project_core(_pn)" not in txt:
        check("R11 orphan synthesis checks CRM by project_core", False,
              "still uses slug-only dedup — must check CRM existing by _project_core")


# Report
print(f"\n=== Carol Lessons Audit ===")
print(f"PASS: {len(passes)} rule(s)")
print(f"FAIL: {len(failures)} rule(s)")
print()
if failures:
    for name, detail in failures:
        print(f"  ❌ {name}")
        print(f"     {detail}")
    print(f"\n→ See AGENTS_LESSONS.md for rule context.")
    sys.exit(1)
else:
    print("All lessons rules satisfied ✓")
    sys.exit(0)
