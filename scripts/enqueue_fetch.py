#!/usr/bin/env python3
r"""enqueue_fetch.py — queue a REAL background doc-fetch that the daemon runs + notifies.

Carol runs this IN-TURN instead of faking "I'll let you know." It writes a job to
data/fetch_queue/; the daemon's fetch_worker picks it up within a few minutes,
downloads the bid documents from the portal, and PUSHES the result back to the
requester on Telegram when done. Because a real job now exists, it is TRUTHFUL for
Carol to say "queued — you'll get a message here when it's done."

Usage:
  python scripts/enqueue_fetch.py --bid BID-0101
  python scripts/enqueue_fetch.py --project "350 Hein"
  python scripts/enqueue_fetch.py --bid BID-0101 --by <chat_id>   # notify this chat_id
"""
import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
QDIR = ROOT / "data" / "fetch_queue"
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bid", default="", help="BID-NNNN")
    ap.add_argument("--project", default="", help='Project name (fuzzy)')
    ap.add_argument("--by", default="", help="requester Telegram chat_id (who to notify)")
    a = ap.parse_args()
    target = (a.bid or a.project).strip()
    if not target:
        print('ERROR: give --bid BID-NNNN or --project "name"')
        return 2
    QDIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    jid = now.strftime("%Y%m%d-%H%M%S") + "-" + hashlib.md5(target.encode()).hexdigest()[:6]
    job = {
        "id": jid,
        "bid": a.bid.strip(),
        "project": a.project.strip(),
        "target": target,
        "requested_by": a.by.strip(),
        "requested_at": now.isoformat(timespec="seconds"),
        "status": "queued",
        "attempts": 0,
    }
    (QDIR / f"{jid}.json").write_text(json.dumps(job, indent=2), encoding="utf-8")
    print(f"✅ Queued a background doc-fetch for {target}. The daemon will pull the "
          f"documents and message you here when it's done (usually a few minutes) — "
          f"you don't need to wait or ask again.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
