#!/usr/bin/env python3
r"""
crm_daily_summary.py — Daily CRM status breakdown + today's changes, to Telegram.

Runs morning (7am) and end-of-day (6pm) via the daemon, but can be invoked manually.

Output (Telegram message) — illustrative numbers only:
  📊 CCF Status — Mon DD
  N bids tracked · $X.XM total bid value

  • Lost: N ($X.XM)
  • Awaiting Decision: N ($X.XM)
  • Bid Submitted: N ($XXXK)
  • Won: N ($XXK bid / $XXK signed)

  Today's activity:
  • +N → Lost (BID-NNNN, BID-NNNN)
  • +N → Awaiting (from On Hold reclassification)
  • N follow-ups sent
  • N GC replies processed

  Active pipeline: $X.XM still in play

Usage:
  python scripts/crm_daily_summary.py            # send to Telegram
  python scripts/crm_daily_summary.py --print    # print only, no send
  python scripts/crm_daily_summary.py --quiet
"""

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
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

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

STATE_FILE = ROOT / "data" / "memory" / "crm_daily_summary_state.json"
LOG_FILE   = ROOT / "data" / "logs" / "crm_daily_summary.log"

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.environ.get("USER_TELEGRAM_CHAT_ID", "")


def load_state() -> dict:
    if STATE_FILE.exists():
        try: return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception: pass
    return {}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def parse_amount(v) -> float:
    if not v: return 0
    if isinstance(v, (int, float)): return float(v)
    cleaned = re.sub(r"[^\d.]", "", str(v))
    try: return float(cleaned) if cleaned else 0
    except ValueError: return 0


def fmt_money(n: float) -> str:
    if n >= 1_000_000:
        return f"${n/1_000_000:.2f}M"
    if n >= 10_000:
        return f"${n/1000:.0f}K"
    return f"${n:,.0f}"


def get_status_breakdown() -> dict:
    """Pull live status counts and dollar values from CRM."""
    from crm_lib import get_sheet
    ws = get_sheet("Bid Log")
    recs = ws.get_all_records()
    counts = Counter()
    values = defaultdict(float)
    today_lost = []
    today_won = []
    today_str = date.today().isoformat()
    for r in recs:
        s = (r.get("Status") or "").strip()
        if not s: continue
        counts[s] += 1
        values[s] += parse_amount(r.get("Bid Amount ($)"))
        # Bids that were classified today (look for "[MM/DD reply: LOST]" or WON)
        notes = (r.get("Notes") or "")
        today_md = date.today().strftime("%m/%d")
        if f"[{today_md} reply: LOST]" in notes:
            today_lost.append({"bid_id": r.get("Bid #"), "name": r.get("Project Name"),
                               "amount": parse_amount(r.get("Bid Amount ($)"))})
        if f"[{today_md} reply: WON]" in notes:
            today_won.append({"bid_id": r.get("Bid #"), "name": r.get("Project Name"),
                              "amount": parse_amount(r.get("Bid Amount ($)"))})
    return {
        "counts": dict(counts),
        "values": dict(values),
        "total_count": sum(counts.values()),
        "total_value": sum(values.values()),
        "today_lost": today_lost,
        "today_won": today_won,
    }


def count_followups_sent_today() -> int:
    """Count today's outbound follow-up emails."""
    import imaplib, email as elib
    GMAIL_USER = os.environ.get("GMAIL_USER", "estimates@carolinacommercialfinishes.com")
    GMAIL_PASS = os.environ.get("GMAIL_APP_PASSWORD", "")
    try:
        M = imaplib.IMAP4_SSL("imap.gmail.com")
        M.login(GMAIL_USER, GMAIL_PASS)
        M.select('"[Gmail]/Sent Mail"')
        today = date.today().strftime("%d-%b-%Y")
        typ, data = M.search(None, f'(SINCE "{today}" SUBJECT "Follow-Up")')
        ids = data[0].split() if data and data[0] else []
        M.logout()
        return len(ids)
    except Exception:
        return 0


def count_replies_processed_today() -> int:
    """Count CRM updates from reply-classifier today."""
    log = ROOT / "data" / "logs" / "followup_replies.log"
    if not log.exists(): return 0
    today = datetime.now().strftime("%Y-%m-%d")
    n = 0
    try:
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            if today in line and "category=" not in line and ("LOST" in line or "WON" in line
                or "STILL_AWAITING" in line or "PRICING" in line or "OUT_OF_OFFICE" in line
                or "UNCLEAR" in line):
                # rough heuristic: lines that report classifications
                if "  BID-" in line:
                    n += 1
    except Exception:
        pass
    return n


def diff_from_yesterday(current: dict, state: dict) -> list[str]:
    """Show count changes vs the snapshot stored at last run."""
    prev = state.get("last_snapshot", {})
    if not prev:
        return []
    out = []
    for status in sorted(set(current["counts"]) | set(prev.get("counts", {}))):
        c_now = current["counts"].get(status, 0)
        c_prev = prev["counts"].get(status, 0)
        if c_now != c_prev:
            delta = c_now - c_prev
            sign = "+" if delta > 0 else ""
            out.append(f"{sign}{delta} → {status}")
    return out


def build_message(b: dict, fu_count: int, replies_count: int, diffs: list[str]) -> str:
    today_str = date.today().strftime("%a, %b %d")
    lines = [
        f"📊 *CCF Status — {today_str}*",
        f"{b['total_count']} bids · {fmt_money(b['total_value'])} total bid value",
        "",
    ]
    # Status breakdown — most-recent-relevant order
    order = ["Won", "Awaiting Decision", "Bid Submitted", "Lost", "On Hold"]
    for s in order:
        if s in b["counts"]:
            n = b["counts"][s]
            v = b["values"][s]
            lines.append(f"• *{s}*: {n} ({fmt_money(v)})")
    # Other statuses not in our standard order
    for s in b["counts"]:
        if s not in order:
            lines.append(f"• {s}: {b['counts'][s]} ({fmt_money(b['values'][s])})")

    # Today's specific moves
    has_today = bool(b["today_lost"] or b["today_won"] or fu_count or replies_count or diffs)
    if has_today:
        lines.append("")
        lines.append("*Today:*")
        if diffs:
            lines.append("  " + " · ".join(diffs))
        if b["today_lost"]:
            ids = ", ".join(x["bid_id"] for x in b["today_lost"][:5])
            tot = sum(x["amount"] for x in b["today_lost"])
            lines.append(f"  ❌ Newly lost: {len(b['today_lost'])} ({fmt_money(tot)}) — {ids}")
        if b["today_won"]:
            ids = ", ".join(x["bid_id"] for x in b["today_won"][:5])
            tot = sum(x["amount"] for x in b["today_won"])
            lines.append(f"  ✅ Newly won: {len(b['today_won'])} ({fmt_money(tot)}) — {ids}")
        if fu_count:
            lines.append(f"  📤 {fu_count} follow-up email(s) sent")
        if replies_count:
            lines.append(f"  📨 {replies_count} GC reply/replies classified")

    # Active pipeline
    active_value = (b["values"].get("Awaiting Decision", 0)
                    + b["values"].get("Bid Submitted", 0))
    active_count = (b["counts"].get("Awaiting Decision", 0)
                    + b["counts"].get("Bid Submitted", 0))
    lines.append("")
    lines.append(f"_Active pipeline: {active_count} bids, {fmt_money(active_value)} in play_")

    return "\n".join(lines)


def tg_send(text: str):
    try:
        import requests
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=15,
        )
    except Exception as e:
        print(f"[telegram] send failed: {e}")


def log(msg: str, quiet: bool = False):
    if not quiet:
        print(msg)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now().isoformat(timespec='seconds')}  {msg}\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--print", action="store_true",
                    help="Print message only, do not send Telegram")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    log("[summary] pulling live CRM data...", args.quiet)
    breakdown = get_status_breakdown()
    fu_count = count_followups_sent_today()
    replies_count = count_replies_processed_today()

    state = load_state()
    diffs = diff_from_yesterday(breakdown, state)

    msg = build_message(breakdown, fu_count, replies_count, diffs)

    if args.print or args.quiet:
        print(msg)
    if not args.print:
        tg_send(msg)
        log("[summary] telegram message sent", args.quiet)

    # Save snapshot for tomorrow's diff
    state["last_snapshot"] = {
        "counts": breakdown["counts"],
        "values": breakdown["values"],
        "captured_at": datetime.now().isoformat(timespec="seconds"),
    }
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
