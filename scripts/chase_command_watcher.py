#!/usr/bin/env python3
r"""
chase_command_watcher.py — act on the OWNER's Telegram chase commands.

The 7AM brief says "reply 'approve'". This watcher reads Nursultan's Telegram
messages (OpenClaw session files), detects a chase command, and ACTS on it so
the approval gate (god-level rebuild 6/16) can be cleared without Carol-the-agent
in the loop:

  • "send chases" / "send all" / "approve chases" / "go ahead" / "fire"
        → issue a ONE-TIME random code and reply it to the owner. This does NOT
          send. (6/30 safeguard: a casual word can only ever PRODUCE a code.)
  • the one-time code itself (e.g. "SEND-9F3A")
        → fire the batch: launch chase_executor --confirm-token <code>, the ONLY
          path that bypasses the draft-only lock. Single-use, expires in 15 min.
  • "pause chases" / "stop chases" / "don't chase"
        → add today to pause_dates AND cancel any live send-code (executor aborts).
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
import secrets
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
# A SEND REQUEST (chase-scoped send/fire/approve, or a bare standalone) does NOT
# send — it makes Carol issue a ONE-TIME CODE; the owner must reply that exact code
# to fire. So over-matching here is safe (worst case = an extra code the user
# ignores). "approve the estimate/proposal" needs a chase word, so it won't match.
_SEND_REQUEST = re.compile(
    r"\b(send|fire|approve[ds]?|go ?ahead|release|shoot|blast)\b"
    r"[^.]{0,30}(\bchas\w*|\bfollow[- ]?ups?|\bthem\b|\ball\b|\bthese\b|\bthose\b|\btoday\b|\b\d+\b)|"
    r"\bchas\w*[^.]{0,20}\b(send|fire|approve|go ?ahead|now|away)\b", re.I)
_SEND_REQUEST_BARE = re.compile(
    r"^\s*(yes[,!\s]*)?(approve[ds]?|send|fire|go ahead|do it|"
    r"send (them|all|the chases?|today'?s chases?))\s*[.!]*\s*$", re.I)


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


def _today() -> str:
    return datetime.now().date().isoformat()


SEND_PENDING = ROOT / "data" / "memory" / "chase_send_pending.json"
PLAN_DIR = ROOT / "data" / "proposed_chases"
CODE_TTL_MIN = 15


def _gen_code() -> str:
    return "SEND-" + secrets.token_hex(2).upper()   # e.g. SEND-9F3A (random, one-time)


def _active_pending():
    """The live unused / un-expired / today send-code, else None."""
    p = _load(SEND_PENDING, None)
    if not isinstance(p, dict) or p.get("used") or p.get("date") != _today():
        return None
    exp = p.get("expires_at")
    try:
        if exp and datetime.fromisoformat(exp) < datetime.now():
            return None
    except Exception:
        pass
    return p


def _plan_count() -> int:
    pf = PLAN_DIR / f"proposed_chases_{_today()}.json"
    plan = _load(pf, {})
    return len(plan.get("proposed", []) if isinstance(plan, dict) else [])


def _classify(text: str) -> str | None:
    t = text.strip()
    if not t or len(t) > 200:
        return None
    # An EXACT match to the live one-time code = the send confirm (check first).
    pend = _active_pending()
    if pend and t.upper() == str(pend.get("code", "")).strip().upper():
        return "send_confirm"
    if _RESUME.search(t):
        return "resume"
    if _PAUSE.search(t):
        return "pause"
    if _NEG.search(t):
        return None
    if _SEND_REQUEST.search(t) or _SEND_REQUEST_BARE.match(t):
        return "send_request"
    return None


def do_send_request(dry: bool):
    """Owner asked to send — issue a ONE-TIME code and DO NOT send. The owner must
    reply that exact code to fire (chase_executor consumes it via --confirm-token).
    A casual word produces only a code, never a send — that's the 6/30 safeguard."""
    n = _plan_count()
    if n == 0:
        tg("No chases are eligible right now — nothing to send. (See the chase report.)")
        return "send-request: nothing eligible"
    code = _gen_code()
    pend = {"code": code, "date": _today(), "used": False,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "expires_at": (datetime.now() + timedelta(minutes=CODE_TTL_MIN)).isoformat(timespec="seconds"),
            "batch_count": n}
    if not dry:
        _save(SEND_PENDING, pend)
    tg(f"📤 Ready to send *{n}* chase(s) now — ~10-min pace, every guard on "
       f"(reply-aware · business hours · stops by 4 PM ET).\n\n"
       f"⚠️ To FIRE, reply EXACTLY:  *{code}*\n"
       f"_(one-time · expires in {CODE_TTL_MIN} min · any other message sends nothing)_")
    return f"send-request: issued code for {n} bids"


def do_send_confirm(dry: bool):
    """Owner replied the valid code — fire the batch. Lift today's emergency pause so
    the executor's loop starts clean, then launch it with the one-time token."""
    pend = _active_pending()
    if not pend:
        tg("That code expired or was already used. Text *send chases* for a fresh one.")
        return "send-confirm: no active code"
    code, n = pend.get("code", ""), pend.get("batch_count", "?")
    if not dry:
        cfg = _load(AUTOPILOT, {})
        cfg["pause_dates"] = [d for d in (cfg.get("pause_dates") or []) if d != _today()]
        _save(AUTOPILOT, cfg)
        try:
            subprocess.Popen([sys.executable, str(EXECUTOR), "--confirm-token", code],
                             cwd=str(ROOT),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            log(f"  executor launch failed: {e}")
            tg("⚠️ Couldn't launch the sender — try again, or send from the workstation.")
            return "send-confirm: launch failed"
    tg(f"✅ *Confirmed.* Sending {n} chase(s) now at ~10-min pace — I'll report each as "
       f"it goes. Reply *pause chases* to stop mid-batch.")
    return f"send-confirm: fired {n} bids via one-time code"


def do_pause(dry: bool):
    cfg = _load(AUTOPILOT, {})
    pause = set(cfg.get("pause_dates") or [])
    pause.add(_today())
    cfg["pause_dates"] = sorted(pause)
    cfg["approved_dates"] = [d for d in (cfg.get("approved_dates") or []) if d != _today()]
    # cancel any live send code so a queued confirm can't fire
    pend = _load(SEND_PENDING, None)
    if isinstance(pend, dict) and not pend.get("used"):
        pend["used"] = True
        if not dry:
            _save(SEND_PENDING, pend)
    if not dry:
        _save(AUTOPILOT, cfg)
    tg("⏸ *Chases paused.* Nothing sends (any pending send-code is cancelled). "
       "Reply 'resume chases' to lift.")
    return "paused today + cancelled pending code"


def do_resume(dry: bool):
    cfg = _load(AUTOPILOT, {})
    cfg["pause_dates"] = [d for d in (cfg.get("pause_dates") or []) if d != _today()]
    if not dry:
        _save(AUTOPILOT, cfg)
    tg("▶ *Chase pause lifted.* To send, text *send chases* and reply the one-time code.")
    return "resumed (pause lifted)"


ACTIONS = {"send_request": do_send_request, "send_confirm": do_send_confirm,
           "pause": do_pause, "resume": do_resume}


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
