#!/usr/bin/env python3
"""
sync_openclaw_workspace.py — Keep Telegram Carol's instruction files in sync
with the canonical Carol agent files.

Problem this fixes:
  - The OpenClaw agent (Carol over Telegram/WhatsApp) reads its rules from
    C:\\Users\\Nursm\\.openclaw\\workspace\\AGENTS.md, USER.md, IDENTITY.md.
  - The canonical edits we make for Carol's behavior live at
    C:\\Agent Carol\\AGENTS.md, USER.md, IDENTITY.md.
  - Without an explicit sync, those two sets drift and Carol reverts to old
    rules (e.g. she stops knowing about crm_stats.py --list-status etc).

This script copies the canonical files to the OpenClaw workspace whenever
the canonical version is newer. Idempotent — does nothing if already in sync.

Run:
  python scripts/sync_openclaw_workspace.py            # one-shot sync
  python scripts/sync_openclaw_workspace.py --quiet
  python scripts/sync_openclaw_workspace.py --check    # report drift, no copy
"""

import argparse
import hashlib
import shutil
import sys
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

CAROL_ROOT     = Path("C:/Agent Carol")
OPENCLAW_ROOT  = Path("C:/Users/Nursm/.openclaw/workspace")
LOG_FILE       = CAROL_ROOT / "data" / "logs" / "sync_openclaw.log"

# Canonical → OpenClaw mapping.
# Add entries here if more Carol files need to reach the Telegram agent.
FILES = [
    "AGENTS.md",
    "USER.md",
    "IDENTITY.md",
    "AGENTS_LESSONS.md",  # don't-repeat rules — Carol must read these too
]


def file_hash(path: Path) -> str:
    if not path.exists(): return ""
    return hashlib.sha1(path.read_bytes()).hexdigest()[:12]


def log(msg: str, quiet: bool = False):
    if not quiet:
        print(msg)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now().isoformat(timespec='seconds')}  {msg}\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--check", action="store_true",
                    help="Report drift without copying")
    args = ap.parse_args()

    if not OPENCLAW_ROOT.exists():
        log(f"[sync] OpenClaw workspace not found: {OPENCLAW_ROOT}", args.quiet)
        return 0

    synced = 0
    in_sync = 0
    missing = 0

    for name in FILES:
        src = CAROL_ROOT / name
        dst = OPENCLAW_ROOT / name
        if not src.exists():
            log(f"  [skip] {name} not in {CAROL_ROOT}", args.quiet)
            missing += 1
            continue
        src_hash = file_hash(src)
        dst_hash = file_hash(dst)
        if src_hash == dst_hash:
            in_sync += 1
            continue
        if args.check:
            log(f"  [drift] {name}: canonical={src_hash} openclaw={dst_hash}", args.quiet)
            synced += 1
            continue
        shutil.copy2(src, dst)
        log(f"  [sync] {name}  {dst_hash or '(new)'} -> {src_hash}", args.quiet)
        synced += 1

    if args.check:
        log(f"[sync] CHECK: {synced} file(s) drifted, {in_sync} in sync, {missing} missing", args.quiet)
    else:
        log(f"[sync] {synced} file(s) updated, {in_sync} already in sync, {missing} missing source", args.quiet)
    return 0


if __name__ == "__main__":
    sys.exit(main())
