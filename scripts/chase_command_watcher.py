#!/usr/bin/env python3
r"""
chase_command_watcher.py — act on the OWNER's Telegram chase commands.

The 7AM brief says "reply 'approve'". This watcher reads Nursultan's Telegram
messages (OpenClaw session files), detects a chase command, and ACTS on it so
the approval gate (god-level rebuild 6/16) can be cleared without Carol-the-agent
in the loop:

  • "approve" / "approve chases" / "send the chases" / "go ahead"
        → add today to chase_autopilot.approved_dates AND launch chase_executor
          (sends now if in business hours; the executor re-checks every guard).
  • "pause chases" / "stop chases" / "hold the chases" / "don't chase"
        → add today to pause_dates (executor aborts).
  • "resume chases" / "unpause"
        → remove today from pause_dates.

SAFETY: only the OWNER's messages count; only commands in the last
RECENT_MIN minutes act (so a stale "approve" from days ago is never replayed);
each message acts at most once (deduped via state). One-pass; daemon runs it
every ~2 min.

Run:
  python scripts/chase_command_watcher.py [--quiet] [--dry-run]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SESSIONS_DIR = Path(r"C:\Users\Nursm\.openclaw\agents\main\sessions")
STATE_FILE = ROOT / "data" / "memory" / "chase_command_watcher_state.json"
LOG_FILE = ROOT / "data" / "logs" / "chase_command_watcher.log"
AUTOPILOT = ROOT / "data" / "config" / "chase_autopilot.json"
EXECUTOR = ROOT / "scripts" / "chase_executor.py"
OWNER_ID = os.environ.get("USER_TELEGRAM_CHAT_ID", "")
RECENT_MIN = 25  # only act on commands sent within this many minutes

SKIP_TOKENS = ("stale_", "reset.", "backup.", "heartbeat_bug",
               "no_revenue", "count_hallucination", "team_roles", "brief_loop")

# ── command intents ───────────────────────────────────────────────────────
# Negation guard so "don't approve" / "do not send" never approves.
_NEG = re.compile(r"\b(don'?t|do not|never|hold off|cancel|not yet|wait)\b", re.I)
_PAUSE = re.compile(
    r"\b(pause|stop|hold|halt|skip)\b[^.]*\bchas|\bdon'?t (chase|send)\b|"
    r"\bno chas|\bhold (the )?chases?\b|\bpause chases?\b", re.I)
_RESUME = re.compile(r"\b(resume|unpause|un-pause|restart|continue)\b[^.]*\bchas|\bunpause\b", re.I)
# Approve must be chase-scoped OR a bare standalone approval — so "approve the
# estimate" / "approve the proposal" does NOT fire chases.
_APPROVE_CHASE = re.compile(
    r"(approve|approved|send|go ahead|fire|release)[^.]{0,25}\bchas|"
    r"\bchas[^.]{0,25}(approve|send|go ahead|away|now)", re.I)
_APPROVE_BARE = re.compile(
    r"^\s*(yes[,!\s]*)?(approve[ds]?|send (the |today'?s )?chases?|go ahead|"
    r"approved? (them|all|today))\s*[.!]*\s*$", re.I)


def log(msg: str, quiet: bool = False):
    if not quiet:
        print(msg)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now().isoformat(timespec='seconds')}  {msg}\n")


def tg(text: str):
    try:
        from _lib import telegram
        telegram.send(text, chat_id=OWNER_ID)
    except Exception:
        pass


def _is_active(fn: str) -> bool:
    return not any(t in fn for t in SKIP_TOKENS)


def _extract_body(content) -> str:
    if isinstance(content, list):
        content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
    s = str(content)
    parts = re.split(r"```\s*\n\s*\n", s)
    if len(parts) > 1:
        return parts[-1].strip()
    m = re.search(r"```\s*\n\s*\n(.+)\Z", s, re.DOTALL)
    return m.group(1).strip() if m else s.strip()


def _sender_id(content) -> str | None:
    m = re.search(r'"(?:sender_)?id":\s*"(\d{6,})"', str(content))
    return m.group(1) if m else None


def _load(p, default):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return default


def _save(p, obj):
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(p) + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    tmp.replace(p)


def _classify(text: str) -> str | None:
    t = text.strip()
    if not t or len(t) > 200:
        return None
    if _RESUME.search(t):
        return "resume"
    if _PAUSE.search(t):
        return "pause"
    if _NEG.search(t):
        return None
    if _APPROVE_CHASE.search(t) or _APPROVE_BARE.match(t):
        return "approve"
    return None


def _today() -> str:
    return datetime.now().date().isoformat()


def do_approve(dry: bool):
    cfg = _load(AUTOPILOT, {})
    appr = set(cfg.get("approved_dates") or [])
    appr.add(_today())
    cfg["approved_dates"] = sorted(d for d in appr if d >= _today())
    cfg.setdefault("approval_required", True)
    if not dry:
        _save(AUTOPILOT, cfg)
        # launch the (guarded) executor — sends now if in business hours
        try:
            subprocess.Popen([sys.executable, str(EXECUTOR)],
                             cwd=str(ROOT),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            log(f"  executor launch failed: {e}")
    tg("✅ *Chases approved for today.* Sending now (the executor re-checks every "
       "guard — reply-aware, tiered, business-hours). Reply 'pause chases' to stop.")
    return "approved + executor launched"


def do_pause(dry: bool):
    cfg = _load(AUTOPILOT, {})
    pause = set(cfg.get("pause_dates") or [])
    pause.add(_today())
    cfg["pause_dates"] = sorted(pause)
    # un-approve too, so an earlier approval can't override the pause
    cfg["approved_dates"] = [d for d in (cfg.get("approved_dates") or []) if d != _today()]
    if not dry:
        _save(AUTOPILOT, cfg)
    tg("⏸ *Chases paused for today.* Nothing will send. Reply 'resume chases' to lift.")
    return "paused today"


def do_resume(dry: bool):
    cfg = _load(AUTOPILOT, {})
    cfg["pause_dates"] = [d for d in (cfg.get("pause_dates") or []) if d != _today()]
    if not dry:
        _save(AUTOPILOT, cfg)
    tg("▶ *Chase pause lifted.* Reply 'approve' to authorize today's batch.")
    return "resumed (pause lifted; still needs approval)"


ACTIONS = {"approve": do_approve, "pause": do_pause, "resume": do_resume}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not SESSIONS_DIR.exists():
        log(f"[cmd] sessions dir missing: {SESSIONS_DIR}", args.quiet)
        return 0

    state = _load(STATE_FILE, {"seen": []})
    seen = set(state.get("seen", []))
    cutoff = datetime.now() - timedelta(minutes=RECENT_MIN)
    acted = 0
    newest_cmd = None  # only act on the single most-recent fresh command

    candidates = []  # (ts, event_id, intent, body)
    for sf in glob.glob(str(SESSIONS_DIR / "*.jsonl")):
        if not _is_active(os.path.basename(sf)):
            continue
        try:
            lines = open(sf, "r", encoding="utf-8", errors="replace").readlines()
        except Exception:
            continue
        for i, line in enumerate(lines):
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("type") != "message":
                continue
            m = rec.get("message", {})
            if m.get("role") != "user":
                continue
            content = m.get("content", "")
            if isinstance(content, list):
                content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
            content = str(content)
            if _sender_id(content) != OWNER_ID:
                continue
            event_id = f"{os.path.basename(sf)}#{i}"
            if event_id in seen:
                continue
            # recency gate
            ts = (rec.get("timestamp") or "")[:19]
            try:
                when = datetime.fromisoformat(ts)
            except Exception:
                when = None
            if when is None or when < cutoff:
                seen.add(event_id)   # mark old/unparseable as seen, never act
                continue
            body = _extract_body(content)
            intent = _classify(body)
            seen.add(event_id)       # acted-or-not, see each message once
            if intent:
                candidates.append((ts, event_id, intent, body))

    # Act on only the MOST RECENT fresh command (avoid approve+pause whiplash)
    if candidates:
        candidates.sort()
        ts, eid, intent, body = candidates[-1]
        result = ACTIONS[intent](args.dry_run)
        acted += 1
        log(f"[cmd] {'DRY ' if args.dry_run else ''}{intent.upper()} from owner "
            f"({ts}) -> {result}  «{body[:50]}»", args.quiet)

    state["seen"] = list(seen)[-2000:]
    if not args.dry_run:
        _save(STATE_FILE, state)
    if not acted:
        log(f"[cmd] no fresh chase command (seen {len(seen)} events)", args.quiet)
    return 0


if __name__ == "__main__":
    sys.exit(main())
