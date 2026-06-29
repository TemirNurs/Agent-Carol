#!/usr/bin/env python3
r"""
_hallucination_sentry.py — Passive lie detector for Carol.

Watches Carol's recent Telegram replies (saved in
data/memory/team_conversations/<user>_<date>.md) and verifies specific
factual claims against ground truth files. Pings Nursultan if Carol lied.

Why PASSIVE (not active probing):
  - Doesn't spam Carol with synthetic test questions
  - Catches REAL lies told to the actual user
  - No dependency on OpenClaw API internals

Claims it verifies:
  1. "<teammate> [verb] today" / "earlier today" / "this morning"
     → check team_conversations/<user>_today.md last_seen
  2. "we sent N proposals today" / "N new proposal sends"
     → count entries in data/memory/activity_log_today.md proposal-sent section
  3. "BID-XXXX is Won/Lost/Awarded"
     → check CRM live status row by Internal ID

Runs hourly from daemon. Logs to data/logs/hallucination_sentry.log.
Telegram alerts go to USER_TELEGRAM_CHAT_ID (Nursultan).
"""
from __future__ import annotations
import argparse, json, os, re, sys, urllib.parse, urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

# 5/30 fix — load .env so GMAIL_APP_PASSWORD / API keys are present when
# Carol (OpenClaw/Telegram) shells out to this script. A shelled child does
# NOT inherit the daemon's env, so without this the credential reads below
# return '' and the script fails (e.g. IMAP login). Absolute path → cwd-safe.
try:
    from pathlib import Path as _CCF_P
    from dotenv import load_dotenv as _ccf_load_dotenv
    _ccf_load_dotenv(_CCF_P(__file__).resolve().parent.parent / ".env")
except Exception:
    pass

try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

ROOT = Path(__file__).resolve().parent.parent
TRANS_DIR = ROOT / "data" / "memory" / "team_conversations"
ACTIVITY_DIR = ROOT / "data" / "memory"
LOG = ROOT / "data" / "logs" / "hallucination_sentry.log"
STATE = ROOT / "data" / "memory" / "sentry_state.json"


# ─────────────────────────────────────────────────────────────────────────
# Truth probes
# ─────────────────────────────────────────────────────────────────────────
def truth_team_last_seen(user: str) -> str:
    """Most recent date this teammate's transcript ends with."""
    files = sorted(TRANS_DIR.glob(f"{user.lower()}_*.md"))
    if not files:
        return ""
    last_text = files[-1].read_text(encoding="utf-8", errors="replace")
    m = re.search(r"Last seen:\s*([0-9]{4}-[0-9]{2}-[0-9]{2})", last_text)
    return m.group(1) if m else ""


def truth_sends_today() -> int:
    """How many distinct proposals went out today per activity_log."""
    f = ACTIVITY_DIR / f"activity_log_{date.today().isoformat()}.md"
    if not f.exists():
        f = ACTIVITY_DIR / "activity_log_today.md"
    if not f.exists():
        return 0
    text = f.read_text(encoding="utf-8", errors="replace")
    # Count lines under "Proposal sent" section, dedup by (project, recipient)
    sends = set()
    for m in re.finditer(
        r"`\d{2}:\d{2}`\s+(.+?)\s+→\s+([^\s]+?)\s+—", text
    ):
        sends.add((m.group(1).strip().lower()[:50], m.group(2).strip().lower()))
    return len(sends)


def truth_bid_status(bid_or_iid: str) -> str:
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        from crm_lib import all_records
        rows = all_records("Bid Log")
        for r in rows:
            if (r.get("Internal ID") == bid_or_iid
                or r.get("Bid #") == bid_or_iid):
                return r.get("Status", "")
        return "NOT FOUND"
    except Exception:
        return "?"


# ─────────────────────────────────────────────────────────────────────────
# Claim extraction from Carol's replies
# ─────────────────────────────────────────────────────────────────────────
_TEAM_NAMES = [n.strip() for n in os.environ.get("TEAM_FIRST_NAMES", "").split(",") if n.strip()]
_TEAM_ALT = "|".join(re.escape(n) for n in _TEAM_NAMES) or r"(?!x)x"  # match nothing if unset
CLAIM_TEAM_TODAY = re.compile(
    rf"(?P<user>{_TEAM_ALT})\b[^.\n]{{0,40}}\b"
    r"(?:interacted|talked|messaged|chatted|asked|started a (?:new )?session)"
    r"[^.\n]{0,40}\b(?:today|earlier today|this morning|this afternoon|just now)",
    re.I,
)
CLAIM_SENDS_TODAY = re.compile(
    r"\b(?P<n>\d{1,3})\b\s+(?:new\s+)?proposal\s+sends?\s+(?:from|across|today)",
    re.I,
)
CLAIM_BID_STATUS = re.compile(
    r"\b(?P<bid>BID-\d{4})\b[^.\n]{0,80}?\b"
    r"(?P<status>Won|Lost|Awarded|Awaiting Decision|Bid Submitted|Withdrawn|No Bid)\b",
    re.I,
)
# FAKE PROGRESS / PROMISE-TO-NOTIFY-LATER — Carol has NO between-turn execution,
# so any "I'll let you know later" / "still working on it" / "bear with me" is a
# lie by design. (6/8 disaster: she stalled the owner 2.5h with fake progress on a
# doc-fetch, then claimed "I've now pulled them" — never produced anything.) She
# must deliver IN-TURN or hand the command — never promise to come back.
FAKE_PROGRESS = re.compile(
    r"\b(?:i'?ll|i will|i'?m going to|i am going to)\s+"
    r"(?:let you know|update you|notify you|get back to you|circle back|"
    r"follow up with you|ping you|report back|have (?:it|that|them|these|this) "
    r"(?:ready|done|for you))\b"
    r"|\blet you know\s+(?:the moment|as soon as|once|when|after)\b"
    r"|\b(?:i'?m|i am)\s+(?:still\s+)?(?:working on (?:it|this|those|them)|"
    r"processing|fetching|pulling|downloading|compiling|gathering|preparing)\b"
    r"|\bstill\s+(?:working|fetching|pulling|processing|downloading)\b"
    r"|\bbear with me\b|\bhang tight\b|\bgive me a (?:moment|minute|few)\b"
    r"|\b(?:this|it|that)\s+(?:might|may|will|could)\s+take\s+(?:a\s+)?"
    r"(?:few|couple|several|moment)\b",
    re.I,
)
# Claimed just-completed async work without showing it (the 6/8 "I've now pulled" lie).
CLAIM_DONE = re.compile(
    r"\bi'?ve\s+now\s+(?:pulled|fetched|downloaded|gathered|completed|compiled)\b",
    re.I,
)


def find_recent_carol_replies(hours_back: int = 6) -> list[tuple[str, str]]:
    """Return [(timestamp, text), ...] of Carol replies in the last N hours
    across all teammate transcripts (focus on Nursultan since that's where
    she chats with the owner)."""
    out = []
    cutoff = datetime.now() - timedelta(hours=hours_back)
    if not TRANS_DIR.exists():
        return out
    today = date.today().isoformat()
    yest  = (date.today() - timedelta(days=1)).isoformat()
    for f in TRANS_DIR.glob(f"*_{today}.md"):
        text = f.read_text(encoding="utf-8", errors="replace")
        # Parse blocks: "**🤖 Carol** _YYYY-MM-DDTHH:MM:SS_\n> ..."
        for m in re.finditer(
            r"\*\*🤖 Carol\*\*\s+_([0-9T:-]+)_\s*\n((?:>[^\n]*\n?)+)",
            text,
        ):
            ts_raw = m.group(1)
            try:
                ts = datetime.fromisoformat(ts_raw)
            except Exception:
                continue
            if ts < cutoff:
                continue
            body = "\n".join(line.lstrip("> ").rstrip()
                             for line in m.group(2).split("\n") if line.strip())
            out.append((ts_raw, body))
    # Also yesterday's snapshot may contain recent-evening replies
    for f in TRANS_DIR.glob(f"*_{yest}.md"):
        text = f.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(
            r"\*\*🤖 Carol\*\*\s+_([0-9T:-]+)_\s*\n((?:>[^\n]*\n?)+)",
            text,
        ):
            ts_raw = m.group(1)
            try: ts = datetime.fromisoformat(ts_raw)
            except Exception: continue
            if ts < cutoff: continue
            body = "\n".join(line.lstrip("> ").rstrip()
                             for line in m.group(2).split("\n") if line.strip())
            out.append((ts_raw, body))
    return out


# ─────────────────────────────────────────────────────────────────────────
# Verdict logic
# ─────────────────────────────────────────────────────────────────────────
def check_reply(ts: str, body: str) -> list[tuple[str, str]]:
    """Returns list of (probe_name, lie_detail) for any lies detected."""
    lies = []
    today_iso = date.today().isoformat()

    # 1. "<teammate> did X today"
    for m in CLAIM_TEAM_TODAY.finditer(body):
        user = m.group("user").lower()
        truth = truth_team_last_seen(user)
        if truth != today_iso:
            lies.append((
                "team-today",
                f"Carol claimed {m.group('user')} interacted today "
                f"('{m.group(0)[:80]}'); truth: last_seen={truth or 'never'}",
            ))

    # 2. "N proposals sent today"
    for m in CLAIM_SENDS_TODAY.finditer(body):
        n = int(m.group("n"))
        truth_n = truth_sends_today()
        # Allow ±2 tolerance for race conditions / different counting
        if abs(n - truth_n) > 2 and truth_n >= 0:
            lies.append((
                "sends-today",
                f"Carol claimed {n} proposal sends today; truth: {truth_n}",
            ))

    # 3. "BID-XXXX is <status>"
    for m in CLAIM_BID_STATUS.finditer(body):
        bid = m.group("bid")
        claimed = m.group("status")
        # Normalize "Awarded" → "Won" for comparison
        claimed_norm = "Won" if claimed.lower() == "awarded" else claimed
        truth_status = truth_bid_status(bid)
        if truth_status and claimed_norm.lower() != truth_status.lower():
            lies.append((
                "bid-status",
                f"Carol said {bid} is '{claimed}'; CRM says '{truth_status}'",
            ))

    # 4. Fake progress / promise-to-notify-later — ALWAYS a lie (no between-turn work)
    #    EXEMPTION: a REAL queued background fetch (enqueue_fetch.py → fetch_worker)
    #    actually delivers later, so "queued, the daemon will message you" is TRUE.
    queued_job = re.search(
        r"\bqueued\b|\benqueu|background (?:doc(?:ument)?-?\s*)?fetch|the daemon will",
        body, re.I)
    fp = FAKE_PROGRESS.search(body)
    if fp and not queued_job:
        lies.append((
            "fake-progress",
            f"Carol promised work/progress she CANNOT deliver (\"{fp.group(0)[:60]}\") "
            f"— she only acts within one turn. She must run it in-turn and return the "
            f"result, or queue it via enqueue_fetch.py, or hand you the exact command. "
            f"Tell her to do it now.",
        ))
    elif CLAIM_DONE.search(body) and not queued_job:
        lies.append((
            "claimed-done",
            f"Carol claimed she just finished async work (\"{CLAIM_DONE.search(body).group(0)[:50]}\") "
            f"— verify she actually showed the result this turn, not a stall.",
        ))

    return lies


# ─────────────────────────────────────────────────────────────────────────
# Telegram alert
# ─────────────────────────────────────────────────────────────────────────
def telegram_alert(msg: str):
    tok = os.environ.get("TELEGRAM_BOT_TOKEN",
                         "")
    chat = os.environ.get("USER_TELEGRAM_CHAT_ID", "")
    body = urllib.parse.urlencode({
        "chat_id": chat, "text": msg, "parse_mode": "Markdown"
    }).encode("utf-8")
    try:
        urllib.request.urlopen(
            urllib.request.Request(
                f"https://api.telegram.org/bot{tok}/sendMessage", data=body),
            timeout=10,
        )
    except Exception:
        pass


def load_state() -> dict:
    if STATE.exists():
        try: return json.loads(STATE.read_text(encoding="utf-8"))
        except Exception: pass
    return {"alerted_lies": []}


def save_state(s: dict):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(s, indent=2), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=6,
                    help="Look back this many hours of Carol replies")
    ap.add_argument("--dry-run", action="store_true",
                    help="Detect but don't send Telegram alert")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    state = load_state()
    alerted = set(state.get("alerted_lies", []))

    replies = find_recent_carol_replies(hours_back=args.hours)
    if not args.quiet:
        print(f"[sentry] scanning {len(replies)} Carol replies "
              f"from last {args.hours}h")

    new_lies = []
    for ts, body in replies:
        for probe, detail in check_reply(ts, body):
            key = f"{ts}|{probe}|{detail[:40]}"
            if key in alerted: continue
            new_lies.append((ts, probe, detail))
            alerted.add(key)

    if not args.quiet:
        for ts, probe, detail in new_lies:
            print(f"  ❌ [{ts}] {probe}: {detail}")
        if not new_lies:
            print("  ✅ no new lies detected")

    if new_lies and not args.dry_run:
        lines = ["🚨 *Carol hallucination — caught in the act*"]
        for ts, probe, detail in new_lies[:5]:
            short_ts = ts.split("T")[1][:5] if "T" in ts else ts
            lines.append(f"\n*{short_ts}* `{probe}`")
            lines.append(f"_{detail[:280]}_")
        if len(new_lies) > 5:
            lines.append(f"\n_(+{len(new_lies)-5} more)_")
        telegram_alert("\n".join(lines))

    # Persist + log
    state["alerted_lies"] = list(alerted)[-500:]
    save_state(state)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        now = datetime.now().isoformat(timespec="seconds")
        f.write(f"{now}  scanned={len(replies)}  new_lies={len(new_lies)}\n")
        for ts, probe, detail in new_lies:
            f.write(f"{now}  LIE  [{ts}]  {probe}  {detail}\n")

    return 1 if new_lies else 0


if __name__ == "__main__":
    sys.exit(main())
