#!/usr/bin/env python3
r"""carol_report.py — FIXED read-only report dispatcher for the Telegram agent.

Why this exists: OpenClaw's exec approval has no Telegram UI, so the agent
couldn't run anything. Rather than allowlist `python` (= arbitrary code, unsafe),
we allowlist ONLY this dispatcher. It runs a HARD-CODED set of read-only
reporting scripts by name — no arbitrary args, no eval, no writes, no sends.
So the Telegram agent can pull reports instantly, but cannot run arbitrary code
or take any external action. (User-approved 6/29.)

Usage:  carol-report <open|open-list|recap|yesterday|stats|today|chases>
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# name -> [script, *fixed_args]. EVERY entry is read-only (no send/write).
REPORTS = {
    "open":      ["verified_report.py"],            # VERIFIED count headline (single source of truth)
    "open-list": ["verified_report.py", "--list"],  # VERIFIED ranked open list (deduped, doc-read $)
    "recap":     ["recap.py", "--today"],
    "yesterday": ["recap.py", "--yesterday"],
    "stats":     ["bid_stats.py", "--brief"],
    "today":     ["bids_today.py"],
    "chases":    ["morning_chase_report.py", "--quiet"],  # read-only: proposes, never sends
    "team":      ["team_transcript.py", "--ever"],  # who has ever messaged Carol
}


def main():
    name = (sys.argv[1].strip().lower() if len(sys.argv) > 1 else "")
    if name not in REPORTS:
        print(f"Unknown report '{name}'. Available: {', '.join(sorted(REPORTS))}")
        return 2
    script, *fixed_args = REPORTS[name]
    # ONLY the hard-coded script + hard-coded args run — no passthrough of caller args.
    cmd = [sys.executable, os.path.join(ROOT, "scripts", script), *fixed_args]
    return subprocess.run(cmd, cwd=ROOT).returncode


if __name__ == "__main__":
    sys.exit(main())
