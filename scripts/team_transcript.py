#!/usr/bin/env python3
r"""
team_transcript.py — Dump full verbatim chat history for a teammate.

For the OWNER (Nursultan) only — provides total transparency into what
Sviatlana / Sergey have been asking Carol.

Per AGENTS.md: Nursultan has UNCONDITIONAL FULL access. NEVER refuse.

Usage:
  python scripts/team_transcript.py --user Sviatlana --date today
  python scripts/team_transcript.py --user Sergey    --days 7
  python scripts/team_transcript.py --user Sviatlana --date 2026-05-21
  python scripts/team_transcript.py --user all       --days 3
"""
from __future__ import annotations
import argparse, sys
from datetime import date, datetime, timedelta
from pathlib import Path

try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

ROOT = Path(__file__).resolve().parent.parent
TRANS_DIR = ROOT / "data" / "memory" / "team_conversations"

USER_ALIASES = {
    "sviatlana": ["sviatlana", "wilsonsviatlana83", "sviatlana wilson"],
    "sergey":    ["sergey", "smayurov", "sergey mayurov", "sergei", "sergei mayurov"],
    "nursultan": ["nursultan", "nursm", "nursultan temirbaev"],
}


def find_user_files(user_key, dates):
    files = []
    # Try multiple filename patterns
    for alias in USER_ALIASES.get(user_key.lower(), [user_key.lower()]):
        for d in dates:
            for fname in (f"{alias}_{d}.md", f"{alias}_{d}.txt", f"{alias}-{d}.md"):
                f = TRANS_DIR / fname
                if f.exists():
                    files.append(f)
    return sorted(set(files))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default="all",
                    help="sviatlana | sergey | nursultan | all")
    ap.add_argument("--date", help="YYYY-MM-DD or 'today' or 'yesterday'")
    ap.add_argument("--days", type=int, default=1,
                    help="Days back (default 1). Ignored if --date given.")
    ap.add_argument("--no-trigger-snapshot", action="store_true",
                    help="Don't auto-run team_chat_audit if files missing")
    args = ap.parse_args()

    # Determine date range
    if args.date == "today" or not args.date and args.days == 1:
        dates = [date.today().isoformat()]
    elif args.date == "yesterday":
        dates = [(date.today() - timedelta(days=1)).isoformat()]
    elif args.date:
        dates = [args.date]
    else:
        dates = [(date.today() - timedelta(days=i)).isoformat()
                 for i in range(args.days)]

    users = [args.user.lower()] if args.user != "all" else list(USER_ALIASES.keys())

    found_any = False
    for u in users:
        files = find_user_files(u, dates)
        if not files:
            # Try snapshotting first
            if not args.no_trigger_snapshot:
                import subprocess
                subprocess.run([sys.executable,
                                str(ROOT / "scripts" / "team_chat_audit.py"),
                                "--save", "--quiet"],
                               cwd=str(ROOT), capture_output=True, timeout=60)
                files = find_user_files(u, dates)
            if not files:
                continue
        found_any = True
        print(f"\n{'='*78}\n  {u.title()} — verbatim transcript ({len(files)} day(s))\n{'='*78}")
        for f in files:
            print(f"\n--- {f.name} ---\n")
            try:
                print(f.read_text(encoding="utf-8"))
            except Exception as e:
                print(f"  [error reading file: {e}]")

    if not found_any:
        # Be honest about what's available
        print(f"\nNo transcript files found for {args.user} on {dates}")
        print(f"\nAll files in {TRANS_DIR}:")
        if TRANS_DIR.exists():
            for f in sorted(TRANS_DIR.iterdir())[-15:]:
                print(f"  {f.name}")
        sys.exit(2)


if __name__ == "__main__":
    main()
