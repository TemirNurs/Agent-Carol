#!/usr/bin/env python3
"""
log_activity.py - Append a work event to today's activity log.

The activity log answers "what have we done today" when team members ask Carol.

PRIVACY: This log is for OPERATIONAL events only (CRM updates, follow-ups sent,
audit findings, scripts run). NEVER log private chat content (no quotes from
teammates' Telegram messages). The whole team can read this log.

Usage from another script:
  from log_activity import log_activity
  log_activity("📤 Follow-ups", "Sent 5 FU emails to overdue Bid Submitted bids")
  log_activity("📊 CRM", "Updated BID-0057 Status: Awaiting -> Lost (Caleb reply, $26K over winner)")

From CLI:
  python scripts/log_activity.py --section "📤 Follow-ups" --text "Sent batch of 19/29"
"""
from __future__ import annotations
import argparse
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = ROOT / "data" / "memory" / "activity_log_today.md"

# Privacy: never let chat content reach the log. Heuristic word list — if
# any of these appear in the text, refuse to log and write to a separate
# audit_violations log instead.
CHAT_LEAK_MARKERS = (
    "sviatlana asked",
    "sviatlana said",
    "sergey asked",
    "sergey said",
    "nursultan asked",
    "nursultan said",
    "user said",
    "you asked",
    "you said",
    "private message",
)


def _has_chat_leak(text: str) -> bool:
    if not text: return False
    t = text.lower()
    return any(m in t for m in CHAT_LEAK_MARKERS)


def log_activity(section: str, text: str, force: bool = False) -> bool:
    """Append a bullet under section heading. Returns True if logged."""
    if not text:
        return False
    if _has_chat_leak(text) and not force:
        # Don't log — write to a separate "tried to leak" file for review
        violations = ROOT / "data" / "logs" / "activity_log_privacy_violations.log"
        violations.parent.mkdir(parents=True, exist_ok=True)
        with open(violations, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')}\tBLOCKED\t{section}\t{text[:300]}\n")
        return False

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    today_str = date.today().strftime("%Y-%m-%d (%A)")

    # If file doesn't exist or is for a different day, rotate
    existing = LOG_PATH.read_text(encoding="utf-8") if LOG_PATH.exists() else ""
    if not existing or f"# Activity Log — {today_str}" not in existing.split("\n", 3)[0]:
        # Archive yesterday's log if it exists
        if existing.strip():
            first_line = existing.split("\n", 1)[0]
            # Extract date from "# Activity Log — YYYY-MM-DD ..."
            import re as _re
            m = _re.search(r"(\d{4}-\d{2}-\d{2})", first_line)
            if m:
                prev_date = m.group(1)
                archive = ROOT / "data" / "memory" / f"activity_log_{prev_date}.md"
                if not archive.exists():
                    archive.write_text(existing, encoding="utf-8")
        # Start fresh log for today
        LOG_PATH.write_text(
            f"# Activity Log — {today_str}\n\n"
            f"> Chronological work events. Appended by Claude Code sessions, daemon\n"
            f"> tasks, and major commands. PRIVACY: operational events only, never\n"
            f"> private chat content.\n\n"
            f"---\n\n",
            encoding="utf-8",
        )

    # Append the new entry under the appropriate section
    current = LOG_PATH.read_text(encoding="utf-8")
    timestamp = datetime.now().strftime("%H:%M")
    section_header = f"## {section}"

    if section_header in current:
        # Append a bullet under the existing section
        lines = current.split("\n")
        out_lines = []
        inserted = False
        i = 0
        while i < len(lines):
            out_lines.append(lines[i])
            if not inserted and lines[i].strip() == section_header:
                # Find end of this section (next ## or end of file)
                j = i + 1
                last_content = i
                while j < len(lines):
                    if lines[j].startswith("## ") or lines[j].startswith("# "):
                        break
                    if lines[j].strip():
                        last_content = j
                    j += 1
                # Insert after the last content line of this section
                # Copy remaining lines up to insertion point
                k = i + 1
                while k <= last_content:
                    out_lines.append(lines[k])
                    k += 1
                out_lines.append(f"- `{timestamp}` {text}")
                # Continue from line after last_content
                i = last_content
                inserted = True
            i += 1
        new_content = "\n".join(out_lines)
        if not inserted:
            new_content = current + f"\n- `{timestamp}` {text}\n"
    else:
        # Create new section at end
        new_content = current.rstrip() + f"\n\n## {section}\n- `{timestamp}` {text}\n"

    LOG_PATH.write_text(new_content, encoding="utf-8")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--section", required=True, help="Section heading (emoji + label)")
    ap.add_argument("--text", required=True, help="The bullet text to append")
    ap.add_argument("--force", action="store_true", help="Override chat-leak filter")
    args = ap.parse_args()
    ok = log_activity(args.section, args.text, force=args.force)
    print("logged" if ok else "BLOCKED (chat leak detected — see data/logs/activity_log_privacy_violations.log)")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
