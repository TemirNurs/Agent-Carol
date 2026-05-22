#!/usr/bin/env python3
r"""
launch_chase_detached.py - Spawn chase_silent_followups.py as a TRULY detached
Windows process that survives the parent shell terminating.

Why this exists: PowerShell `Start-Process -WindowStyle Hidden` and bash
`nohup`/`&` do NOT fully detach on Windows. When the parent terminal/session
ends, the child gets cleaned up too — that's why PID 4964 died at 15:39
even though we expected it to run until ~8 PM.

This launcher uses `subprocess.Popen` with `DETACHED_PROCESS |
CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW` flags so the child is fully
independent of any parent process. The launcher exits immediately after
spawning; the chase continues even if every interactive shell on the
machine closes.

Usage:
  python scripts/launch_chase_detached.py                       # defaults
  python scripts/launch_chase_detached.py --interval 1500       # 25-min
  python scripts/launch_chase_detached.py --max-per-recipient 3
  python scripts/launch_chase_detached.py --dry-run             # just print
"""
from __future__ import annotations
import argparse
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHASE_SCRIPT = ROOT / "scripts" / "chase_silent_followups.py"
LOG_DIR = ROOT / "data" / "logs"
LOCK_FILE = ROOT / "data" / "memory" / "chase_silent.lock"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=1500,
                    help="Seconds between fires (default 1500 = 25 min)")
    ap.add_argument("--max-per-recipient", type=int, default=3,
                    help="Daily cap per recipient email (default 3)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print what would launch; don't spawn")
    args = ap.parse_args()

    # Pre-flight: check lock — if a chase is already alive, refuse to launch
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text(encoding="utf-8").strip())
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x0400, False, pid)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                print(f"[launch] ABORT — chase already running (PID {pid}). "
                      f"To force-restart, first kill PID {pid} and delete {LOCK_FILE}.")
                sys.exit(2)
        except Exception:
            # Stale lock — safe to delete
            try:
                LOCK_FILE.unlink()
            except Exception:
                pass

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / "chase_silent_run.log"
    err_path = LOG_DIR / "chase_silent_run.err"

    cmd = [
        sys.executable, str(CHASE_SCRIPT),
        "--apply",
        "--interval", str(args.interval),
        "--max-per-recipient", str(args.max_per_recipient),
    ]
    print(f"[launch] cmd: {' '.join(cmd)}")
    print(f"[launch] stdout -> {log_path}")
    print(f"[launch] stderr -> {err_path}")

    if args.dry_run:
        print("[launch] --dry-run, not spawning")
        return

    # Windows-specific flags for TRUE detachment:
    # DETACHED_PROCESS (0x08)     — detach from parent's console
    # CREATE_NEW_PROCESS_GROUP    — own process group, immune to Ctrl+C / shell hangup
    # CREATE_NO_WINDOW            — no console window
    DETACHED = 0x00000008
    CREATE_NEW_PG = 0x00000200
    NO_WINDOW = 0x08000000
    flags = DETACHED | CREATE_NEW_PG | NO_WINDOW

    # Open log files and pass file descriptors to the child
    # Append mode so multiple restarts accumulate
    lf = open(log_path, "ab")
    ef = open(err_path, "ab")
    # Write a header so it's clear when this run started
    header = f"\n=== launch_chase_detached.py spawn @ {datetime.now().isoformat(timespec='seconds')} ===\n"
    lf.write(header.encode("utf-8"))
    lf.flush()

    proc = subprocess.Popen(
        cmd,
        stdout=lf,
        stderr=ef,
        stdin=subprocess.DEVNULL,
        creationflags=flags,
        cwd=str(ROOT),
        close_fds=True,
    )
    # IMPORTANT: do NOT call proc.wait() — that would re-couple us to it.
    # Just record the PID and exit.
    print(f"[launch] spawned chase PID={proc.pid} at {datetime.now().strftime('%H:%M:%S')}")
    print(f"[launch] tail logs:    type {log_path}")
    print(f"[launch] check status: tasklist /FI \"PID eq {proc.pid}\"")


if __name__ == "__main__":
    main()
