#!/usr/bin/env python3
r"""
chase_consolidated.py — Send ONE follow-up email per contact, listing
ALL their pending bids in a single message.

The original chase_silent_followups.py sends per-bid emails. That meant if
Contact A at GC-1 has 2 pending bids, they got 2 separate FU emails 25 min
apart. If Contact B at GC-2 has 4 pending bids, they got 4 separate emails. That
looks scattered and annoys the GC.

This consolidator:
  1. Reads aggressive_chase_state.json
  2. Looks up each active (status=active) bid's current CRM row by Internal ID
  3. Groups by (contact_email)
  4. For each group:
       - Builds ONE consolidated email body listing every bid
       - Subject: "Status check on N CCF proposals (project1, project2, ...)"
       - Body: per-bid bullet with project + $ + submitted-date + attempt#
       - Sends via send_email.py
       - Updates state for ALL bids in group (attempts++, last_sent=now)
       - Writes ONE entry to Activity Log
  5. Respects:
       - Per-recipient daily cap (default 3 — but consolidation means 1 send/day)
       - File lock at data/memory/chase_silent.lock (shared with chase_silent_followups)
       - has_replied_since check (per-bid, project keywords + contact domain)

Usage:
  python scripts/chase_consolidated.py            # dry-run
  python scripts/chase_consolidated.py --apply    # send
  python scripts/chase_consolidated.py --interval 600 --apply  # 10-min spacing
"""
from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Reuse helpers from chase_silent_followups
from chase_silent_followups import (
    STATE, LOG_FILE, SEND_EMAIL, GMAIL_USER, GMAIL_PASS, CC_INTERNAL,
    INTERVAL_HOURS, MAX_ATTEMPTS, DEFAULT_MAX_PER_RECIPIENT_PER_DAY,
    INACTIVE_TAGS, SIGNATURE,
    load_state, save_state, log,
    first_name, first_email, format_amount,
    has_replied_since, parse_date_safe, parse_dt_safe,
    acquire_lock, release_lock, LOCK_FILE,
)

# 5/30 fix — load .env so GMAIL_APP_PASSWORD / API keys are present when
# Carol (OpenClaw/Telegram) shells out to this script. A shelled child does
# NOT inherit the daemon's env, so without this the credential reads below
# return '' and the script fails (e.g. IMAP login). Absolute path → cwd-safe.
try:
    from pathlib import Path as _CCF_P
    from dotenv import load_dotenv as _ccf_load_dotenv
    _ccf_load_dotenv(_CCF_P(__file__).resolve().parent.parent / ".env")
except Exception:
    pass


def build_consolidated_message(bids_for_contact, recipient_name, recipient_email):
    """Compose ONE email referencing all of `bids_for_contact`.
    Each item in bids_for_contact: dict with project, bid_id, amount, sub_date_str, attempt_num.
    Returns (subject, body)."""
    name = first_name(recipient_name) if recipient_name else "there"

    # Determine overall tone — most aggressive attempt # in the group sets it
    max_attempt = max(b.get("attempt_num", 2) for b in bids_for_contact)
    n_bids = len(bids_for_contact)

    # Build short project list for subject
    if n_bids == 1:
        b = bids_for_contact[0]
        subject = f"Follow-Up: {b['project']} ({b['bid_id']})"
    elif n_bids == 2:
        subject = (f"Status check — {bids_for_contact[0]['project']} + "
                   f"{bids_for_contact[1]['project']}")
    else:
        subject = (f"Status check on {n_bids} CCF proposals "
                   f"({bids_for_contact[0]['project'][:30]} + {n_bids-1} more)")

    # Build body
    lines = [f"Hi {name},", ""]
    if max_attempt <= 3:
        lines.append(
            f"Following up on {'the' if n_bids == 1 else f'our {n_bids}'} CCF "
            f"painting proposal{'s' if n_bids > 1 else ''} I sent over recently. "
            f"Could you share a quick status update on {'this' if n_bids == 1 else 'each'} below?"
        )
    elif max_attempt <= 5:
        lines.append(
            f"I'd like to circle back on {'the' if n_bids == 1 else 'these'} CCF "
            f"painting bid{'s' if n_bids > 1 else ''} — a few have gone "
            f"{'a while' if n_bids == 1 else 'several days'} without an update. "
            f"Even a one-line status (still pending / awarded / no decision yet) helps us prioritize crew & pricing."
        )
    else:
        lines.append(
            f"I want to respect your inbox, but I've reached out multiple times on "
            f"{'this proposal' if n_bids == 1 else f'these {n_bids} proposals'} "
            f"without hearing back. Before I close {'it' if n_bids == 1 else 'them'} "
            f"out or escalate to a phone call, could you give me the 5-second "
            f"{'status' if n_bids == 1 else 'status on each'}? Active / Awarded / "
            f"On Hold / Lost — that's all I need."
        )
    lines.append("")

    # Bullet list of bids
    for b in sorted(bids_for_contact, key=lambda x: -x.get("amount_num", 0)):
        proj = b["project"]
        bid_id = b["bid_id"]
        amt = b.get("amount") or "(no $ on file)"
        sub_date_str = b.get("sub_date_str", "recently")
        lines.append(f"  • {proj} ({bid_id})")
        lines.append(f"      Our proposal: {amt}  —  Submitted: {sub_date_str}")
    lines.append("")

    if max_attempt >= 6:
        lines.append("I'll plan to call your office tomorrow if I don't hear back.")
        lines.append("")
    elif max_attempt >= 4:
        lines.append("Happy to revise pricing, jump on a call, or send updated COIs / references if helpful.")
        lines.append("")

    lines.append(SIGNATURE)
    body = "\n".join(lines)
    return subject, body


def main():
    # GLOBAL CHASE-BATCH LOCK — refuses to start if ANY chase pipeline is alive.
    _scripts_dir = str(Path(__file__).resolve().parent)
    if _scripts_dir not in sys.path:
        sys.path.insert(0, _scripts_dir)
    try:
        from _lib.chase_batch_lock import acquire as _global_acquire, release as _global_release
        if not _global_acquire(__file__):
            sys.exit(2)
        import atexit as _atx; _atx.register(_global_release)
    except ImportError as _e:
        print(f"WARN: global chase lock unavailable ({_e}) — refusing to run",
              file=sys.stderr)
        sys.exit(3)

    if not acquire_lock():
        try:
            pid = LOCK_FILE.read_text(encoding="utf-8").strip()
        except Exception:
            pid = "?"
        print(f"[chase-consolidated] ABORT — another chase instance is running (PID {pid}).")
        sys.exit(2)
    import atexit
    atexit.register(release_lock)

    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--interval", type=int, default=1500,
                    help="Seconds between recipients (default 1500 = 25 min)")
    ap.add_argument("--force", action="store_true",
                    help="Ignore cadence — fire next attempt NOW for all active bids")
    ap.add_argument("--max-per-recipient", type=int,
                    default=DEFAULT_MAX_PER_RECIPIENT_PER_DAY)
    args = ap.parse_args()

    from crm_lib import get_sheet
    sh = get_sheet("Bid Log")
    hdrs = sh.row_values(1)
    rows = sh.get_all_values()
    iid_to_data = {}
    for r_idx, r in enumerate(rows[1:], start=2):
        d = {h: (r[i] if i < len(r) else "") for i, h in enumerate(hdrs)}
        d["_row_idx"] = r_idx
        iid = (d.get("Internal ID") or "").strip()
        if iid:
            iid_to_data[iid] = d

    state = load_state()
    today = date.today()
    now = datetime.now()

    # Build per-bid eligibility list
    candidates = []
    skipped = []
    for key, info in state.items():
        if info.get("status") in ("done", "stale", "skip"):
            continue
        d = iid_to_data.get(key)
        if not d:
            skipped.append((info.get("legacy_bid_id", "?"), "not in CRM"))
            continue
        bid_id = (d.get("Bid #") or info.get("legacy_bid_id", "?")).strip()
        notes = (d.get("Notes") or "").upper()
        if any(t in notes for t in INACTIVE_TAGS):
            info["status"] = "done"
            skipped.append((bid_id, "inactive flag"))
            continue
        status = (d.get("Status") or "").strip()
        if status in ("Won", "Lost", "Withdrawn", "No Decision", "No Bid"):
            info["status"] = "done"
            skipped.append((bid_id, f"Status={status}"))
            continue
        last_sent = parse_dt_safe(info.get("last_sent", ""))
        if not last_sent:
            skipped.append((bid_id, "no last_sent"))
            continue
        crm_email = first_email(d.get("Contact Email", ""))
        if not crm_email:
            skipped.append((bid_id, "no CRM email"))
            continue
        project_name = (d.get("Project Name") or "").strip()
        # Reply detection (project keywords + contact domain). For the FIRST
        # follow-up (attempts<=1) use NO lookback widening — otherwise the
        # GC's own pre-submission invitation/addendum email (which contains
        # the project name) falsely registers as a "reply" and suppresses the
        # bid's first nudge (a real ~$300K bid behind a GC pre-submission email).
        _attempts = info.get("attempts", 1)
        _widen = 0 if _attempts <= 1 else 3
        if has_replied_since(project_name, crm_email, last_sent.date(),
                             bid_id=bid_id, widen_days=_widen):
            info["status"] = "done"
            info["closed_at"] = now.isoformat(timespec="seconds")
            info["closed_reason"] = f"reply from {crm_email} about {project_name[:40]}"
            skipped.append((bid_id, f"reply received about {project_name[:35]}"))
            continue
        attempts = info.get("attempts", 1)
        if attempts >= MAX_ATTEMPTS:
            info["status"] = "stale"
            skipped.append((bid_id, f"max attempts hit ({MAX_ATTEMPTS})"))
            continue
        next_attempt = attempts + 1
        interval_h = INTERVAL_HOURS.get(next_attempt, 6)
        elapsed_h = (now - last_sent).total_seconds() / 3600
        if not args.force and elapsed_h < interval_h:
            hrs = interval_h - elapsed_h
            skipped.append((bid_id, f"attempt {next_attempt} in {hrs:.1f}h"))
            continue
        # Eligible
        try:
            amt_num = int(str(d.get("Bid Amount ($)", "")).replace("$","").replace(",","").split(".")[0])
        except Exception:
            amt_num = 0
        candidates.append({
            "internal_id": key,
            "bid_id": bid_id,
            "project": project_name,
            "contact_email": crm_email,
            "contact_name": (d.get("Contact Name") or "").strip(),
            "amount": d.get("Bid Amount ($)", "") or "",
            "amount_num": amt_num,
            "sub_date_str": d.get("Bid Submitted Date", ""),
            "attempt_num": next_attempt,
            "state_info": info,
            "_row_idx": d["_row_idx"],
        })

    # Group by contact_email (lowercase normalized)
    groups = defaultdict(list)
    for c in candidates:
        groups[c["contact_email"].lower()].append(c)

    print(f"=== Consolidated chase queue (today {today}) ===")
    print(f"Total eligible bids: {len(candidates)} across {len(groups)} unique contacts")
    print(f"Skipped: {len(skipped)}")
    print()

    if not candidates:
        print("Nothing to fire.")
        return

    # Print plan
    fire_plan = []
    for email, bids in sorted(groups.items(), key=lambda x: -sum(b["amount_num"] for b in x[1])):
        total_value = sum(b["amount_num"] for b in bids)
        max_attempt = max(b["attempt_num"] for b in bids)
        proj_list = ", ".join(b["bid_id"] for b in bids)
        print(f"  → {email[:35]:<35}  attempt #{max_attempt}  "
              f"({len(bids)} bids, ${total_value:,}): {proj_list}")
        fire_plan.append((email, bids))
    print()

    if not args.apply:
        print(f"[chase-consolidated] DRY-RUN — would send {len(fire_plan)} emails "
              f"({sum(len(b) for _, b in fire_plan)} bids consolidated).")
        return

    # FIRE — one email per recipient
    sent_count = 0
    for i, (email, bids) in enumerate(fire_plan):
        rcpt_name = bids[0]["contact_name"]
        subj, body = build_consolidated_message(bids, rcpt_name, email)
        if i > 0:
            time.sleep(args.interval)
        # Re-check for a reply IMMEDIATELY before each send — the plan was
        # built before up-to-hours of interval sleeps; a mid-batch reply
        # must stop this send.
        try:
            from _lib.presend_reply_guard import recipient_replied_recently
            _iid = (bids[0].get("internal_id") or bids[0].get("iid") or "").strip()
            _g = recipient_replied_recently(
                to_email=email, iid_full=_iid,
                project_name=bids[0].get("project", "") or bids[0].get("project_name", ""),
                hours=72)
            if _g:
                print(f"[{i+1}/{len(fire_plan)}] SKIP {email} — replied {_g.get('at','?')} (guard)")
                continue
        except Exception as _e:
            print(f"[{i+1}/{len(fire_plan)}] warn: presend guard unavailable: {_e}")
        print(f"[{i+1}/{len(fire_plan)}] Sending to {email} "
              f"({len(bids)} bids, max attempt #{max(b['attempt_num'] for b in bids)})...")
        r = subprocess.run(
            [sys.executable, str(SEND_EMAIL),
             "--to", email,
             "--cc", CC_INTERNAL,
             "--subject", subj,
             "--body", body,
             "--no-signature"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60,
        )
        if '"status": "sent"' in (r.stdout or ""):
            sent_count += 1
            now_iso = datetime.now().isoformat(timespec="seconds")
            log(f"[chase-consolidated] SENT to {email} ({len(bids)} bids)")
            # Update state for ALL bids in group
            for b in bids:
                info = b["state_info"]
                info["attempts"] = b["attempt_num"]
                info["last_sent"] = now_iso
                info["contact_email"] = email
                info["legacy_bid_id"] = b["bid_id"]
                info["project_snapshot"] = b["project"]
                state[b["internal_id"]] = info
            save_state(state)
            # Activity log — one entry per bid in this consolidated send
            try:
                from activity_log import log_event
                bid_list_str = ", ".join(b["bid_id"] for b in bids)
                for b in bids:
                    log_event(
                        internal_id=b["internal_id"],
                        bid_id=b["bid_id"],
                        project=b["project"],
                        type="follow_up",
                        direction="outbound",
                        counterparty=email,
                        channel="email",
                        summary=(f"Consolidated FU attempt #{b['attempt_num']} — "
                                 f"sent with {len(bids)-1} other bid(s): {bid_list_str}"),
                    )
            except Exception as e:
                log(f"[chase-consolidated] Activity Log write failed: {e}", quiet=True)
        else:
            log(f"[chase-consolidated] FAIL to {email}: {(r.stdout or '')[:200]}")

    print()
    print(f"[chase-consolidated] Done. {sent_count}/{len(fire_plan)} emails sent.")


if __name__ == "__main__":
    # RETIRED 2026-06-16 (god-level rebuild): collapsed to ONE pipeline.
    import sys as _sys
    if "--force-legacy" not in _sys.argv:
        print("RETIRED: use the single pipeline — morning_chase_report.py (decide) -> "
              "chase_executor.py (send, APPROVAL-GATED). This legacy sender no longer "
              "fires; pass --force-legacy only if you know why.")
        raise SystemExit(0)
    main()
