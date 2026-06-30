#!/usr/bin/env python3
r"""build_report_snapshots.py — render the read-only reports to TEXT files that
Telegram-Carol READS (no exec).

Why: OpenClaw's allowlist exec mode categorically blocks .cmd shell wrappers, so
Carol cannot run `carol-report` on Telegram (and `python` can't be allowlisted —
that's arbitrary code). Instead, the daemon renders each read-only report to a
snapshot file here, and Carol answers report questions by READING the file via her
file tool. This is inherently read-only — it cannot send email or need approval.

Output: data/memory/report_cache/<name>.txt  (data/memory is gitignored — these
snapshots hold live operational data and must never be committed).

  python scripts/build_report_snapshots.py            # render all
  python scripts/build_report_snapshots.py chases     # render just one
"""
import datetime
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "memory" / "report_cache"
PY = sys.executable

# name -> [script, *args]  (mirror of carol_report.py REPORTS — all READ-ONLY)
REPORTS = {
    "chases":    ["morning_chase_report.py", "--quiet"],
    "open":      ["verified_report.py"],
    "open_list": ["verified_report.py", "--list"],
    "recap":     ["recap.py", "--today"],
    "yesterday": ["recap.py", "--yesterday"],
    "stats":     ["bid_stats.py", "--brief"],
    "today":     ["bids_today.py"],
}


def render(name, argv):
    cmd = [PY, str(ROOT / "scripts" / argv[0])] + argv[1:]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=300, cwd=str(ROOT))
        body = (r.stdout or "").strip() or (r.stderr or "").strip() or "(no output)"
    except Exception as e:
        body = f"(report failed: {e})"
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{name}.txt").write_text(
        f"# {name} snapshot — rendered {ts} (read-only; auto-refreshed by the daemon)\n\n{body}\n",
        encoding="utf-8")
    return len(body)


def main():
    only = [a for a in sys.argv[1:] if not a.startswith("-")] or list(REPORTS)
    for name in only:
        if name in REPORTS:
            n = render(name, REPORTS[name])
            print(f"wrote {name}.txt ({n} chars)")
        else:
            print(f"unknown report '{name}'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
