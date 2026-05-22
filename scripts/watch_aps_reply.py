#!/usr/bin/env python3
"""
watch_aps_reply.py - Detect reply from Autodesk Platform Services about the
BC API access request. Telegram-pings the owner the moment it arrives.

Daemon runs this every ~17 min. State is tracked so we only alert once per
new reply.

Stop condition: when bc_auth.json has both client_id AND client_secret filled
in, this watcher self-disables (no more pings about an issue that's resolved).
"""
from __future__ import annotations
import imaplib
import email as email_lib
import json
import os
import sys
from datetime import datetime
from email.header import decode_header
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / "data" / "memory" / "watch_aps_state.json"
BC_AUTH = ROOT / "data" / "config" / "bc_auth.json"
LOG_FILE = ROOT / "data" / "logs" / "watch_aps_reply.log"

GMAIL_USER = os.environ.get("GMAIL_USER", "estimates@carolinacommercialfinishes.com")
GMAIL_PASS = os.environ.get("GMAIL_APP_PASSWORD", "")

# Senders / patterns that count as "API access reply".
# We MUST be strict here — `team@buildingconnected.com` sends bid invitations
# constantly, so a loose filter pings on every GC's invite. Real replies to
# our API request will be ABOUT the API request (subject mentions API, credentials,
# OAuth, developer, etc.) or come from Autodesk APS developer / support addresses.
API_DOMAINS = (
    "apsdevelopers@autodesk.com",
    "developer@autodesk.com",
    "support@autodesk.com",
    "aps-support@autodesk.com",
    "@autodesk.com",  # broader autodesk - but combined with strict subject filter
)
API_SUBJECT_REQUIRED = (
    "api",
    "credentials",
    "client_id",
    "client id",
    "client secret",
    "oauth",
    "developer",
    "access request",
    "platform services",
    "aps",
)
# Never alert on these (BC's automated stuff)
NEVER_ALERT_PATTERNS = (
    "bid invite",
    "bid invitation",
    "invitation to bid",
    "bid submission",
    "addendum",
    "rfi",
    "request for information",
    "your message wasn't delivered",
    "automatic reply",  # auto-responder — uninteresting unless it confirms
)


def decode_h(value):
    if not value: return ""
    out = ""
    for p, e in decode_header(value):
        out += p.decode(e or "utf-8", errors="replace") if isinstance(p, bytes) else p
    return out


def load_state():
    if STATE_FILE.exists():
        try: return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception: pass
    return {"seen_ids": [], "request_sent_at": "2026-05-12T11:00:00"}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def is_request_resolved():
    """If BC auth file has both client_id + client_secret filled in,
    the request is done — watcher should self-disable."""
    try:
        auth = json.loads(BC_AUTH.read_text(encoding="utf-8"))
        return bool(auth.get("client_id")) and bool(auth.get("client_secret"))
    except Exception:
        return False


def log(msg):
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now().isoformat(timespec='seconds')}  {msg}\n")


def telegram_alert(text):
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        from _lib import telegram
        return telegram.send(text, chat_id="")
    except Exception as e:
        log(f"telegram send failed: {e}")
        return False


def main():
    if is_request_resolved():
        log("BC API credentials already in bc_auth.json — watcher disabled")
        return 0

    state = load_state()
    seen = set(state.get("seen_ids", []))

    try:
        M = imaplib.IMAP4_SSL("imap.gmail.com")
        M.login(GMAIL_USER, GMAIL_PASS)
        M.select("INBOX")
    except Exception as e:
        log(f"IMAP login failed: {e}")
        return 1

    # Search since the request was sent
    sent_dt = datetime.fromisoformat(state.get("request_sent_at", "2026-05-12T11:00:00"))
    since = sent_dt.strftime("%d-%b-%Y")
    new_alerts = 0
    seen_in_this_run = []

    for domain in API_DOMAINS:
        st, ids = M.search(None, f'(SINCE "{since}" FROM "{domain}")')
        if st != "OK" or not ids[0]: continue
        for mid in ids[0].split():
            msg_id_key = mid.decode() if isinstance(mid, bytes) else str(mid)
            if msg_id_key in seen: continue
            seen_in_this_run.append(msg_id_key)
            st, data = M.fetch(mid, '(BODY.PEEK[HEADER])')
            if st != "OK": continue
            msg = email_lib.message_from_bytes(data[0][1])
            fr = decode_h(msg.get("From", ""))
            subj = decode_h(msg.get("Subject", ""))

            # Hard-skip: BC's automated bid invites, addenda, RFIs, etc.
            subj_lower = subj.lower()
            fr_lower = fr.lower()
            if any(p in subj_lower for p in NEVER_ALERT_PATTERNS):
                continue
            # Hard-skip: BC's team@ alias forwarding GC submissions (most common false positive)
            if "team@buildingconnected" in fr_lower:
                continue
            if "noreply" in fr_lower or "no-reply" in fr_lower:
                continue

            # Required: subject must contain an API-related keyword
            if not any(k in subj_lower for k in API_SUBJECT_REQUIRED):
                continue

            body = (
                f"📬 *APS/BC reply on the API access request*\n"
                f"From: {fr[:80]}\n"
                f"Subj: {subj[:120]}\n"
                f"Open inbox to read full reply."
            )
            if telegram_alert(body):
                log(f"alerted: {fr[:60]} | {subj[:80]}")
                new_alerts += 1
            else:
                log(f"alert FAILED: {fr[:60]} | {subj[:80]}")
    M.logout()

    state["seen_ids"] = list(seen | set(seen_in_this_run))
    save_state(state)

    if new_alerts == 0:
        log(f"no new APS/BC replies (state has {len(state['seen_ids'])} seen)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
