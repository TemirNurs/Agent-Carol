#!/usr/bin/env python3
r"""
followup_scheduler.py — Auto-cadence engine for follow-up emails.

Runs daily. For every Bid Submitted / Awaiting Decision row in the CRM:
  - Computes days since submitted + days since last touch
  - Decides if a follow-up is due based on size-tiered cadence
  - Drafts the appropriate email type via Gemini Flash
  - Decides between auto-send (small bids <$25K) vs approval staging (≥$25K)
  - Auto-closes stale bids (3+ FUs, 60+ days silent, <$100K)
  - Flags big silent bids for phone-call escalation (≥$100K, ≥30d, ≥2 FUs)

Cadence by bid size:
                  FU#1     FU#2     FU#3     Closeout/Phone
  < $25K          d+7      d+21     d+45     auto-close at d+60
  $25K - $100K    d+7      d+14     d+28     auto-close at d+60
  $100K+          d+5      d+12     d+25     PHONE flag at d+30, no auto-close

"d+N" = days since Bid Submitted Date.
A follow-up is "due" if days-since-last-touch >= the gap to the next FU number,
based on count of prior FUs we've sent.

Approval gate:
  - <$25K → auto-send (drafts straight to send_email.py)
  - ≥$25K → stage to data/pending_followups/{bid_id}.json + Telegram digest
            user replies "send pending" to release queue

Usage:
  python scripts/followup_scheduler.py              # full pass
  python scripts/followup_scheduler.py --dry-run    # preview, no send/stage
  python scripts/followup_scheduler.py --quiet
"""

import argparse
import json
import os
import re
import subprocess
import sys
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

PENDING_DIR = ROOT / "data" / "pending_followups"
LOG_FILE    = ROOT / "data" / "logs" / "followup_scheduler.log"

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.environ.get("USER_TELEGRAM_CHAT_ID", "")

# Approval threshold
AUTO_SEND_BELOW = 25_000     # auto-send small bids
PHONE_FLAG_AT   = 100_000    # ≥$100K + 30 days + ≥2 FUs → flag for phone call
AUTO_CLOSE_BELOW = 100_000   # only auto-close bids under this size


def _has_prior_win_with_gc(gc_name: str) -> bool:
    """True if we have any 'Won' status with this GC. Cached per-run."""
    if not gc_name: return False
    if not hasattr(_has_prior_win_with_gc, "_cache"):
        from crm_lib import get_sheet
        try:
            recs = get_sheet("Bid Log").get_all_records()
        except Exception:
            return False
        _has_prior_win_with_gc._cache = {
            (r.get("GC / Client") or "").strip().lower()
            for r in recs
            if (r.get("Status") or "").strip() == "Won"
        }
    return gc_name.strip().lower() in _has_prior_win_with_gc._cache


def cadence_for_size(amount: float) -> dict:
    """Return cadence dict for a bid size."""
    if amount < 25_000:
        return {"fu1": 7, "fu2": 21, "fu3": 45, "closeout": 60}
    elif amount < 100_000:
        return {"fu1": 7, "fu2": 14, "fu3": 28, "closeout": 60}
    else:
        return {"fu1": 5, "fu2": 12, "fu3": 25, "closeout": None}


def parse_amount(v) -> float:
    if not v: return 0
    if isinstance(v, (int, float)): return float(v)
    s = re.sub(r"[^\d.]", "", str(v))
    try: return float(s) if s else 0
    except ValueError: return 0


def days_between_iso(date_str: str) -> int | None:
    """Days from a flexible date string to today."""
    if not date_str: return None
    s = str(date_str).strip()
    for fmt in ("%a, %d %b %Y", "%m/%d/%Y", "%Y-%m-%d", "%d %b %Y", "%B %d, %Y"):
        try:
            d = datetime.strptime(s, fmt).date()
            return (date.today() - d).days
        except ValueError:
            continue
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        try:
            d = datetime(int(m.group(3)), int(m.group(1)), int(m.group(2))).date()
            return (date.today() - d).days
        except Exception:
            return None
    return None


def get_pm_data(bid_id: str) -> dict:
    """Pull follow-up history from the postmortem JSON sidecar.

    Prefers `{bid_id}_{slug}.json` (structured) — falls back to regex-parsing
    the .md if no sidecar exists (legacy postmortems from before 5/6/2026).
    """
    pm_dir = ROOT / "data" / "memory" / "loss_postmortems"

    # Prefer JSON sidecar (fast, exact)
    json_matches = list(pm_dir.glob(f"{bid_id}_*.json"))
    if json_matches:
        path = sorted(json_matches, key=lambda p: p.stat().st_mtime, reverse=True)[0]
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return {
                "followup_count": data.get("followup_count", 0),
                "last_followup":  data.get("last_followup_date", ""),
                "last_gc_reply":  data.get("last_gc_reply_date", ""),
            }
        except Exception:
            pass  # fall through to .md

    # Legacy fallback — regex-parse the .md
    md_matches = list(pm_dir.glob(f"{bid_id}_*.md"))
    if not md_matches:
        return {"followup_count": 0, "last_followup": "", "last_gc_reply": ""}
    md = sorted(md_matches, key=lambda p: p.stat().st_mtime, reverse=True)[0].read_text(encoding="utf-8")
    fu_match = re.search(r"\*\*Follow-ups\s*\((\d+)\)", md)
    fu_count = int(fu_match.group(1)) if fu_match else 0
    fu_section = re.search(r"\*\*Follow-ups[^*]+?(?=\*\*GC replies|\Z)", md, re.DOTALL)
    last_fu = ""
    if fu_section:
        dates = re.findall(r"(\d{4}-\d{2}-\d{2})\s+→", fu_section.group(0))
        if dates: last_fu = max(dates)
    gc_section = re.search(r"\*\*GC replies / inbound[\s\S]*?(?=\Z)", md)
    last_gc = ""
    if gc_section:
        dates = re.findall(r"(\d{4}-\d{2}-\d{2})\s+←", gc_section.group(0))
        if dates: last_gc = max(dates)
    return {"followup_count": fu_count, "last_followup": last_fu, "last_gc_reply": last_gc}


def days_since(iso_date: str) -> int | None:
    """Days since YYYY-MM-DD."""
    if not iso_date: return None
    try:
        d = datetime.strptime(iso_date[:10], "%Y-%m-%d").date()
        return (date.today() - d).days
    except Exception:
        return None


def decide_action(row: dict, pm: dict) -> dict:
    """Decide what action (if any) to take on this bid.

    Returns dict with:
      action: 'send_fu' | 'closeout' | 'auto_close' | 'phone_flag' | 'none'
      fu_type: 'follow-up-1' / -2 / -3 / 'closeout'  (when sending)
      reason: string
    """
    amount = parse_amount(row.get("Bid Amount ($)"))
    cadence = cadence_for_size(amount)
    submitted = row.get("Bid Submitted Date") or ""
    days_total = days_between_iso(submitted)
    if days_total is None:
        return {"action": "none", "reason": "no submitted date"}

    fu_count = pm["followup_count"]
    last_fu_iso = pm["last_followup"]
    last_fu_days = days_since(last_fu_iso) if last_fu_iso else None
    last_gc_iso = pm["last_gc_reply"]
    last_gc_days = days_since(last_gc_iso) if last_gc_iso else None

    # Phone flag check for big bids (priority over send)
    if (amount >= PHONE_FLAG_AT
            and days_total >= 30
            and fu_count >= 2
            and (last_gc_days is None or last_gc_days >= 14)):
        return {"action": "phone_flag", "reason": f"${amount:,.0f}, {days_total}d, {fu_count} FUs, GC silent {last_gc_days}d"}

    # Auto-close check for stale small/mid bids.
    # Tightened thresholds (per user, May 5 2026): require 90 days total,
    # 45+ days since last GC reply, AND we don't have a prior Won with this GC.
    if (cadence.get("closeout")
            and amount < AUTO_CLOSE_BELOW
            and fu_count >= 3
            and days_total >= 90               # was 60 — too aggressive
            and (last_gc_days is None or last_gc_days >= 45)   # was 30
            and not _has_prior_win_with_gc(row.get("GC / Client", ""))):
        return {"action": "auto_close",
                "reason": f"{fu_count} FUs sent, {days_total}d total, last reply {last_gc_days}d ago, no prior wins"}

    # Closeout email (final pre-close warning) for small/mid bids approaching closeout day
    if (cadence.get("closeout")
            and amount < AUTO_CLOSE_BELOW
            and fu_count >= 3
            and days_total >= cadence["closeout"] - 7  # 7 days before auto-close
            and days_total < cadence["closeout"]
            and (last_gc_days is None or last_gc_days >= 21)):
        return {"action": "send_fu", "fu_type": "closeout",
                "reason": f"final closeout email before auto-close at d+{cadence['closeout']}"}

    # Determine which FU is next based on count of prior FUs
    if fu_count == 0:
        # FU#1 due if days_total >= cadence['fu1']
        if days_total >= cadence["fu1"]:
            return {"action": "send_fu", "fu_type": "follow-up-1",
                    "reason": f"day {days_total}, no FUs yet (threshold {cadence['fu1']})"}
    elif fu_count == 1:
        # FU#2 due if days_total >= cadence['fu2'] AND last_fu was >7 days ago
        if days_total >= cadence["fu2"] and (last_fu_days is None or last_fu_days >= 7):
            return {"action": "send_fu", "fu_type": "follow-up-2",
                    "reason": f"day {days_total}, 1 FU sent {last_fu_days}d ago (threshold {cadence['fu2']})"}
    elif fu_count == 2:
        # FU#3 due
        if days_total >= cadence["fu3"] and (last_fu_days is None or last_fu_days >= 7):
            return {"action": "send_fu", "fu_type": "follow-up-3",
                    "reason": f"day {days_total}, 2 FUs sent, last {last_fu_days}d ago (threshold {cadence['fu3']})"}

    return {"action": "none", "reason": f"day {days_total}, {fu_count} FUs, no action due"}


def stage_for_approval(bid_id: str, draft: dict, decision: dict, amount: float):
    """Save a pending follow-up draft to disk for user approval."""
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "bid_id": bid_id,
        "amount": amount,
        "fu_type": decision.get("fu_type"),
        "reason": decision.get("reason"),
        "to": draft.get("to"),
        "subject": draft.get("subject"),
        "body": draft.get("body"),
        "staged_at": datetime.now().isoformat(timespec="seconds"),
    }
    out = PENDING_DIR / f"{bid_id}.json"
    out.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")


def auto_close_bid(bid_id: str, reason: str, dry_run: bool):
    """Set Status=Closed-No Response and Loss Reason on a stale bid."""
    if dry_run: return
    from crm_lib import get_sheet
    from gspread.utils import rowcol_to_a1
    ws = get_sheet("Bid Log")
    headers = ws.row_values(1)
    bid_col = headers.index("Bid #") + 1
    status_col = headers.index("Status") + 1
    loss_reason_col = headers.index("Loss Reason") + 1 if "Loss Reason" in headers else None
    win_loss_col = headers.index("Win/Loss") + 1 if "Win/Loss" in headers else None
    notes_col = headers.index("Notes") + 1
    bid_ids = ws.col_values(bid_col)
    try:
        row = bid_ids.index(bid_id) + 1
    except ValueError:
        return
    cur_notes = ws.cell(row, notes_col).value or ""
    note_line = f"[{date.today():%m/%d} auto-close] {reason}"
    new_notes = (cur_notes + "\n" + note_line).strip() if cur_notes else note_line
    updates = [
        {"range": rowcol_to_a1(row, status_col), "values": [["Closed - No Response"]]},
        {"range": rowcol_to_a1(row, notes_col),  "values": [[new_notes]]},
    ]
    if loss_reason_col:
        cur_lr = ws.cell(row, loss_reason_col).value or ""
        if not cur_lr:
            updates.append({"range": rowcol_to_a1(row, loss_reason_col),
                            "values": [[f"No Response (auto-closed after {reason[:60]})"]]})
    if win_loss_col:
        updates.append({"range": rowcol_to_a1(row, win_loss_col), "values": [["NO RESPONSE"]]})
    ws.batch_update(updates, value_input_option="USER_ENTERED")


def tg_send(text: str):
    try:
        import requests
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=15,
        )
    except Exception:
        pass


def log(msg: str, quiet: bool = False):
    if not quiet:
        print(msg)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now().isoformat(timespec='seconds')}  {msg}\n")


def fmt_money(n: float) -> str:
    if n >= 1_000_000: return f"${n/1_000_000:.2f}M"
    if n >= 10_000:    return f"${n/1000:.0f}K"
    return f"${n:,.0f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    from crm_lib import get_sheet
    ws = get_sheet("Bid Log")
    recs = ws.get_all_records()

    # Pull active bids (Bid Submitted + Awaiting Decision)
    active = [r for r in recs if (r.get("Status") or "").strip()
              in ("Bid Submitted", "Awaiting Decision")]
    log(f"[scheduler] {len(active)} active bid(s) to evaluate", args.quiet)

    # Refresh postmortems for ALL active bids before deciding cadence — the
    # scheduler's logic depends on accurate "last_followup" dates from Gmail.
    # Skip in dry-run to keep dry-runs cheap.
    if not args.dry_run:
        log("[scheduler] refreshing postmortems for active bids (Gmail scan)...", args.quiet)
        # Delete existing files to force regeneration
        pm_dir = ROOT / "data" / "memory" / "loss_postmortems"
        for r in active:
            bid_id = r.get("Bid #", "")
            for old in pm_dir.glob(f"{bid_id}_*"):
                try: old.unlink()
                except Exception: pass
        # Regenerate, one call per status
        for status in ("Bid Submitted", "Awaiting Decision"):
            subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "loss_postmortem.py"),
                 "--status", status, "--quiet"],
                capture_output=True, timeout=900,
            )

    # Counters
    auto_sent = []
    staged = []
    auto_closed = []
    phone_flagged = []
    no_action = 0

    for r in active:
        bid_id = r.get("Bid #", "")
        if not bid_id: continue
        amount = parse_amount(r.get("Bid Amount ($)"))
        pm = get_pm_data(bid_id)
        decision = decide_action(r, pm)
        log(f"  {bid_id}  {(r.get('Project Name') or '')[:40]:40}  {fmt_money(amount):>8}  → {decision['action']}  ({decision['reason']})", args.quiet)

        if decision["action"] == "none":
            no_action += 1

        elif decision["action"] == "auto_close":
            auto_close_bid(bid_id, decision["reason"], args.dry_run)
            auto_closed.append({"bid_id": bid_id, "name": r.get("Project Name"),
                                "amount": amount, "reason": decision["reason"]})

        elif decision["action"] == "phone_flag":
            phone_flagged.append({
                "bid_id": bid_id,
                "name": r.get("Project Name"),
                "gc": r.get("GC / Client"),
                "contact": r.get("Contact Name"),
                "phone": r.get("Contact Phone") or "(no phone in CRM)",
                "amount": amount,
                "reason": decision["reason"],
            })

        elif decision["action"] == "send_fu":
            # Draft
            r_draft = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "draft_email.py"),
                 "--bid", bid_id, "--type", decision["fu_type"], "--json"],
                capture_output=True, text=True, encoding="utf-8", timeout=60,
            )
            if r_draft.returncode != 0:
                log(f"     DRAFT FAIL: {(r_draft.stderr or '')[:120]}", args.quiet)
                continue
            try:
                draft = json.loads(r_draft.stdout)
            except Exception:
                continue
            if "error" in draft:
                log(f"     DRAFT ERROR: {draft.get('error')}", args.quiet)
                continue

            # Decide: auto-send or stage?
            if amount < AUTO_SEND_BELOW:
                # Auto-send
                if args.dry_run:
                    log(f"     [DRY] would AUTO-SEND to {draft.get('to')}", args.quiet)
                    auto_sent.append({"bid_id": bid_id, "name": r.get("Project Name"),
                                      "amount": amount, "to": draft.get("to"),
                                      "fu_type": decision["fu_type"], "dry_run": True})
                else:
                    to_clean = re.sub(r"\s+", ",", draft["to"].strip()).strip(",")
                    r_send = subprocess.run(
                        [sys.executable, str(ROOT / "scripts" / "send_email.py"),
                         "--to", to_clean, "--subject", draft["subject"], "--body", draft["body"]],
                        capture_output=True, text=True, encoding="utf-8", timeout=60,
                    )
                    if '"status": "sent"' in r_send.stdout:
                        log(f"     AUTO-SENT to {to_clean}", args.quiet)
                        auto_sent.append({"bid_id": bid_id, "name": r.get("Project Name"),
                                          "amount": amount, "to": to_clean,
                                          "fu_type": decision["fu_type"]})
                    else:
                        log(f"     SEND FAIL: {r_send.stdout[:200]}", args.quiet)
            else:
                # Stage for approval
                if args.dry_run:
                    log(f"     [DRY] would STAGE for approval", args.quiet)
                else:
                    stage_for_approval(bid_id, draft, decision, amount)
                staged.append({"bid_id": bid_id, "name": r.get("Project Name"),
                               "amount": amount, "fu_type": decision["fu_type"]})

    # Summary
    log(f"\n[scheduler] no action: {no_action}, auto-sent: {len(auto_sent)}, "
        f"staged: {len(staged)}, auto-closed: {len(auto_closed)}, "
        f"phone-flagged: {len(phone_flagged)}", args.quiet)

    # Telegram digest
    if not args.dry_run and (auto_sent or staged or auto_closed or phone_flagged):
        lines = [f"📅 *Follow-up scheduler — {date.today():%a, %b %d}*"]
        if auto_sent:
            lines.append(f"\n*Auto-sent ({len(auto_sent)} small bids):*")
            for x in auto_sent[:8]:
                lines.append(f"  ✓ {x['bid_id']} {fmt_money(x['amount'])} — {(x['name'] or '')[:35]} ({x['fu_type']})")
        if staged:
            lines.append(f"\n*Pending your approval ({len(staged)} ≥$25K):*")
            for x in staged[:8]:
                lines.append(f"  📤 {x['bid_id']} {fmt_money(x['amount'])} — {(x['name'] or '')[:35]} ({x['fu_type']})")
            lines.append(f"\nReply *send pending* to release all, or *skip pending* to defer.")
        if auto_closed:
            lines.append(f"\n*Auto-closed ({len(auto_closed)} stale bids):*")
            for x in auto_closed[:5]:
                lines.append(f"  🗄 {x['bid_id']} {fmt_money(x['amount'])} — {(x['name'] or '')[:35]}")
        if phone_flagged:
            lines.append(f"\n📞 *PHONE-CALL FLAGS — big bids gone silent:*")
            for x in phone_flagged[:5]:
                contact = x['contact'] or '?'
                phone = x['phone'] or '?'
                lines.append(f"  • {x['bid_id']} {fmt_money(x['amount'])} {(x['name'] or '')[:30]}")
                lines.append(f"    Call {contact} at {phone} ({x['gc'][:25]})")
                lines.append(f"    _{x['reason']}_")
        tg_send("\n".join(lines))

    return 0


if __name__ == "__main__":
    sys.exit(main())
