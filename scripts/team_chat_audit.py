#!/usr/bin/env python3
r"""
team_chat_audit.py — Owner-only tool to review what the CCF team is asking Carol.

Privacy model: Each Telegram user gets a separate Carol session (per-channel-peer
sessionKey). They can't see each other's chats. But the file system has all
sessions in one place at:
  C:\Users\Nursm\.openclaw\agents\main\sessions\*.jsonl

This script — running on Nursultan's owner machine — walks those JSONL files
and dumps a clean per-user transcript so Nursultan can audit team usage.

It also writes a per-user Markdown file under data/memory/team_conversations/
so you have a permanent record outside the OpenClaw runtime.

Usage:
  python scripts/team_chat_audit.py                    # list all senders
  python scripts/team_chat_audit.py --who <telegram_id>   # one user's transcript
  python scripts/team_chat_audit.py --since 2026-05-05 # filter by date
  python scripts/team_chat_audit.py --save             # write per-user .md files
  python scripts/team_chat_audit.py --quiet --save     # for cron/daemon
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
SESSIONS_DIR = Path(r"C:\Users\Nursm\.openclaw\agents\main\sessions")
OUT_DIR = ROOT / "data" / "memory" / "team_conversations"

# Known team members — labels for the report.
# Owner (empty-key session) is the audit operator; teammate id→name labels are
# sourced from env TEAM_TELEGRAM_IDS ("id:Name,id:Name") so no ids/names ship
# as literals.
KNOWN = {
    "":  ("Owner", "owner — full audit access"),
}
for _pair in os.environ.get("TEAM_TELEGRAM_IDS", "").split(","):
    _pair = _pair.strip()
    if ":" in _pair:
        _tid, _tname = _pair.split(":", 1)
        _tid, _tname = _tid.strip(), _tname.strip()
        if _tid:
            KNOWN[_tid] = (_tname or f"User {_tid}", "CCF teammate")

# JSONL files to skip (orphaned / replaced sessions)
SKIP_TOKENS = ("stale_", "reset.", "backup.", "heartbeat_bug",
               "no_revenue", "count_hallucination", "team_roles", "brief_loop")


def _is_active_session(filename: str) -> bool:
    return not any(t in filename for t in SKIP_TOKENS)


def _extract_message_body(content) -> str:
    """Strip Telegram metadata wrapper, return just the user-typed text."""
    if isinstance(content, list):
        content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
    s = str(content)
    # User messages from the Telegram bridge come wrapped in metadata blocks.
    # Real text is typically AFTER the last ``` block.
    parts = re.split(r"```\s*\n\s*\n", s)
    if len(parts) > 1:
        return parts[-1].strip()
    # Or after "Sender" block ends
    m = re.search(r"```\s*\n\s*\n(.+)\Z", s, re.DOTALL)
    if m:
        return m.group(1).strip()
    return s.strip()


def _extract_sender_id(content: str) -> str | None:
    """Pull the Telegram sender_id from a metadata block."""
    if not isinstance(content, str): return None
    m = re.search(r'"(?:sender_)?id":\s*"(\d{6,})"', content)
    return m.group(1) if m else None


def _strip_carol_xml(text: str) -> str:
    """Remove <final>...</final> tags and similar XML wrappers."""
    text = re.sub(r"</?final>", "", text)
    text = re.sub(r"</?response>", "", text)
    return text.strip()


def walk_sessions(since_date: date | None = None) -> dict:
    """Walk all session JSONL files, group events by Telegram sender_id."""
    by_sender = defaultdict(list)  # sender_id -> [{ts, role, text}, ...]
    if not SESSIONS_DIR.exists():
        return by_sender

    for sf in glob.glob(str(SESSIONS_DIR / "*.jsonl")):
        if not _is_active_session(os.path.basename(sf)):
            continue
        # Walk events; track who the current session belongs to
        current_sender = None
        try:
            with open(sf, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("type") != "message":
                        continue
                    msg = rec.get("message", {})
                    role = msg.get("role", "")
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
                    content = str(content)
                    ts = (rec.get("timestamp", "") or "")[:19]
                    if since_date and ts:
                        try:
                            if datetime.fromisoformat(ts).date() < since_date:
                                continue
                        except ValueError:
                            pass
                    # Identify the session's owner from user messages with metadata
                    if role == "user":
                        sid = _extract_sender_id(content)
                        if sid:
                            current_sender = sid
                            by_sender[sid].append({
                                "ts": ts,
                                "role": "user",
                                "text": _extract_message_body(content),
                                "session_file": os.path.basename(sf),
                            })
                    elif role == "assistant" and current_sender:
                        # Skip startup boilerplate
                        if content.startswith("✅ New session"):
                            continue
                        if not content.strip():
                            continue
                        by_sender[current_sender].append({
                            "ts": ts,
                            "role": "assistant",
                            "text": _strip_carol_xml(content),
                            "session_file": os.path.basename(sf),
                        })
        except Exception:
            continue

    # Sort each sender's events by timestamp
    for sid in by_sender:
        by_sender[sid].sort(key=lambda e: e["ts"])
    return by_sender


def summarize_user(sid: str, events: list[dict]) -> dict:
    """Per-sender stats."""
    user_msgs = [e for e in events if e["role"] == "user"]
    asst_msgs = [e for e in events if e["role"] == "assistant"]
    # Dedupe consecutive identical user messages (Telegram repeat-tap pattern)
    unique_user = []
    last = None
    for u in user_msgs:
        if u["text"] != last:
            unique_user.append(u)
        last = u["text"]
    return {
        "sender_id": sid,
        "name": KNOWN.get(sid, (f"User {sid}", "(unknown teammate)"))[0],
        "role": KNOWN.get(sid, ("", "(unknown)"))[1],
        "user_messages_total": len(user_msgs),
        "user_messages_unique": len(unique_user),
        "assistant_replies": len(asst_msgs),
        "first_seen": events[0]["ts"] if events else "",
        "last_seen": events[-1]["ts"] if events else "",
    }


def render_markdown(sid: str, events: list[dict]) -> str:
    """Render a per-sender Markdown transcript."""
    name, role = KNOWN.get(sid, (f"User {sid}", "(unknown)"))
    summary = summarize_user(sid, events)

    lines = [
        f"# Carol chat — {name}",
        f"_Telegram ID `{sid}` · {role}_",
        f"_Generated: {datetime.now().isoformat(timespec='seconds')}_",
        "",
        "## Stats",
        f"- Messages from {name}: **{summary['user_messages_total']}** ({summary['user_messages_unique']} unique)",
        f"- Replies from Carol: {summary['assistant_replies']}",
        f"- First seen: {summary['first_seen']}",
        f"- Last seen: {summary['last_seen']}",
        "",
        "## Transcript",
        "",
    ]
    last_text = None
    for e in events:
        # Collapse consecutive duplicate user messages
        if e["role"] == "user" and e["text"] == last_text:
            continue
        last_text = e["text"] if e["role"] == "user" else None
        speaker = "👤 " + name if e["role"] == "user" else "🤖 Carol"
        lines.append(f"**{speaker}** _{e['ts']}_")
        # Indent each line with > for blockquote
        text = e["text"][:1500]  # cap for readability
        for ln in text.split("\n"):
            lines.append(f"> {ln}" if ln else ">")
        lines.append("")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--who", help="Telegram sender_id to dump (default: list all)")
    ap.add_argument("--since", help="Filter events to date YYYY-MM-DD or later")
    ap.add_argument("--save", action="store_true",
                    help="Write per-user .md files to data/memory/team_conversations/")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    since_d = None
    if args.since:
        try:
            since_d = datetime.strptime(args.since, "%Y-%m-%d").date()
        except ValueError:
            print(f"Bad --since format. Expected YYYY-MM-DD, got {args.since!r}")
            return 1

    by_sender = walk_sessions(since_date=since_d)

    if not by_sender:
        print("No team chats found.")
        return 0

    # Roster summary (always show)
    if not args.quiet:
        print(f"=== CCF team Carol chat audit ===")
        print(f"({len(by_sender)} sender(s) found"
              + (f" since {since_d}" if since_d else "") + ")\n")
        for sid in sorted(by_sender.keys()):
            s = summarize_user(sid, by_sender[sid])
            tag = "OWNER" if sid == "" else ""
            print(f"  {tag:5} {s['name']:15} (ID {sid})  · "
                  f"{s['user_messages_unique']:3} unique msg  ·  "
                  f"last {s['last_seen']}")
        print()

    # If --who, dump that one
    if args.who:
        if args.who not in by_sender:
            print(f"No chats from sender_id {args.who}")
            return 1
        print(render_markdown(args.who, by_sender[args.who]))

    # If --save, write per-user .md files
    if args.save:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        for sid, events in by_sender.items():
            name = KNOWN.get(sid, (f"user_{sid}", ""))[0].lower().replace(" ", "_")
            today = date.today().isoformat()
            out = OUT_DIR / f"{name}_{today}.md"
            out.write_text(render_markdown(sid, events), encoding="utf-8")
            if not args.quiet:
                print(f"  saved → {out.relative_to(ROOT)}  ({len(events)} events)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
