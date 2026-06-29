#!/usr/bin/env python3
r"""
process_pending_followups.py — Send (or skip) staged follow-ups awaiting approval.

After followup_scheduler.py stages drafts to data/pending_followups/,
the user replies "send pending" on Telegram → Carol runs this script.

Usage:
  python scripts/process_pending_followups.py                # send all pending
  python scripts/process_pending_followups.py --skip         # delete all without sending
  python scripts/process_pending_followups.py --bid BID-NNNN # send/skip just one
  python scripts/process_pending_followups.py --list         # show pending without acting
"""

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
PENDING_DIR = ROOT / "data" / "pending_followups"
LOG_FILE    = ROOT / "data" / "logs" / "pending_followups.log"


def list_pending(bid_filter: str | None = None) -> list[dict]:
    if not PENDING_DIR.exists(): return []
    out = []
    for p in sorted(PENDING_DIR.glob("*.json")):
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if bid_filter and r.get("bid_id") != bid_filter:
            continue
        r["_path"] = str(p)
        out.append(r)
    return out


def log(msg: str, quiet: bool = False):
    if not quiet:
        print(msg)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now().isoformat(timespec='seconds')}  {msg}\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bid", default=None)
    ap.add_argument("--skip", action="store_true",
                    help="Delete pending without sending")
    ap.add_argument("--list", action="store_true",
                    help="Show pending and exit")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    pending = list_pending(args.bid)
    if not pending:
        log("[pending] no follow-ups staged. Nothing to do.", args.quiet)
        return 0

    if args.list:
        for r in pending:
            print(f"  {r.get('bid_id')} ${r.get('amount',0):,.0f}  {r.get('fu_type','?')}  → {r.get('to','?')}")
            print(f"    Subject: {r.get('subject','')[:80]}")
        return 0

    if args.skip:
        for r in pending:
            Path(r["_path"]).unlink(missing_ok=True)
            log(f"  SKIPPED {r.get('bid_id')}", args.quiet)
        return 0

    # Send each
    sent, failed = 0, 0
    for r in pending:
        bid = r.get("bid_id", "?")
        to = re.sub(r"\s+", ",", str(r.get("to", "")).strip()).strip(",")
        subject = r.get("subject", "")
        body = r.get("body", "")
        if not (to and subject and body):
            log(f"  SKIP {bid}: incomplete record", args.quiet)
            failed += 1
            continue
        s = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "send_email.py"),
             "--to", to, "--subject", subject, "--body", body],
            capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
        if '"status": "sent"' in s.stdout:
            log(f"  SENT {bid}  →  {to}", args.quiet)
            Path(r["_path"]).unlink(missing_ok=True)
            sent += 1
        else:
            log(f"  FAIL {bid}: {s.stdout[:200]}", args.quiet)
            failed += 1

    log(f"\n[pending] sent {sent}, failed {failed}, remaining {len(pending) - sent - failed}", args.quiet)
    return 0


if __name__ == "__main__":
    sys.exit(main())
