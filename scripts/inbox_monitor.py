#!/usr/bin/env python3
r"""inbox_monitor.py — ALWAYS-ON inbox intelligence (user 2026-06-19:
"you have to monitor it always … when I ask you should already know what's
happening in our inbox live. I need god-level intelligence there!").

Carol must KNOW the live ground-truth of every active bid thread at all times, not
read reactively when asked. This worker:
  1. detects NEW inbound GC mail since the last run (cheap IMAP scan),
  2. maps each new message to the active bid it belongs to,
  3. re-reads ONLY those threads through thread_intel (LLM ground-truth — the engine
     that reads meaning + chronology, not keywords),
  4. updates a per-bid live state store (data/memory/inbox_state.json),
  5. on a MATERIAL change (WON / LOST / new WE_OWE / fresh reply) pings the owner.

So "what's happening in our inbox" becomes an instant read of inbox_state.json —
Carol already knows. Run by the daemon every few minutes.

Run:  python scripts/inbox_monitor.py            # incremental (since last run)
      python scripts/inbox_monitor.py --full     # (re)grade every active bid (seed)
      python scripts/inbox_monitor.py --quiet
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    pass

from _lib import thread_intel as TI
from _lib import gmail

STATE = ROOT / "data" / "memory" / "inbox_state.json"
ACTIVE = {"bid submitted", "awaiting decision", "in progress", "submitted",
          "pending", "open", "proposal drafted"}
OWNER = os.environ.get("USER_TELEGRAM_CHAT_ID", "")
STOP = {"the", "and", "for", "inc", "llc", "co", "of", "at", "nc", "sc", "remodel",
        "project", "store", "center", "building", "buildings", "improvements", "campus"}

ALERT_ICON = {"WON": "🟢 WON", "LOST": "🔴 LOST", "WE_OWE": "📨 THEY NEED SOMETHING",
              "BALL_IN_COURT": "✋ ball in their court", "AWAITING_AWARD": "⏳ awaiting award",
              "SILENT": "🔇 silent"}


def _toks(s):
    return {w for w in re.findall(r"[a-z0-9#]{3,}", (s or "").lower()) if w not in STOP}


def load_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {"bids": {}, "last_check": None}


def active_bids():
    from crm_lib import get_sheet
    out = []
    for r in get_sheet("Bid Log").get_all_records():
        if str(r.get("Status") or "").strip().lower() not in ACTIVE:
            continue
        out.append({
            "bid": (r.get("Bid #") or "").strip(),
            "project": (r.get("Project Name") or "").strip(),
            "gc": (r.get("GC / Client") or "").strip(),
            "contact": (r.get("Contact Email") or "").strip().lower(),
        })
    return [b for b in out if b["bid"] and b["project"]]


def new_inbound_since(since_dt):
    """Inbound (GC) messages newer than since_dt, with (from, subject, date)."""
    rows = []
    with gmail.connect() as M:
        for m in gmail.search(M, gmail.ALL_MAIL, "newer_than:2d", limit=120):
            fe = (m.from_email or "").lower()
            if "carolinacommercial" in fe:
                continue  # our own outbound
            try:
                d = parsedate_to_datetime(m.date_str)
            except Exception:
                d = None
            if since_dt and d and d <= since_dt:
                continue
            rows.append({"from": fe, "subject": m.subject or "", "date": d})
    return rows


def match_bid(msg, bids):
    """Best active bid for an inbound msg: project-token overlap + contact-email match."""
    subj_t = _toks(msg["subject"])
    frm = msg["from"]
    best, score = None, 0
    for b in bids:
        ov = len(subj_t & _toks(b["project"]))
        if b["contact"] and frm:
            if b["contact"] == frm:
                ov += 2
            elif b["contact"].split("@")[-1] == frm.split("@")[-1]:
                ov += 1
        if ov > score:
            best, score = b, ov
    return best if score > 0 else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="(re)grade every active bid")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    state = load_state()
    bids = active_bids()
    by_bid = {b["bid"]: b for b in bids}
    last_check = None
    if state.get("last_check"):
        try:
            last_check = datetime.fromisoformat(state["last_check"])
        except Exception:
            last_check = None

    # which bids to (re)grade
    if a.full or not last_check:
        dirty = list(bids)
        reason = "full seed" if a.full else "first run"
    else:
        inbound = new_inbound_since(last_check)
        seen = {}
        for msg in inbound:
            b = match_bid(msg, bids)
            if b:
                seen[b["bid"]] = b
        dirty = list(seen.values())
        reason = f"{len(inbound)} new inbound → {len(dirty)} bid(s) changed"
    if not a.quiet:
        print(f"[monitor] {reason}; grading {len(dirty)} of {len(bids)} active bids")

    alerts = []
    for b in dirty:
        env = TI.comprehend(b["project"], project=b["project"], gc=b["gc"], contact=b["contact"])
        prev = state["bids"].get(b["bid"], {})
        rec = {"bid": b["bid"], "project": b["project"], "gc": b["gc"],
               "status": env["status"], "ball_in_court": env.get("ball_in_court"),
               "next_action": env.get("next_action"), "open_items": env.get("open_items", []),
               "evidence": env.get("evidence", ""), "n_msgs": env.get("n_msgs"),
               "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        state["bids"][b["bid"]] = rec
        # material change → alert (only a genuine flip from a KNOWN prior state; never on first-seen/seed)
        changed = bool(prev.get("status")) and prev.get("status") != rec["status"]
        material = rec["status"] in ("WON", "LOST", "WE_OWE")
        if changed and material:
            alerts.append(rec)
        if not a.quiet:
            print(f"  {b['bid']:9} {b['project'][:34]:34} {rec['status']:14} "
                  f"(was {prev.get('status','—')}) {('*ALERT*' if (changed and material) else '')}")

    state["last_check"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

    if alerts:
        lines = ["*Inbox update — live monitor:*"]
        for r in alerts:
            tag = ALERT_ICON.get(r["status"], r["status"])
            ev = (r.get("evidence") or "").strip()
            lines.append(f"{tag} — {r['project']} ({r['gc']})" + (f"\n  ↳ \"{ev[:120]}\"" if ev else ""))
        try:
            from _lib import telegram
            telegram.send("\n".join(lines), chat_id=OWNER)
        except Exception as e:
            if not a.quiet:
                print("  (telegram failed:", e, ")")
    if not a.quiet:
        print(f"[monitor] {len(alerts)} alert(s); state → {STATE.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
