#!/usr/bin/env python3
"""
Deterministic bid reminders — zero LLM cost.

Reads active_bids.json, finds items needing attention today, sends formatted
Telegram messages directly via the Bot API. Designed for daemon runs at 7 AM,
12 PM, and 4 PM daily.

Triggers (any of these fires an alert):
  - Bid due TODAY  (if not already alerted today)
  - Bid due TOMORROW  (once, morning only)
  - Bid due in 2 hours or less  (hourly check)
  - Known-GC invitation arrived (first time seen)
  - $100K+ invitation arrived (first time seen)

Deduplication: tracks alerted items in data/memory/reminder_log.json.

Usage:
  python scripts/bid_reminders.py                 # normal run
  python scripts/bid_reminders.py --dry-run       # print, don't send
  python scripts/bid_reminders.py --mode morning  # morning brief (due today+tomorrow)
  python scripts/bid_reminders.py --mode imminent # bids closing soon (<6h)
  python scripts/bid_reminders.py --mode daily    # simple 1/day at 7 AM
"""

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = Path(__file__).resolve().parent.parent
BIDS_FILE = BASE / "data" / "memory" / "active_bids.json"
GC_DIR = BASE / "data" / "memory" / "gc"
LOG_FILE = BASE / "data" / "memory" / "reminder_log.json"

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("USER_TELEGRAM_CHAT_ID", "")

FACILITY_RATES = {"Hotel":2.75,"Restaurant":2.25,"Retail":2.0,"School":2.0,
                  "Gov/Mil":1.75,"Civic":1.5,"Multifamily":2.0,"Office":1.5,
                  "Healthcare":2.0,"Other":1.75}
DEFAULT_SF = {"Retail":4000,"Hotel":60000,"Restaurant":3500,"Gov/Mil":15000,
              "School":50000,"Civic":25000,"Multifamily":150000,"Office":20000,
              "Healthcare":25000,"Other":20000}


def parse_date(s):
    if not s: return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try: return datetime.strptime(s.strip(), fmt).date()
        except ValueError: pass
    return None


def facility_of(name):
    n = (name or "").lower()
    if any(k in n for k in ["school","elementary","university","college"]): return "School"
    if any(k in n for k in ["hotel","suites","marriott","hilton"]): return "Hotel"
    if any(k in n for k in ["bojangles","chase","sheetz","dollar","cvs","ulta","bank","autozone"]): return "Retail"
    if any(k in n for k in ["restaurant","brewery","pure green"]): return "Restaurant"
    if any(k in n for k in ["va ","ems","fire","guard","mcas","military","postal","federal"]): return "Gov/Mil"
    if any(k in n for k in ["community center","park","rec"]): return "Civic"
    if any(k in n for k in ["residence hall","apartment","village"]): return "Multifamily"
    if any(k in n for k in ["hospital","medical","clinic","pediatrics"]): return "Healthcare"
    return "Other"


def est_value_k(name):
    fac = facility_of(name)
    m = re.search(r"([0-9]{3,6})\s*sf", (name or "").lower())
    sf = int(m.group(1)) if m else DEFAULT_SF.get(fac, 20000)
    if "campus" in (name or "").lower() or "multiple" in (name or "").lower(): sf *= 2
    if "renovation" in (name or "").lower() or "reno" in (name or "").lower(): sf = int(sf*0.7)
    return (sf * FACILITY_RATES.get(fac, 1.75)) / 1000


def is_known_gc(gc_name):
    if not gc_name or not GC_DIR.exists(): return False
    key = re.sub(r"[^a-z]", "", (gc_name or "").lower())
    for f in GC_DIR.glob("*.json"):
        k = re.sub(r"[^a-z]", "", f.stem.lower())
        if k and (k in key or (len(key) >= 8 and k.startswith(key[:8]))):
            return True
    return False


def src_tag(s):
    return {"buildingconnected":"BC","constructconnect":"CC","email":"EM"}.get((s or "").lower(),"?")


def tg_send(text):
    """Send a Telegram message via Bot API. Markdown supported."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200
    except Exception as e:
        print(f"[tg] send failed: {e}")
        return False


def load_log():
    if LOG_FILE.exists():
        try: return json.load(open(LOG_FILE, encoding="utf-8"))
        except Exception: return {}
    return {}


def save_log(d):
    LOG_FILE.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")


def mark(log, key):
    log[key] = datetime.now().isoformat(timespec="seconds")


def already_alerted_today(log, key):
    ts = log.get(key)
    if not ts: return False
    try:
        dt = datetime.fromisoformat(ts).date()
        return dt == date.today()
    except Exception:
        return False


def bid_line(b):
    val_k = est_value_k(b.get("project_name",""))
    tag = "🎯" if val_k >= 100 else ("✅" if val_k >= 50 else "⚠️")
    if is_known_gc(b.get("gc","")): tag += "⭐"
    dist = b.get("distance_miles")
    dist_s = f"{dist:.0f}mi" if isinstance(dist,(int,float)) else "?mi"
    name = (b.get("project_name") or "?")[:50]
    gc = (b.get("gc") or "?")[:22]
    return f"{tag} *{name}*\n    {b.get('due_date','?')} · [{src_tag(b.get('source'))}] · {gc} · {dist_s} · ~${val_k:.0f}K"


def morning_brief(bids, log, dry_run=False):
    today = date.today()
    tomorrow = today + timedelta(days=1)
    today_b = [b for b in bids if parse_date(b.get("due_date")) == today]
    tomorrow_b = [b for b in bids if parse_date(b.get("due_date")) == tomorrow]

    if not today_b and not tomorrow_b:
        return False

    key = f"morning_{today.isoformat()}"
    if already_alerted_today(log, key):
        return False

    lines = [f"🌅 *Morning brief — {today.strftime('%a %b %d')}*\n"]
    if today_b:
        lines.append(f"*Due TODAY ({len(today_b)}):*")
        for b in today_b: lines.append(bid_line(b))
        lines.append("")
    if tomorrow_b:
        lines.append(f"*Due TOMORROW ({len(tomorrow_b)}):*")
        for b in tomorrow_b: lines.append(bid_line(b))

    text = "\n".join(lines)
    print(text)
    if dry_run:
        return True
    if tg_send(text):
        mark(log, key)
        return True
    return False


def imminent_alert(bids, log, dry_run=False):
    """Alert for bids due in <6 hours. Fires at most once per bid per day."""
    today = date.today()
    todays = [b for b in bids if parse_date(b.get("due_date")) == today]
    if not todays:
        return False
    # We don't have due TIME, only date. So "imminent" = due today + not yet alerted + after noon
    now = datetime.now()
    if now.hour < 12:
        return False  # morning brief handles AM
    to_alert = []
    for b in todays:
        key = f"imminent_{b.get('project_name','')[:40]}_{today.isoformat()}"
        if already_alerted_today(log, key): continue
        # Only flag sweet-spot or above + known GC
        val = est_value_k(b.get("project_name",""))
        if val >= 50 or is_known_gc(b.get("gc","")):
            to_alert.append((key, b))
    if not to_alert:
        return False
    lines = [f"⏰ *Due TODAY by 5 PM ({len(to_alert)}):*\n"]
    for key, b in to_alert:
        lines.append(bid_line(b))
    text = "\n".join(lines)
    print(text)
    if dry_run:
        return True
    if tg_send(text):
        for key, _ in to_alert: mark(log, key)
        return True
    return False


def new_strong_match(bids, log, dry_run=False):
    """One-time alert when a fresh bid from a known GC or $100K+ appears."""
    to_alert = []
    for b in bids:
        key = f"new_{b.get('project_name','')[:40]}_{(b.get('gc') or '')[:20]}"
        if key in log: continue
        val = est_value_k(b.get("project_name",""))
        is_strong = val >= 100 or is_known_gc(b.get("gc",""))
        if not is_strong: continue
        # Skip past-due
        d = parse_date(b.get("due_date"))
        if d and d < date.today(): continue
        to_alert.append((key, b))
    # Rate-limit: max 5 per run to avoid spam
    to_alert = to_alert[:5]
    if not to_alert:
        return False
    lines = [f"🔥 *New strong-match bid{'s' if len(to_alert)>1 else ''}:*\n"]
    for key, b in to_alert:
        lines.append(bid_line(b))
    text = "\n".join(lines)
    print(text)
    if dry_run:
        return True
    if tg_send(text):
        for key, _ in to_alert: mark(log, key)
        return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["morning", "imminent", "new", "all"], default="all")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not BIDS_FILE.exists():
        print("[reminders] active_bids.json missing"); return
    bids = json.load(open(BIDS_FILE, encoding="utf-8"))
    log = load_log()

    sent_any = False
    if args.mode in ("morning", "all"):
        sent_any |= morning_brief(bids, log, args.dry_run)
    if args.mode in ("imminent", "all"):
        sent_any |= imminent_alert(bids, log, args.dry_run)
    if args.mode in ("new", "all"):
        sent_any |= new_strong_match(bids, log, args.dry_run)

    if not args.dry_run:
        save_log(log)
    if not sent_any:
        print("[reminders] nothing to alert.")


if __name__ == "__main__":
    main()
