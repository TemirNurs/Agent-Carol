#!/usr/bin/env python3
r"""
chase_silent_followups.py - Aggressive retry loop on non-responders.

User mandate: "we will fuck them until they reply — the more they ignore,
the harder we hit them"

ESCALATING cadence (hours since last FU we sent):
  Attempt 1 (initial)  → t+0      (done by send_followups_throttled.py)
  Attempt 2            → +72h     (3 days — polite check-in)
  Attempt 3            → +24h     (1 day — firmer)
  Attempt 4            → +12h     (twice-daily begins)
  Attempt 5            → +8h      (3x/day — last polite escalation)
  Attempt 6            → +6h      (4x/day — explicit "should I close it?")
  Attempt 7+           → +6h      (4x/day cap — escalating tone toward phone)

Safeguards:
  - Per-recipient daily cap (--max-per-recipient, default 3): if same email
    address already received N emails today, skip subsequent fires until tomorrow.
    Protects Gmail sender reputation from spam-filter triggers.
  - Stop on any reply (subject contains BID#) → that bid closes immediately.
  - Subject rotation prevents literal-duplicate spam-classifier triggers.

Stop conditions (unchanged):
  - GC has replied about this BID# (subject match)
  - Bid Status is Won/Lost/Withdrawn
  - Attempt count >= MAX_ATTEMPTS (default 12 — was 5; raised for ignore-pattern)
  - Notes contain [BOUNCE] or [NOT BIDDING]

State: data/memory/aggressive_chase_state.json
  { bid_id: {attempts: int, last_sent: "YYYY-MM-DDTHH:MM:SS", contact_email: "..."} }

Daemon runs this every ~6 hours; will only actually SEND if an interval has elapsed.

Usage:
  python scripts/chase_silent_followups.py                # dry-run
  python scripts/chase_silent_followups.py --apply        # actually send
  python scripts/chase_silent_followups.py --list         # show state only
  python scripts/chase_silent_followups.py --max-per-recipient 5 --apply
"""
from __future__ import annotations
import argparse
import imaplib
import email as email_lib
import json
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from email.header import decode_header
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "data" / "memory" / "aggressive_chase_state.json"
LOG_FILE = ROOT / "data" / "logs" / "chase_silent.log"
SEND_EMAIL = ROOT / "scripts" / "send_email.py"
LOCK_FILE = ROOT / "data" / "memory" / "chase_silent.lock"


def acquire_lock():
    """Single-process guarantee. Aborts if another instance is alive. Returns
    True if lock acquired, False if another instance owns it."""
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text(encoding="utf-8").strip())
            # Check if that PID is still alive
            import os as _os
            try:
                if _os.name == "nt":
                    import ctypes
                    PROCESS_QUERY = 0x0400
                    handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY, False, pid)
                    if handle:
                        ctypes.windll.kernel32.CloseHandle(handle)
                        return False  # still running
                else:
                    _os.kill(pid, 0)  # signal 0 = check existence
                    return False
            except (OSError, ProcessLookupError):
                pass  # stale lock — claim it
        except Exception:
            pass  # corrupt lock — claim it
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.write_text(str(os.getpid()), encoding="utf-8")
    return True


def release_lock():
    try:
        if LOCK_FILE.exists():
            LOCK_FILE.unlink()
    except Exception:
        pass

GMAIL_USER = os.environ.get("GMAIL_USER", "estimates@carolinacommercialfinishes.com")
GMAIL_PASS = os.environ.get("GMAIL_APP_PASSWORD", "")
CC_INTERNAL = "cs@carolinacommercialfinishes.com,wilsonsviatlana83@gmail.com,smayurov@gmail.com"

# Hours between attempts. Index = attempt # we're ABOUT to send.
# Escalating — each ignore tightens the loop. Attempts 7+ stay at 6h (4x/day cap).
INTERVAL_HOURS = {2: 72, 3: 24, 4: 12, 5: 8, 6: 6, 7: 6, 8: 6, 9: 6, 10: 6, 11: 6, 12: 6}
MAX_ATTEMPTS = 12
# Hard ceiling — never send more than this many emails/day to the SAME recipient
# address across ALL bids combined. Protects Gmail sender reputation.
DEFAULT_MAX_PER_RECIPIENT_PER_DAY = 3

INACTIVE_TAGS = ("[BOUNCE]", "[NOT BIDDING]", "[WITHDRAWN]")

SIGNATURE = """Best,
Nursultan Temirbaev | Manager
Carolina Commercial Finishes
c: (980) 348-1827
estimates@carolinacommercialfinishes.com | CarolinaCommercialFinishes.com
3308 Chancellor Lane | Monroe, NC 28110"""


def decode_h(s):
    if not s: return ""
    out = ""
    for p, e in decode_header(s):
        out += p.decode(e or "utf-8", errors="replace") if isinstance(p, bytes) else p
    return out


def load_state():
    if STATE.exists():
        try: return json.loads(STATE.read_text(encoding="utf-8"))
        except Exception: pass
    return {}


def save_state(st):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, indent=2), encoding="utf-8")


def log(msg, quiet=False):
    if not quiet: print(msg)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now().isoformat(timespec='seconds')}  {msg}\n")


def first_name(full):
    if not full: return "there"
    parts = full.strip().split()
    if not parts: return "there"
    first = parts[0].rstrip(",.").strip('"')
    if len(first) <= 2 and first.endswith("."):
        return parts[1].rstrip(",") if len(parts) > 1 else "there"
    return first or "there"


def first_email(s):
    if not s: return ""
    return next((p for p in re.split(r"[\s,;]+", s.strip()) if "@" in p), "")


def format_amount(s):
    if not s: return ""
    return f"USD {str(s).replace('$', '').strip()}"


def has_replied_since(project_name, contact_email, since_date, bid_id=None,
                      widen_days=3):
    """Did the GC contact reply about THIS project since since_date?
    Matches by project-name keywords + sender email (the contact's domain).
    NOT by Bid# in subject — Bid# is a row-number formula and shifts every
    time the user re-sorts the sheet, so reply subjects preserve the OLD
    Bid# and a current-Bid# search would miss them.

    Logic:
      1. Extract distinctive keywords from project_name (numbers + key words).
      2. Gmail search SINCE date FROM <contact_domain>.
      3. For each hit, verify subject contains at least one keyword.
      4. Skip outbound (carolinacommercial), delivery failures, BC noise.
    """
    if not contact_email or not project_name:
        return False
    contact_email = contact_email.split(",")[0].strip().lower()
    if "@" not in contact_email:
        return False
    domain = contact_email.split("@", 1)[1]
    # Extract distinctive keywords: 4-digit numbers (e.g. store #), proper nouns
    proj_lower = project_name.lower()
    nums = re.findall(r"\d{3,5}", project_name)
    # Pick distinctive long words (skip generic ones)
    stop = {"food", "lion", "store", "the", "and", "inc", "llc", "corp", "co",
            "construction", "company", "project", "building", "tenant", "center",
            "remodel", "renovation", "renovations", "upfit", "phase", "for"}
    words = [w.lower() for w in re.findall(r"[A-Za-z]{4,}", project_name)
             if w.lower() not in stop]
    keywords = nums + words[:3]  # most-distinctive only
    if not keywords:
        keywords = [w.lower() for w in re.findall(r"[A-Za-z]{4,}", project_name)[:2]]
    try:
        M = imaplib.IMAP4_SSL("imap.gmail.com")
        M.login(GMAIL_USER, GMAIL_PASS)
        M.select("INBOX")
        # Widen search window by 3 days — enough to catch a reply that came a
        # day or two before our previous fire (the Debbie case), but NOT so
        # wide it reaches back into the GC's own pre-submission invitation /
        # addendum emails and falsely suppresses a fresh bid's first follow-up
        # (the Midtown East $307K case: 7-day window caught a pre-submission
        # LF Jennings email and wrongly marked it "replied").
        from datetime import timedelta as _td
        widened = since_date - _td(days=max(0, widen_days))
        since_str = widened.strftime("%d-%b-%Y")
        # Search all inbound from this contact's domain since the cutoff
        st, ids = M.search(None, f'(SINCE "{since_str}" FROM "{domain}")')
        replied = False
        if st == "OK" and ids[0]:
            for mid in ids[0].split():
                st, data = M.fetch(mid, '(BODY.PEEK[HEADER])')
                if st != "OK": continue
                msg = email_lib.message_from_bytes(data[0][1])
                fr = decode_h(msg.get("From", "")).lower()
                if any(x in fr for x in ("carolinacommercial", "mailer-daemon",
                                          "noreply", "no-reply",
                                          "team@buildingconnected", "notifications@")):
                    continue
                subj = decode_h(msg.get("Subject", "")).lower()
                # Match: at least one keyword OR the legacy BID# in subject
                if any(k.lower() in subj for k in keywords):
                    replied = True
                    break
                if bid_id and bid_id.lower() in subj:
                    replied = True
                    break
        M.logout()
        return replied
    except Exception:
        return False


def build_message(bid_row, attempt_num, days_since_initial):
    """Compose an escalating-tone follow-up based on attempt number."""
    proj = bid_row.get("Project Name", "")
    bid_id = bid_row.get("Bid #", "")
    name = first_name(bid_row.get("Contact Name", ""))
    amount = format_amount(bid_row.get("Bid Amount ($)", ""))
    sub_date_str = bid_row.get("Bid Submitted Date", "")

    subject = subject_variant(bid_id, proj, attempt_num)

    if attempt_num == 2:
        # +3 days — soft "just confirming receipt"
        body = (
            f"Hi {name},\n\n"
            f"Wanted to make sure my follow-up email from earlier this week reached you on "
            f"{proj} ({bid_id}). Our proposal of {amount} was submitted {sub_date_str}.\n\n"
            f"Quick check-in — any read on the project timeline, or anything you need from us on our end?\n\n"
            f"{SIGNATURE}\n"
        )
    elif attempt_num == 3:
        # +1 day after attempt 2 — straightforward "any update"
        body = (
            f"Hi {name},\n\n"
            f"Circling back on {proj} ({bid_id}, {amount}, submitted {sub_date_str}). "
            f"Wanted to follow up since I haven't heard back yet.\n\n"
            f"Could you share a quick update on where things stand? Even a one-line "
            f"\"still pending\" or \"timeline pushed\" helps us prioritize.\n\n"
            f"{SIGNATURE}\n"
        )
    elif attempt_num == 4:
        # +12h — firmer
        body = (
            f"Hi {name},\n\n"
            f"Following up again on {proj} ({bid_id}, {amount}). I want to make sure our "
            f"number is still in front of you and the project is moving.\n\n"
            f"If the scope has changed, the project is on hold, or you've gone with another sub, "
            f"a quick note keeps our records clean. Otherwise I'll continue to assume we're in the running.\n\n"
            f"Happy to revise pricing, jump on a call, or send updated COIs / references if helpful.\n\n"
            f"{SIGNATURE}\n"
        )
    elif attempt_num == 5:
        # +8h — third email today, asks for definitive answer
        body = (
            f"Hi {name},\n\n"
            f"Reaching out once more on {proj} ({bid_id}, {amount}, submitted {sub_date_str}). "
            f"Still no response on our end.\n\n"
            f"Could you let me know:\n"
            f"  • Is the project still active?\n"
            f"  • Has it been awarded? (To us or someone else?)\n"
            f"  • Should I close it out on our end?\n\n"
            f"A one-line reply is plenty. Otherwise I'll plan to call your office tomorrow.\n\n"
            f"{SIGNATURE}\n"
        )
    elif attempt_num == 6:
        # +6h — explicit "calling soon"
        body = (
            f"Hi {name},\n\n"
            f"I want to respect your inbox, but I've sent several emails on {proj} "
            f"({bid_id}, {amount}) without a response. Before I close this out on our side "
            f"or move to a phone call, can you give me the 5-second status?\n\n"
            f"  Active / Awarded / On Hold / Lost — that's all I need.\n\n"
            f"{SIGNATURE}\n"
        )
    elif attempt_num in (7, 8):
        # +6h each — firm but professional
        body = (
            f"Hi {name},\n\n"
            f"This is attempt #{attempt_num} on {proj} ({bid_id}, {amount}). I'd genuinely "
            f"rather get a \"no\" than no answer at all — our pipeline planning needs accurate data.\n\n"
            f"If our bid is no longer in consideration, please let me know and I'll close it "
            f"out cleanly. If it IS still alive, a one-line update helps us hold pricing/crew.\n\n"
            f"I'll be calling your office shortly if this doesn't land.\n\n"
            f"{SIGNATURE}\n"
        )
    else:
        # Attempts 9-12 — final phase, professional but no longer soft
        body = (
            f"Hi {name},\n\n"
            f"I've reached out repeatedly on {proj} ({bid_id}, {amount}) and haven't been "
            f"able to get a response. I want to make sure my emails aren't going to your "
            f"spam folder.\n\n"
            f"If you'd prefer I stop emailing and call instead — say the word. If you'd "
            f"prefer I close out our bid and stop following up — also say the word. Either "
            f"way, a one-line reply ends this loop.\n\n"
            f"Otherwise I'll switch to phone outreach by end of day.\n\n"
            f"{SIGNATURE}\n"
        )
    return subject, body


def parse_date_safe(s):
    if not s: return None
    for fmt in ("%a, %d %b %Y", "%m/%d/%Y", "%Y-%m-%d"):
        try: return datetime.strptime(str(s).strip()[:30], fmt).date()
        except Exception: pass
    return None


def parse_dt_safe(s):
    """Parse either ISO datetime or date — returns datetime for hour-precision math."""
    if not s: return None
    s = str(s).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
                "%a, %d %b %Y", "%m/%d/%Y"):
        try: return datetime.strptime(s[:len(fmt)+5 if 'T' in fmt else 30], fmt)
        except Exception: pass
    # Fallback: date-only string
    d = parse_date_safe(s)
    return datetime.combine(d, datetime.min.time()) if d else None


def count_today_sends_to(recipient, state):
    """How many emails have we ALREADY sent to this recipient today across all bids?"""
    if not recipient: return 0
    today = date.today()
    recipient = recipient.lower().strip()
    n = 0
    for bid, info in state.items():
        if (info.get("contact_email") or "").lower().strip() != recipient:
            continue
        last = parse_dt_safe(info.get("last_sent", ""))
        if last and last.date() == today:
            n += info.get("sends_today", 1)
    return n


def subject_variant(bid_id, proj, attempt_num):
    """Rotate subject prefixes by attempt to avoid literal-duplicate spam triggers."""
    variants = {
        2: f"Follow-Up: {proj} ({bid_id})",
        3: f"Re: Follow-Up — {proj} ({bid_id})",
        4: f"Quick check: {proj} ({bid_id})",
        5: f"Status update on {proj} ({bid_id})?",
        6: f"Closing out {proj} ({bid_id}) — need a one-line confirm",
        7: f"{proj} ({bid_id}) — calling tomorrow if no reply",
        8: f"Final email check: {proj} ({bid_id})",
        9: f"{bid_id} {proj} — moving to phone outreach",
        10: f"Last email on {proj} ({bid_id}) before close-out",
        11: f"{bid_id} — closing this out today unless I hear from you",
        12: f"{bid_id} — please confirm status to keep bid open",
    }
    return variants.get(attempt_num, variants[12])


def main():
    # ACQUIRE LOCK FIRST — fail loudly if another instance is alive
    if not acquire_lock():
        try:
            pid = LOCK_FILE.read_text(encoding="utf-8").strip()
        except Exception:
            pid = "?"
        msg = f"[chase] ABORT — another instance is running (PID {pid}). Lock file: {LOCK_FILE}"
        print(msg)
        log(msg, quiet=True)
        sys.exit(2)
    import atexit
    atexit.register(release_lock)

    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--list", action="store_true", help="Show queue state only")
    ap.add_argument("--seed", action="store_true",
                    help="Seed state from yesterday's 29-bid batch (one-time)")
    ap.add_argument("--interval", type=int, default=1500,
                    help="Seconds between sends (default 1500 = 25 min). Matches "
                         "yesterday's pacing — never dump everything at once.")
    ap.add_argument("--force", action="store_true",
                    help="Ignore the hour-based cadence — fire next attempt NOW "
                         "for every active bid in state.")
    ap.add_argument("--max-per-recipient", type=int,
                    default=DEFAULT_MAX_PER_RECIPIENT_PER_DAY,
                    help=f"Daily cap per recipient email address "
                         f"(default {DEFAULT_MAX_PER_RECIPIENT_PER_DAY}). Set higher "
                         f"to allow 4-5x/day but risk Gmail spam-filter triggers.")
    args = ap.parse_args()

    state = load_state()
    today = date.today()

    # One-time seed: pre-populate state with yesterday's 29 sends so the chaser
    # knows where to start.
    if args.seed:
        sent_yesterday = [
            'BID-0004','BID-0005','BID-0006','BID-0008','BID-0009','BID-0011','BID-0013',
            'BID-0014','BID-0015','BID-0021','BID-0022','BID-0023','BID-0024','BID-0025',
            'BID-0026','BID-0027','BID-0028','BID-0030','BID-0035','BID-0037','BID-0038',
            'BID-0039','BID-0040','BID-0043','BID-0047','BID-0051','BID-0057','BID-0068',
            'BID-0070',
        ]
        for bid in sent_yesterday:
            if bid not in state:
                state[bid] = {"attempts": 1, "last_sent": "2026-05-11", "status": "active"}
        save_state(state)
        print(f"Seeded state with {len(sent_yesterday)} bids from 5/11 batch.")

    from crm_lib import get_sheet, batch_update_rows
    sh = get_sheet("Bid Log")
    hdrs = sh.row_values(1)
    rows = sh.get_all_values()
    # STABLE LOOKUP — key CRM rows by Internal ID (UUID, never changes when
    # the user sorts the sheet). Bid# is a row-number formula that shifts on
    # every sort and is unreliable as a primary key.
    iid_to_data = {}
    has_iid_col = "Internal ID" in hdrs
    for r_idx, r in enumerate(rows[1:], start=2):
        d = {h: (r[i] if i < len(r) else "") for i, h in enumerate(hdrs)}
        d["_row_idx"] = r_idx
        iid = (d.get("Internal ID") or "").strip() if has_iid_col else ""
        if iid:
            iid_to_data[iid] = d

    actions = []   # list of (display_label, action_type, msg)
    for key, info in list(state.items()):
        if info.get("status") in ("done", "stale", "skip"):
            continue
        # State is keyed by Internal ID (UUID). Look up CRM row by it.
        d = iid_to_data.get(key)
        # display_label = current Bid# (for log readability) — but stable_key is `key`
        bid_id = (d.get("Bid #") if d else info.get("legacy_bid_id", "")).strip() or key[:8]
        if not d:
            actions.append((bid_id, "skip", f"Internal ID {key[:8]} not in CRM (deleted row?)"))
            continue
        notes = (d.get("Notes") or "").upper()
        if any(tag in notes for tag in INACTIVE_TAGS):
            info["status"] = "done"
            actions.append((bid_id, "stop", "inactive flag in Notes"))
            continue
        status = (d.get("Status") or "").strip()
        if status in ("Won", "Lost", "Withdrawn", "No Decision"):
            info["status"] = "done"
            actions.append((bid_id, "stop", f"Status={status}"))
            continue
        last_sent = parse_dt_safe(info.get("last_sent", ""))
        if not last_sent:
            actions.append((bid_id, "skip", "no last_sent date"))
            continue
        # AUTHORITATIVE: always re-read Contact Email and Project Name from CRM.
        crm_email = first_email(d.get("Contact Email", ""))
        if not crm_email:
            actions.append((bid_id, "skip", "no CRM contact email"))
            continue
        contact_email = crm_email
        project_name = (d.get("Project Name") or "").strip()
        if info.get("contact_email") and info["contact_email"].lower() != crm_email.lower():
            log(f"[chase] WARN {bid_id} email shifted: state={info['contact_email']} -> crm={crm_email}")
            info["contact_email"] = crm_email
        # Project-keyword + contact-domain reply detection (Bid#-shift safe)
        if has_replied_since(project_name, contact_email, last_sent.date(), bid_id=bid_id):
            info["status"] = "done"
            info["closed_at"] = datetime.now().isoformat(timespec="seconds")
            info["closed_reason"] = f"reply from {contact_email} about {project_name[:40]}"
            actions.append((bid_id, "stop", f"reply received about {project_name[:40]}"))
            continue
        attempts = info.get("attempts", 1)
        if attempts >= MAX_ATTEMPTS:
            info["status"] = "stale"
            actions.append((bid_id, "stop", f"hit max attempts ({MAX_ATTEMPTS}) — escalate to phone"))
            continue
        # Next attempt number = attempts + 1
        next_attempt = attempts + 1
        interval_h = INTERVAL_HOURS.get(next_attempt, 6)  # cap at 6h
        elapsed_h = (datetime.now() - last_sent).total_seconds() / 3600
        if not args.force and elapsed_h < interval_h:
            hrs_to_wait = interval_h - elapsed_h
            if hrs_to_wait >= 24:
                wait_str = f"{hrs_to_wait/24:.1f}d"
            else:
                wait_str = f"{hrs_to_wait:.1f}h"
            actions.append((bid_id, "wait", f"attempt {next_attempt} in {wait_str}"))
            continue
        # Per-recipient daily cap — protect sender reputation
        rcpt = contact_email  # already from CRM (authoritative)
        sent_to_rcpt_today = count_today_sends_to(rcpt, state)
        if sent_to_rcpt_today >= args.max_per_recipient:
            actions.append((bid_id, "wait",
                f"rcpt {rcpt} hit daily cap ({sent_to_rcpt_today}/"
                f"{args.max_per_recipient}) — retry tomorrow"))
            continue
        # FIRE!
        sub_date = parse_date_safe(d.get("Bid Submitted Date", ""))
        days_since_initial = (today - sub_date).days if sub_date else 0
        subj, body = build_message(d, next_attempt, days_since_initial)
        actions.append((bid_id, "fire", f"attempt #{next_attempt} → {rcpt} "
                                       f"(rcpt today: {sent_to_rcpt_today+1}/"
                                       f"{args.max_per_recipient})"))
        if args.apply:
            r = subprocess.run(
                [sys.executable, str(SEND_EMAIL),
                 "--to", rcpt,
                 "--cc", CC_INTERNAL,
                 "--subject", subj,
                 "--body", body,
                 "--no-signature"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=60,
            )
            if '"status": "sent"' in (r.stdout or ""):
                info["attempts"] = next_attempt
                info["last_sent"] = datetime.now().isoformat(timespec="seconds")
                info["contact_email"] = rcpt
                info["legacy_bid_id"] = bid_id  # for human-readable logs
                info["project_snapshot"] = project_name
                log(f"[chase] FIRED {bid_id} attempt#{next_attempt} to {rcpt}")
                # Persist state KEYED BY INTERNAL ID (UUID), not Bid#
                state[key] = info
                save_state(state)
                # Write to CRM Activity Log sheet so the team can see what we sent
                try:
                    from activity_log import log_event
                    log_event(
                        internal_id=key,
                        bid_id=bid_id,
                        project=project_name,
                        type="follow_up",
                        direction="outbound",
                        counterparty=rcpt,
                        channel="email",
                        summary=f"Chase attempt #{next_attempt} — {subj[:120]}",
                        reference="",
                    )
                except Exception as e:
                    log(f"[chase] (Activity Log write failed: {e})", quiet=True)
            else:
                log(f"[chase] FAIL {bid_id} attempt#{next_attempt}: {r.stdout[:200]}")
            time.sleep(args.interval)
        # Update local state in-memory (keyed by Internal ID)
        state[key] = info

    if not args.list:
        save_state(state)

    # Report
    print(f"=== Aggressive chase state (today {today}) ===")
    by_action = {}
    for bid, action, msg in actions:
        by_action.setdefault(action, []).append((bid, msg))
    for action in ("fire", "wait", "stop", "skip"):
        items = by_action.get(action, [])
        if items:
            print(f"\n[{action.upper()}] {len(items)}:")
            for bid, msg in items:
                print(f"  {bid:<10} {msg}")
    if not args.apply:
        fires = len(by_action.get("fire", []))
        if fires:
            print(f"\n[chase] dry-run. {fires} would fire. Re-run with --apply to send.")


if __name__ == "__main__":
    main()
