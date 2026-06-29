#!/usr/bin/env python3
r"""
team_chat_watcher.py — Real-time Telegram alert when teammates message Carol.

Polls the OpenClaw session files every cycle and sends the owner a Telegram ping
the moment a non-owner sender writes to the Telegram bot. Tracks per-message IDs
in state so each message is reported exactly once.

The owner (USER_TELEGRAM_CHAT_ID) is the recipient of the alerts. We do NOT
alert on the owner's own Telegram messages — only on teammates (or any new
sender).

Run:
  python scripts/team_chat_watcher.py            # one-pass check
  python scripts/team_chat_watcher.py --quiet    # for daemon

Daemon schedules this every ~3 minutes for near-real-time delivery.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SESSIONS_DIR = Path(r"C:\Users\Nursm\.openclaw\agents\main\sessions")
STATE_FILE   = ROOT / "data" / "memory" / "team_chat_watcher_state.json"
LOG_FILE     = ROOT / "data" / "logs" / "team_chat_watcher.log"

# Owner — receives alerts, but their own messages don't trigger alerts.
# Loaded from env so the value isn't hardcoded for git history.
OWNER_ID = os.environ.get("USER_TELEGRAM_CHAT_ID", "")

# Known teammates → friendly names for the alert message.
# Sourced from env TEAM_TELEGRAM_IDS ("id:Name,id:Name") so no ids/names ship
# as literals; the owner id maps to a "(you)" label.
KNOWN_NAMES = {OWNER_ID: "Owner (you)"} if OWNER_ID else {}
for _pair in os.environ.get("TEAM_TELEGRAM_IDS", "").split(","):
    _pair = _pair.strip()
    if ":" in _pair:
        _tid, _tname = _pair.split(":", 1)
        _tid, _tname = _tid.strip(), _tname.strip()
        if _tid:
            KNOWN_NAMES[_tid] = _tname or f"User {_tid}"

SKIP_TOKENS = ("stale_", "reset.", "backup.", "heartbeat_bug",
               "no_revenue", "count_hallucination", "team_roles", "brief_loop")


def _is_active(filename: str) -> bool:
    return not any(t in filename for t in SKIP_TOKENS)


def _strip_carol_xml(text: str) -> str:
    text = re.sub(r"</?final>", "", text)
    text = re.sub(r"</?response>", "", text)
    return text.strip()


def _extract_message_body(content) -> str:
    """Pull just the user-typed text out of Telegram-wrapped content."""
    if isinstance(content, list):
        content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
    s = str(content)
    # Strip Telegram metadata block (everything before the last ``` -block boundary)
    parts = re.split(r"```\s*\n\s*\n", s)
    if len(parts) > 1:
        return parts[-1].strip()
    m = re.search(r"```\s*\n\s*\n(.+)\Z", s, re.DOTALL)
    return m.group(1).strip() if m else s.strip()


def _extract_sender_id(content) -> str | None:
    s = str(content) if not isinstance(content, str) else content
    m = re.search(r'"(?:sender_)?id":\s*"(\d{6,})"', s)
    return m.group(1) if m else None


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"seen_ids": []}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def alert(sender_id: str, sender_name: str, msg_text: str, ts: str,
          carol_reply: str | None = None) -> bool:
    """Send the Telegram alert. Returns True on success."""
    try:
        from _lib import telegram
    except Exception:
        return False
    # Truncate long messages for the alert
    msg = msg_text[:300] + ("…" if len(msg_text) > 300 else "")
    body = [
        f"💬 *Carol got a new message*",
        f"From: *{sender_name}* (ID `{sender_id}`)",
        f"At: {ts[:19]}",
        "",
        f"_{msg}_" if msg else "_(empty)_",
    ]
    if carol_reply:
        cr = carol_reply[:300] + ("…" if len(carol_reply) > 300 else "")
        body.append("")
        body.append(f"*Carol replied:* _{cr}_")
    return telegram.send("\n".join(body), chat_id=OWNER_ID)


def log(msg: str, quiet: bool = False):
    if not quiet:
        print(msg)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now().isoformat(timespec='seconds')}  {msg}\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--include-owner", action="store_true",
                    help="Also alert on Nursultan's own messages (testing only)")
    args = ap.parse_args()

    state = load_state()
    seen = set(state.get("seen_ids", []))

    if not SESSIONS_DIR.exists():
        log(f"[watcher] Sessions dir missing: {SESSIONS_DIR}", args.quiet)
        return 0

    new_alerts = 0
    new_seen = []
    for sf in glob.glob(str(SESSIONS_DIR / "*.jsonl")):
        if not _is_active(os.path.basename(sf)):
            continue
        # Iterate events, group user msg → next assistant reply
        try:
            with open(sf, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except Exception:
            continue
        current_sender = None
        for i, line in enumerate(lines):
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
            ts = rec.get("timestamp", "") or ""
            if role == "user":
                sid = _extract_sender_id(content)
                if sid:
                    current_sender = sid
                    # Skip owner messages unless --include-owner
                    if sid == OWNER_ID and not args.include_owner:
                        continue
                    # Build a unique event ID (session_file + line_index)
                    event_id = f"{os.path.basename(sf)}#{i}"
                    if event_id in seen:
                        continue
                    seen.add(event_id)
                    new_seen.append(event_id)
                    body = _extract_message_body(content)
                    # Skip Telegram bootstrap "/start" wrapper noise if body is empty
                    if not body or body.strip().lower() in ("/start", ""):
                        continue
                    # Find the assistant reply that follows (if any)
                    reply = None
                    for j in range(i + 1, min(i + 6, len(lines))):
                        try:
                            r2 = json.loads(lines[j])
                            if (r2.get("type") == "message"
                                    and r2.get("message", {}).get("role") == "assistant"):
                                rc = r2["message"].get("content", "")
                                if isinstance(rc, list):
                                    rc = " ".join(c.get("text", "") for c in rc if isinstance(c, dict))
                                reply = _strip_carol_xml(str(rc))
                                if reply.startswith("✅ New session"):
                                    reply = None
                                    continue
                                break
                        except Exception:
                            continue
                    name = KNOWN_NAMES.get(sid, f"Unknown user (ID {sid})")
                    ok = alert(sid, name, body, ts, carol_reply=reply)
                    if ok:
                        new_alerts += 1
                        log(f"[watcher] alerted: {name} — {body[:60]}", args.quiet)
                    else:
                        log(f"[watcher] alert FAILED: {name}", args.quiet)

    state["seen_ids"] = list(seen)
    save_state(state)

    if new_alerts == 0:
        log(f"[watcher] no new teammate messages (state has {len(seen)} seen events)", args.quiet)
    else:
        log(f"[watcher] sent {new_alerts} alert(s), state has {len(seen)} seen events", args.quiet)
    return 0


if __name__ == "__main__":
    sys.exit(main())
