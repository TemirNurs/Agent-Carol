#!/usr/bin/env python3
"""Today's chase — consolidated per recipient, 30-min spacing, 3/day cap,
skips bids submitted < 3 days ago (within 72h cadence), skips recipients
already-chased today. Sends via send_email.py (proper CCF Gmail) with
the internal CC list. Logs each send to chase state + bid_status history.
"""
from __future__ import annotations
import argparse, json, os, re, subprocess, sys, time
from collections import defaultdict
from datetime import datetime, date, timedelta
from pathlib import Path

try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
SEND_EMAIL = ROOT / "scripts" / "send_email.py"
STATE_FILE = ROOT / "data" / "memory" / "aggressive_chase_state.json"
BS_FILE    = ROOT / "data" / "memory" / "bid_status.json"
LOG_FILE   = ROOT / "data" / "logs" / "chase_today.log"
CC_INTERNAL = os.environ.get("CCF_INTERNAL_CC", "cs@carolinacommercialfinishes.com")

ACTIVE_STATUSES = {"Bid Submitted", "Awaiting Decision"}
INACTIVE_FLAGS = ("[BOUNCE]", "[NOT BIDDING]", "[WITHDRAWN]", "[ON HOLD]")
PYEXE = sys.executable

SIG = (
    "\n\nBest,\nNursultan Temirbaev | Manager\n"
    "Carolina Commercial Finishes / Budget Painting and Wallcovering LLC\n"
    "3308 Chancellor Lane, Monroe NC 28110\n"
    "(980) 348-1827 · cs@carolinacommercialfinishes.com\n"
)

from crm_lib import get_sheet

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


def log(msg):
    print(msg)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now().isoformat(timespec='seconds')}  {msg}\n")


def first_name(s):
    s = (s or "").strip()
    if not s: return ""
    return s.split()[0].rstrip(",;:.")


def parse_date(s):
    if not s: return None
    for fmt in ("%m/%d/%Y","%m/%d/%y","%Y-%m-%d"):
        try: return datetime.strptime(str(s).strip(), fmt).date()
        except Exception: pass
    return None


def days_chased_today(history, email):
    """Count chases sent to this exact recipient today."""
    n = 0
    today = date.today().isoformat()
    for h in history:
        if h.get("trigger") not in ("chase","chase_silent","chase_today","chase_silent_followups","followup"):
            continue
        if (h.get("to_email","") or "").lower() != email.lower():
            continue
        if h.get("at","").startswith(today):
            n += 1
    return n


_REPLY_CACHE = {}
def has_replied_recently(email, days=14):
    """Did this recipient send us ANY reply in the last N days?

    Critical: chase_today must respect prior intelligence. If a GC replied
    saying the project was awarded to another sub, we MUST NOT re-chase them
    later with another generic status-check. The 'fuck them
    till they reply' rule means STOP once they reply.

    Cached per-process so we don't hammer IMAP for the same address twice.
    """
    if not email: return False
    em = email.strip().lower()
    if em in _REPLY_CACHE:
        return _REPLY_CACHE[em]
    import imaplib, os
    USER = os.environ.get("GMAIL_USER", "estimates@carolinacommercialfinishes.com")
    PASS = os.environ.get("GMAIL_APP_PASSWORD", "")
    since_dt = date.today() - timedelta(days=days)
    since = since_dt.strftime("%d-%b-%Y")
    try:
        M = imaplib.IMAP4_SSL("imap.gmail.com")
        M.login(USER, PASS)
        M.select("INBOX")
        st, ids = M.search(None, f'(FROM "{em}" SINCE "{since}")')
        n = len(ids[0].split()) if ids[0] else 0
        M.logout()
    except Exception:
        n = 0
    result = n > 0
    _REPLY_CACHE[em] = result
    return result


def build_email(contact_name, bids):
    """Build a consolidated chase body referencing all `bids` for one contact."""
    name = first_name(contact_name) or "there"
    n = len(bids)
    # Subject
    if n == 1:
        b = bids[0]
        subj = f"Status check — {b['project'][:55]} ({b['bid_id']})"
        opener = (f"Hi {name},\n\n"
                  f"Just checking in on our {b['project']} proposal "
                  f"({b['bid_id']}, submitted {b['sub_date']}).\n")
    elif n == 2:
        subj = (f"Status check — {bids[0]['project'][:30]} + "
                f"{bids[1]['project'][:30]}")
        opener = (f"Hi {name},\n\nWe submitted two proposals to you recently "
                  f"and wanted to check on status:\n")
    else:
        subj = f"Status check — {n} CCF proposals with {bids[0]['gc'][:25]}"
        opener = (f"Hi {name},\n\nWe submitted {n} proposals to you recently "
                  f"and wanted to check on status:\n")

    # Build bullet list
    lines = []
    max_age = max(b["age"] for b in bids)
    for b in sorted(bids, key=lambda x: -x["age"]):
        amt_str = f" — ${b['amount']:,}" if b.get("amount") else ""
        lines.append(f"  • {b['project']} ({b['bid_id']}) — submitted "
                     f"{b['sub_date']} ({b['age']}d ago){amt_str}")

    # Tone by age
    if max_age >= 60:
        closer = (
            "\nThese have been open a while now — should I close them out, or "
            "are decisions still pending? Happy to revise if pricing needs a "
            "fresh look.\n"
        )
    elif max_age >= 30:
        closer = (
            "\nAny update on the timeline or award status? Happy to clarify "
            "scope or revisit pricing if it helps move things along.\n"
        )
    else:
        closer = (
            "\nHave you been able to review? Let me know if you have any "
            "questions or need anything else from us before bid time / award.\n"
        )

    body = opener + "\n" + "\n".join(lines) + "\n" + closer
    return subj, body + SIG


def main():
    # GLOBAL CHASE-BATCH LOCK — only one chase pipeline at a time. See
    # scripts/_lib/chase_batch_lock.py for the rationale (5/25 incident).
    _scripts_dir = str(Path(__file__).resolve().parent)
    if _scripts_dir not in sys.path:
        sys.path.insert(0, _scripts_dir)
    try:
        from _lib.chase_batch_lock import acquire as _global_acquire, release as _global_release
        _need_lock = ("--apply" in sys.argv)
        if _need_lock and not _global_acquire(__file__):
            sys.exit(2)
        if _need_lock:
            import atexit as _atx; _atx.register(_global_release)
    except ImportError as _e:
        if "--apply" in sys.argv:
            print(f"WARN: global chase lock unavailable ({_e}) — refusing to "
                  "run rather than risk a double chase", file=sys.stderr)
            sys.exit(3)

    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="Actually send (default: dry-run)")
    ap.add_argument("--interval", type=int, default=1800,
                    help="Seconds between sends (default 1800 = 30 min)")
    ap.add_argument("--min-age-days", type=int, default=3,
                    help="Skip bids newer than N days (default 3 = wait 72h)")
    ap.add_argument("--max-per-recipient", type=int, default=3,
                    help="Daily cap per recipient (default 3)")
    args = ap.parse_args()

    state = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    bs = json.loads(BS_FILE.read_text(encoding="utf-8")) if BS_FILE.exists() else {"overrides":{},"history":[]}
    history = bs.get("history", [])

    # Pull CRM
    ws = get_sheet("Bid Log")
    rows = ws.get_all_values()
    hdr = rows[0]
    H = {h:i for i,h in enumerate(hdr)}

    # Build pool of chaseable bids
    today = date.today()
    pool_by_contact = defaultdict(list)
    contact_meta = {}
    seen_projects = set()  # de-dup same project to same address

    for r in rows[1:]:
        if len(r) < len(hdr): continue
        status = r[H["Status"]]
        if status not in ACTIVE_STATUSES: continue
        notes = (r[H["Notes"]] or "").upper() if "Notes" in H else ""
        if any(f in notes for f in INACTIVE_FLAGS): continue
        proj = r[H["Project Name"]]
        if not proj: continue
        email = (r[H["Contact Email"]] or "").strip().lower()
        # First email if multi
        email = re.split(r"[,;]\s*|\s+", email)[0].strip() if email else ""
        if not email or "@" not in email: continue
        contact = r[H["Contact Name"]] if "Contact Name" in H else ""
        gc = r[H["GC / Client"]] if "GC / Client" in H else ""
        bid_id = r[H["Bid #"]]
        sub_d = parse_date(r[H["Bid Submitted Date"]] if "Bid Submitted Date" in H else "")
        if not sub_d: continue
        age = (today - sub_d).days
        if age < args.min_age_days:
            continue
        amount_str = r[H["Bid Amount ($)"]] if "Bid Amount ($)" in H else ""
        try: amount = int(re.sub(r"[^\d]", "", amount_str)) if amount_str else 0
        except Exception: amount = 0
        # Dedup same project+address
        key = (re.sub(r"[^a-z0-9]","",proj.lower())[:30], email)
        if key in seen_projects: continue
        seen_projects.add(key)

        # Skip recipients already at daily cap
        already = days_chased_today(history, email)
        if already >= args.max_per_recipient:
            continue
        # Skip recipients who replied within the last 14 days. They've already
        # given us status (or said wait, or redirected to PM). Re-chasing them
        # with another generic status check is rude and undoes their reply.
        if has_replied_recently(email, days=14):
            continue
        internal_id = (r[H["Internal ID"]] if "Internal ID" in H else "").strip()
        pool_by_contact[email].append({
            "bid_id": bid_id, "project": proj, "age": age,
            "sub_date": sub_d.strftime("%m/%d/%Y"), "amount": amount,
            "gc": gc, "status": status, "internal_id": internal_id,
        })
        if email not in contact_meta:
            contact_meta[email] = {"contact_name": contact, "gc": gc}

    # Sort contacts by highest-$ bid first
    contacts = sorted(pool_by_contact.items(),
                      key=lambda kv: -max(b["amount"] for b in kv[1]))

    if not contacts:
        log("[chase-today] Nothing to send. Done.")
        return

    log(f"[chase-today] {len(contacts)} contacts queued, "
        f"{sum(len(v) for v in pool_by_contact.values())} total bids referenced")
    if not args.apply:
        log("[chase-today] DRY-RUN — preview:")

    for i, (email, bids) in enumerate(contacts, 1):
        contact_name = contact_meta[email]["contact_name"]
        subj, body = build_email(contact_name, bids)
        log(f"\n[{i}/{len(contacts)}] → {email}  ({contact_name})  "
            f"{len(bids)} bid(s)  max-age={max(b['age'] for b in bids)}d")
        log(f"          SUBJ: {subj[:90]}")
        for b in bids:
            log(f"          • {b['bid_id']}  {b['project'][:42]:<44}  "
                f"sub={b['sub_date']}  ${b['amount']:,}")
        if not args.apply:
            continue

        cmd = [PYEXE, str(SEND_EMAIL), "--to", email, "--cc", CC_INTERNAL,
               "--subject", subj, "--body", body, "--no-signature"]
        # Pass first bid's Internal ID so subject gets [ID:xxxxxxxx] tag for
        # shift-immune reply routing. If multiple bids are grouped, this tags
        # the lead bid; other bids are matched by project-name fallback in the
        # reply processor.
        lead_iid = next((b.get("internal_id") for b in bids if b.get("internal_id")), None)
        if lead_iid:
            cmd.extend(["--internal-id", lead_iid])
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=90)
            ok = (r.returncode == 0)
        except Exception as e:
            log(f"          ✗ EXCEPTION: {e}")
            continue
        if ok:
            log(f"          ✓ sent")
            # Log each bid as a chase history entry
            now_iso = datetime.now().isoformat(timespec="seconds")
            for b in bids:
                history.append({
                    "slug": re.sub(r"[^a-z0-9-]","-", b["project"].lower())[:80],
                    "from": "chase", "to": "chase",
                    "trigger": "chase_today",
                    "to_email": email,
                    "subject": subj[:80],
                    "at": now_iso,
                    "bid_id": b["bid_id"],
                })
            BS_FILE.write_text(json.dumps({"overrides": bs.get("overrides",{}),
                                            "history": history}, indent=2),
                                encoding="utf-8")
        else:
            log(f"          ✗ send failed: stdout={(r.stdout or '')[:200]}  "
                f"stderr={(r.stderr or '')[:200]}")

        # Spacing (skip after last)
        if i < len(contacts):
            log(f"          sleeping {args.interval}s until next…")
            time.sleep(args.interval)

    log("\n[chase-today] DONE.")


if __name__ == "__main__":
    main()
