#!/usr/bin/env python3
r"""
send_followups_throttled.py - Send all overdue follow-ups on a fixed cadence.

Uses a deterministic template (matches Nursultan's existing FU style) instead
of an LLM draft, so it's reliable for batch use. Sends one email every
--interval seconds (default 1200 = 20 min) with the internal CC list CC'd.

The cadence stage is picked based on the bid's age:
  age <=  3 days     -> FU1 Receipt Confirm  ("Have you been able to review...")
  age <=  9 days     -> FU2 Award Status     ("Any update on timeline...")
  age <= 35 days     -> FU3 Feedback Request ("Has the GC made a decision...")
  age >  35 days     -> FU4 Relationship     ("How's the project going...")

Templates pulled from the user's existing Follow-Up style in Gmail Sent folder.

Usage:
  python scripts/send_followups_throttled.py                # 1 every 20min, dry-run
  python scripts/send_followups_throttled.py --apply        # actually send
  python scripts/send_followups_throttled.py --apply --interval 1200  # explicit 20min
  python scripts/send_followups_throttled.py --skip BID-0007,BID-0010,BID-0012
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

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

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PYEXE = sys.executable

CC_INTERNAL = os.environ.get("CCF_INTERNAL_CC", "cs@carolinacommercialfinishes.com")  # cs@ + internal CC list

ACTIVE_STATUSES = {"Bid Submitted", "Awaiting Decision"}
INACTIVE_FLAGS = ("[BOUNCE]", "[NOT BIDDING]", "[WITHDRAWN]", "[ON HOLD]")


_REPLY_CACHE = {}
def _has_replied_recently(recip_email: str, days: int = 14) -> bool:
    """Return True if `recip_email` sent us any message in the last `days`.
    Caches per-process to avoid hammering IMAP on a long chase queue.

    AGENTS_LESSONS.md R3 — never re-chase a contact who already replied.
    """
    if not recip_email: return False
    key = recip_email.strip().lower()
    if key in _REPLY_CACHE:
        return _REPLY_CACHE[key]
    import imaplib, os
    user = os.environ.get("GMAIL_USER", "estimates@carolinacommercialfinishes.com")
    pw   = os.environ.get("GMAIL_APP_PASSWORD", "")
    since = (date.today() - timedelta(days=days)).strftime("%d-%b-%Y")
    try:
        M = imaplib.IMAP4_SSL("imap.gmail.com")
        M.login(user, pw)
        # All Mail, not INBOX — replies are auto-labeled out of the inbox.
        M.select('"[Gmail]/All Mail"', readonly=True)
        st, ids = M.search(None, f'(FROM "{key}" SINCE "{since}")')
        n = len(ids[0].split()) if ids[0] else 0
        M.logout()
    except Exception:
        n = 0
    result = n > 0
    _REPLY_CACHE[key] = result
    return result

SIGNATURE = """Best,
Nursultan Temirbaev | Manager
Carolina Commercial Finishes
c: (980) 348-1827
estimates@carolinacommercialfinishes.com | CarolinaCommercialFinishes.com
3308 Chancellor Lane | Monroe, NC 28110"""


def parse_date_safe(s):
    if not s: return None
    for fmt in ("%a, %d %b %Y", "%m/%d/%Y", "%Y-%m-%d", "%d-%b-%Y"):
        try: return datetime.strptime(str(s).strip()[:30], fmt).date()
        except Exception: pass
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(s).date()
    except Exception:
        return None


def first_name(full):
    if not full: return ""
    full = full.strip()
    # Strip titles or trailing punctuation
    parts = full.split()
    if not parts: return ""
    first = parts[0].rstrip(",").rstrip(".")
    # Skip if first token looks like an initial (e.g. "S.")
    if len(first) <= 2 and first.endswith("."):
        # Use the next part if available
        if len(parts) > 1:
            return parts[1].rstrip(",")
        return ""
    return first


def first_email(s):
    if not s: return ""
    import re
    parts = re.split(r"[\s,;]+", s.strip())
    return next((p for p in parts if "@" in p), "")


def format_amount(s):
    """Output 'USD 41,375' style (avoids OpenClaw $-mangling bug)."""
    if not s: return ""
    s = str(s).replace("$", "").strip()
    return f"USD {s}"


def build_followup(bid):
    """Return (subject, body) for a follow-up email."""
    proj = bid["Project Name"]
    bid_id = bid["Bid #"]
    contact_name = first_name(bid.get("Contact Name", ""))
    if not contact_name:
        contact_name = "there"
    amount = format_amount(bid.get("Bid Amount ($)", ""))
    sub_date = parse_date_safe(bid.get("Bid Submitted Date"))
    age = (date.today() - sub_date).days if sub_date else 0
    sub_date_str = sub_date.strftime("%B %d, %Y") if sub_date else "recently"

    subject = f"Follow-Up: {proj} ({bid_id})"

    # Pick template by age
    if age <= 9:
        # FU1/FU2 style — short check-in
        body = (
            f"Hi {contact_name},\n\n"
            f"Hope this email finds you well. We submitted our proposal for the {proj} ({bid_id}) "
            f"for {amount} on {sub_date_str}. It's been {age} day{'s' if age != 1 else ''} since our submission.\n\n"
            f"Is the project still active, or has the timeline shifted?\n\n"
            f"{SIGNATURE}\n"
        )
    elif age <= 35:
        # FU3 style — feedback / decision
        body = (
            f"Hi {contact_name},\n\n"
            f"Following up on our bid for {proj} ({bid_id}) for {amount}, submitted {age} days ago.\n\n"
            f"Is there an update on the project timeline or decision status?\n\n"
            f"{SIGNATURE}\n"
        )
    else:
        # FU4 style — relationship / longer gap
        body = (
            f"Hi {contact_name},\n\n"
            f"It's been about {age} days since we submitted our {amount} proposal for {proj} ({bid_id}). "
            f"Wanted to check in and see whether the project moved forward, was awarded, or is still pending.\n\n"
            f"Happy to revise pricing or scope if anything has changed on your end.\n\n"
            f"{SIGNATURE}\n"
        )
    return subject, body


def send_one(to, subject, body, cc, dry_run):
    """Run send_email.py for one email. Returns dict with status."""
    # Take only first email if multi-email cell
    to_clean = first_email(to)
    if not to_clean:
        return {"status": "no_email", "to": to}
    if dry_run:
        return {"status": "dry_run", "to": to_clean, "subject": subject}
    cmd = [
        PYEXE, str(ROOT / "scripts" / "send_email.py"),
        "--to", to_clean,
        "--subject", subject,
        "--body", body,
        "--no-signature",   # we already include the signature in the body
    ]
    if cc:
        cmd.extend(["--cc", cc])
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=60)
    out = r.stdout or ""
    if '"status": "sent"' in out:
        return {"status": "sent", "to": to_clean}
    return {"status": "error", "to": to_clean, "raw": out[:200]}


def mark_fu_sent(bid_row_idx, fu_col_name):
    """Stamp today's date in the appropriate FU column on the CRM."""
    from crm_lib import batch_update_rows
    today_str = date.today().strftime("%m/%d/%Y")
    batch_update_rows("Bid Log", [(bid_row_idx, fu_col_name, today_str)])


def pick_fu_column(age):
    """Map age to the FU stage we just sent. Matches cadence:
       FU1 day 2-6 | FU2 day 7-29 | FU3 day 30-89 | FU4 day 90+
       Must stay in sync with followup_plan.py stage logic."""
    if age < 7:    return "FU1 Date"
    if age < 30:   return "FU2 Date"
    if age < 90:   return "FU3 Date"
    return "FU4 Date"


def main():
    # GLOBAL CHASE-BATCH LOCK — refuses to start if ANY other chase pipeline is alive
    _scripts_dir = str(Path(__file__).resolve().parent)
    if _scripts_dir not in sys.path:
        sys.path.insert(0, _scripts_dir)
    try:
        from _lib.chase_batch_lock import acquire as _global_acquire, release as _global_release
        _need_lock = ("--apply" in sys.argv)
        if _need_lock and not _global_acquire(__file__):
            sys.exit(2)
        if _need_lock:
            import atexit as _atx; _atx.register(_global_release)
    except ImportError as _e:
        if "--apply" in sys.argv:
            print(f"WARN: global chase lock unavailable ({_e}) — refusing to run",
                  file=sys.stderr)
            sys.exit(3)

    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="Actually send. Default is dry-run.")
    ap.add_argument("--interval", type=int, default=1200,
                    help="Seconds between sends (default 1200 = 20 min)")
    ap.add_argument("--skip", default="",
                    help="Bid IDs to skip, comma-separated")
    ap.add_argument("--cc", default=CC_INTERNAL,
                    help="CC addresses (comma-separated)")
    ap.add_argument("--max", type=int, default=0,
                    help="Cap how many to send (0 = all)")
    args = ap.parse_args()

    skip_set = {s.strip().upper() for s in args.skip.split(",") if s.strip()}

    from crm_lib import get_sheet
    sh = get_sheet("Bid Log")
    hdrs = sh.row_values(1)
    rows = sh.get_all_values()

    # Build queue of overdue active bids that need a follow-up
    queue = []
    for r_idx, row in enumerate(rows[1:], start=2):
        d = {h: (row[i] if i < len(row) else "") for i, h in enumerate(hdrs)}
        if (d.get("Status") or "").strip() not in ACTIVE_STATUSES:
            continue
        notes = (d.get("Notes") or "").upper()
        if any(flag in notes for flag in INACTIVE_FLAGS):
            continue
        if (d.get("Bid #") or "").upper() in skip_set:
            continue
        sub_date = parse_date_safe(d.get("Bid Submitted Date"))
        if not sub_date:
            continue
        age = (date.today() - sub_date).days
        # Skip if too new (less than 2 days)
        if age < 2:
            continue
        rcpt = first_email(d.get("Contact Email", ""))
        if not rcpt:
            continue
        # AGENTS_LESSONS.md R3: never chase a recipient who replied recently.
        # Re-asking after they already gave intel erodes the relationship and
        # was the exact bug that wasted 13 chases on 2026-05-21. Check Gmail
        # inbox for any reply from this recipient in the last 14 days.
        if _has_replied_recently(rcpt, days=14):
            continue
        d["_row_idx"] = r_idx
        d["_age"] = age
        d["_amount_num"] = _money_int(d.get("Bid Amount ($)", ""))
        queue.append(d)

    # Sort by amount desc so high-value goes out first
    queue.sort(key=lambda x: -x["_amount_num"])
    if args.max > 0:
        queue = queue[: args.max]

    print(f"[fu] {len(queue)} overdue follow-ups queued")
    print(f"[fu] interval: {args.interval}s = {args.interval/60:.1f} min")
    print(f"[fu] CC: {args.cc}")
    print(f"[fu] mode: {'APPLY (real sends)' if args.apply else 'DRY-RUN'}")
    print()

    started = datetime.now()
    sent_count = 0
    for i, bid in enumerate(queue, 1):
        bid_id = bid.get("Bid #", "")
        rcpt = first_email(bid.get("Contact Email", ""))
        subject, body = build_followup(bid)
        eta_min = (i - 1) * args.interval / 60
        send_time = started + timedelta(minutes=eta_min)
        print(f"[{i}/{len(queue)}] {bid_id}  ${bid['_amount_num']:>9,}  "
              f"age={bid['_age']:>2}d  -> {rcpt[:35]:<35}  @ {send_time.strftime('%H:%M')}")

        if i > 1 and args.apply:
            time.sleep(args.interval)

        # Re-check for a reply IMMEDIATELY before each send — the queue-build
        # check is hours stale by the time later sends fire (a reply landing
        # mid-batch must stop this send; the 5/29 Matt Burich incident).
        if args.apply:
            try:
                from _lib.presend_reply_guard import recipient_replied_recently
                _iid = (bid.get("Internal ID") or "").strip()
                _g = recipient_replied_recently(
                    to_email=rcpt, iid_full=_iid,
                    project_name=bid.get("Project Name", ""), hours=72)
                if _g:
                    print(f"          [SKIP] replied {_g.get('at','?')} — guard stopped send")
                    continue
            except Exception as _e:
                print(f"          [warn] presend guard unavailable: {_e}")

        result = send_one(rcpt, subject, body, args.cc, dry_run=not args.apply)
        status = result.get("status", "?")
        marker = {"sent": "[OK]", "dry_run": "[--]", "error": "[!!]",
                  "no_email": "[X]"}.get(status, "[?]")
        print(f"          {marker} {status}")
        if status == "sent":
            sent_count += 1
            try:
                mark_fu_sent(bid["_row_idx"], pick_fu_column(bid["_age"]))
            except Exception as e:
                print(f"          CRM update failed: {e}")
            # Activity log so Carol knows what was sent
            try:
                from log_activity import log_activity
                amt_str = f"${bid['_amount_num']:,}"
                gc_name = bid.get("GC / Client", "?")[:30]
                proj_name = bid.get("Project Name", "?")[:45]
                log_activity(
                    "📤 Follow-ups",
                    f"{bid_id} -> {gc_name} ({rcpt}) — {proj_name} "
                    f"({amt_str}, age {bid['_age']}d)"
                )
            except Exception:
                pass

    print()
    print(f"[fu] done. {sent_count}/{len(queue)} sent ("
          f"{'real' if args.apply else 'dry-run'}).")


def _money_int(s):
    if not s: return 0
    s = str(s).replace("$", "").replace(",", "")
    try: return int(s.split(".")[0])
    except: return 0


if __name__ == "__main__":
    # RETIRED 2026-06-16 (god-level rebuild): collapsed to ONE pipeline.
    import sys as _sys
    if "--force-legacy" not in _sys.argv:
        print("RETIRED: use the single pipeline — morning_chase_report.py (decide) -> "
              "chase_executor.py (send, APPROVAL-GATED). This legacy sender no longer "
              "fires; pass --force-legacy only if you know why.")
        raise SystemExit(0)
    main()
