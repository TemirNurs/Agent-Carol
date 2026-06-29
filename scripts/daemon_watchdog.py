#!/usr/bin/env python3
r"""
daemon_watchdog.py — Detect silently-dead daemon and Telegram-ping the user.

Background: 5/4/2026 the carol_daemon process died at 08:44 AM. CRM updates
stopped. The user noticed only after 7+ hours when invitation classifications
weren't happening. This watchdog is the safety net for that failure mode.

How it works:
  1. Reads data/health/daemon.heartbeat (written every minute by the daemon)
  2. If the heartbeat is older than STALE_AFTER_MIN minutes → Telegram ping
  3. Throttles alerts to once per hour so we don't spam if daemon stays dead

Run:
  python scripts/daemon_watchdog.py            # check + alert if stale
  python scripts/daemon_watchdog.py --quiet
  python scripts/daemon_watchdog.py --status   # show heartbeat info, no alert
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

HEARTBEAT_FILE = ROOT / "data" / "health" / "daemon.heartbeat"
STATE_FILE     = ROOT / "data" / "health" / "watchdog_state.json"
LOG_FILE       = ROOT / "data" / "logs" / "daemon_watchdog.log"

STALE_AFTER_MIN = 15        # heartbeat older than this = daemon dead
ALERT_COOLDOWN_MIN = 60     # don't re-alert within this window


def _now() -> datetime:
    return datetime.now()


def read_heartbeat() -> dict | None:
    if not HEARTBEAT_FILE.exists():
        return None
    try:
        data = json.loads(HEARTBEAT_FILE.read_text(encoding="utf-8"))
        data["_mtime"] = datetime.fromtimestamp(HEARTBEAT_FILE.stat().st_mtime)
        return data
    except Exception:
        return None


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def alert_telegram(msg: str):
    try:
        from _lib import telegram
        telegram.send(msg)
    except Exception:
        pass


def log(msg: str, quiet: bool):
    if not quiet:
        print(msg)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"{_now().isoformat(timespec='seconds')}  {msg}\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--status", action="store_true",
                    help="Show heartbeat info without alerting")
    args = ap.parse_args()

    hb = read_heartbeat()
    state = load_state()

    if hb is None:
        msg = "Daemon heartbeat file missing. Daemon never started or never wrote."
        log(f"[watchdog] {msg}", args.quiet)
        if args.status:
            return 1
        # Throttle
        last_alert = state.get("last_alert_at")
        if last_alert:
            last = datetime.fromisoformat(last_alert)
            if _now() - last < timedelta(minutes=ALERT_COOLDOWN_MIN):
                log("[watchdog] alert throttled (within cooldown)", args.quiet)
                return 1
        alert_telegram(
            "🚨 *Carol daemon DOWN*\n\n"
            "No heartbeat found at `data/health/daemon.heartbeat`. "
            "Daemon may have crashed or never started. "
            "On HOMESERVER run: `python carol_daemon.py`"
        )
        state["last_alert_at"] = _now().isoformat()
        save_state(state)
        return 1

    age = _now() - hb["_mtime"]
    age_min = age.total_seconds() / 60.0
    info = f"heartbeat age={age_min:.1f}min  pid={hb.get('pid','?')}  active_task={hb.get('active_task','?')}"

    if args.status:
        log(f"[watchdog] {info}", args.quiet)
        return 0 if age_min < STALE_AFTER_MIN else 1

    if age_min < STALE_AFTER_MIN:
        log(f"[watchdog] OK — {info}", args.quiet)
        # Reset alert state so next death triggers a fresh ping
        if state.get("last_alert_at"):
            state.pop("last_alert_at", None)
            save_state(state)
        return 0

    # Stale — alert (with throttle)
    log(f"[watchdog] STALE — {info} (threshold {STALE_AFTER_MIN}min)", args.quiet)
    last_alert = state.get("last_alert_at")
    if last_alert:
        last = datetime.fromisoformat(last_alert)
        if _now() - last < timedelta(minutes=ALERT_COOLDOWN_MIN):
            log("[watchdog] alert throttled (within cooldown)", args.quiet)
            return 1
    alert_telegram(
        f"🚨 *Carol daemon STALE*\n\n"
        f"Last heartbeat: {age_min:.0f}min ago (threshold {STALE_AFTER_MIN}min)\n"
        f"Last active task: `{hb.get('active_task','?')}`\n"
        f"PID: {hb.get('pid','?')}\n\n"
        f"Daemon likely crashed. On HOMESERVER:\n"
        f"`python carol_daemon.py`"
    )
    state["last_alert_at"] = _now().isoformat()
    save_state(state)
    return 1


if __name__ == "__main__":
    sys.exit(main())
