#!/usr/bin/env python3
r"""
followup_intelligence_brief.py — DEPRECATED (2026-06-01). Now a thin shim.

WHY: this script was a SECOND chase-planning brain. It used the OLD, rejected
cadence ("recently chased, wait 5-7 days") and had NO proposal-quiet-window
guard, so on 6/1 it told the user to chase AMC South Barrington (whose
REVISED proposal went out 5/28 — inside the 5-day quiet window) while saying
"wait" on the 8 bids the approved workflow correctly marks due that day.
Two brains → contradictory advice → exactly the "confused shit" the user
banned ("You should have only ONE chasing activity").

There is ONE source of truth for "what do we chase / chase brief / follow-up
status": `morning_chase_report.py`. This shim forwards to it so any caller —
the daemon (this task is OFF anyway) or Carol running it on demand — gets the
single correct answer. Original logic preserved in
`followup_intelligence_brief.py.deprecated-20260601`.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "scripts" / "morning_chase_report.py"


def main() -> int:
    sys.stderr.write(
        "[followup_intelligence_brief] DEPRECATED — deferring to "
        "morning_chase_report.py (single source of truth for chases).\n"
    )
    fwd = []
    if "--quiet" in sys.argv:
        fwd.append("--quiet")
    if "--date" in sys.argv:
        i = sys.argv.index("--date")
        if i + 1 < len(sys.argv):
            fwd += ["--date", sys.argv[i + 1]]
    cmd = [sys.executable, str(TARGET), *fwd]
    return subprocess.run(cmd).returncode


if __name__ == "__main__":
    sys.exit(main())
