#!/usr/bin/env python3
r"""
execute_approved_chases.py — Send the chases listed in today's approved plan.

Reads `data/proposed_chases/proposed_chases_YYYY-MM-DD.json` (written by
morning_chase_report.py) and sends each entry via send_email.py with:
  - 15-min spacing between sends
  - [ID:xxxxxxxx] Internal-ID tag in subject
  - CC list: cs@ccf + accountant + owner + CRM CC Contacts column
  - Global chase-batch lock (refuses if any other chase pipeline alive)
  - Single-instance script lock (refuses if THIS script is already running)

Run AFTER user approves the morning brief plan:
  python scripts/execute_approved_chases.py             # uses today's plan
  python scripts/execute_approved_chases.py --date 2026-05-26
  python scripts/execute_approved_chases.py --dry-run   # preview only
  python scripts/execute_approved_chases.py --spacing 600  # 10 min instead of 15
"""
from __future__ import annotations
import argparse, json, os, subprocess, sys, time
from datetime import datetime, date
from pathlib import Path

try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    pass

SEND_EMAIL = ROOT / "scripts" / "send_email.py"
PLAN_DIR = ROOT / "data" / "proposed_chases"
LOG_FILE = ROOT / "data" / "logs" / "execute_approved_chases.log"

# Standing internal CC: the team estimating inbox plus any internal aliases
# (accountant / owner) configured in the gitignored .env.
CC_INTERNAL = ",".join(
    [os.environ.get("CCF_INTERNAL_CC", "cs@carolinacommercialfinishes.com")]
    + [a.strip() for a in os.environ.get("OWNER_ALIAS_EMAILS", "").split(",") if a.strip()]
)


def log(msg: str, quiet: bool = False):
    if not quiet:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now().isoformat(timespec='seconds')}  {msg}\n")


def tg_ping(text: str):
    try:
        import urllib.request, urllib.parse
        tok = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat = os.environ.get("USER_TELEGRAM_CHAT_ID", "")
        if not (tok and chat): return
        data = urllib.parse.urlencode({
            "chat_id": chat, "text": text, "parse_mode": "Markdown",
        }).encode()
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{tok}/sendMessage", data=data), timeout=10)
    except Exception:
        pass


def build_body(p: dict) -> tuple[str, str]:
    """Default chase body. Specific to the bid's project / amount / reason."""
    proj = p.get("project", "this project")
    amt = p.get("amount", 0)
    bid_id = p.get("bid_id", "")
    sub_date = p.get("submitted", "")
    reason = p.get("reason", "")
    amt_str = f"USD {amt:,}" if amt else "(amount on file)"
    subject = f"Follow-Up: {proj} ({bid_id})"
    body = (
        f"Hi,\n\n"
        f"Following up on our proposal for {proj} ({bid_id}).\n\n"
        f"  Bid amount: {amt_str}\n"
        f"  Submitted:  {sub_date}\n\n"
        f"Wanted to check the status and whether the project is still active. "
        f"Happy to revise pricing or clarify scope if anything has shifted on your end.\n\n"
        f"Thanks,\n"
    )
    return subject, body


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None,
                    help="Plan date (default: today)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--spacing", type=int, default=900,
                    help="Seconds between sends (default 900 = 15 min)")
    ap.add_argument("--start-from", type=int, default=1)
    args = ap.parse_args()

    plan_date = (datetime.strptime(args.date, "%Y-%m-%d").date()
                 if args.date else date.today())
    plan_file = PLAN_DIR / f"proposed_chases_{plan_date.isoformat()}.json"
    if not plan_file.exists():
        log(f"No plan found at {plan_file}. Run morning_chase_report.py first.")
        return 1

    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    proposed = plan.get("proposed", [])
    if not proposed:
        log(f"Plan exists but proposed list is empty — nothing to send.")
        return 0

    # GLOBAL chase-batch lock — only one chase pipeline alive at a time.
    if not args.dry_run:
        try:
            from _lib.chase_batch_lock import acquire as _acq, release as _rel
            if not _acq(__file__):
                log("ABORT — another chase batch is alive. Lock contents:")
                lf = ROOT / "data" / "CHASE_BATCH.lock"
                if lf.exists(): log(lf.read_text())
                return 2
            import atexit; atexit.register(_rel)
        except ImportError as e:
            log(f"ABORT — chase_batch_lock unavailable: {e}")
            return 3

    todo = proposed[args.start_from - 1:]
    log(f"{'DRY-RUN' if args.dry_run else 'LIVE'} — {len(todo)} send(s), "
        f"{args.spacing // 60}-min spacing, "
        f"ETA finish in ~{len(todo) * args.spacing // 60} min")
    if not args.dry_run:
        tg_ping(f"🚀 *Executing approved chases — {len(todo)} email(s)*\n"
                f"{args.spacing // 60}-min spacing")

    sent, failed = 0, []
    for i, p in enumerate(todo, start=args.start_from):
        subject, body = build_body(p)
        log(f"\n[{i}/{len(proposed)}] {p['bid_id']} {p['project'][:40]}")
        log(f"   TO:   {p['to']}")
        log(f"   IID:  {p.get('internal_id', '(none)')}")
        log(f"   $:    ${p.get('amount', 0):,}")
        log(f"   why:  {p.get('reason', '')}")

        if args.dry_run:
            sent += 1
            continue

        cmd = [sys.executable, str(SEND_EMAIL),
               "--to", p["to"], "--cc", CC_INTERNAL,
               "--subject", subject, "--body", body]
        if p.get("internal_id"):
            cmd.extend(["--internal-id", p["internal_id"]])

        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=90)
            ok = ('"status": "sent"' in (r.stdout or "")) or r.returncode == 0
        except Exception as e:
            ok = False
            log(f"   EXCEPTION: {e}")

        if ok:
            log(f"   ✅ sent")
            sent += 1
            tg_ping(f"✅ *[{i}/{len(proposed)}]* {p['bid_id']} {p['project'][:30]} → {p['to']}")
        else:
            log(f"   ❌ FAILED")
            log(f"      stderr: {(r.stderr or '')[:200]}")
            failed.append(p["bid_id"])
            tg_ping(f"❌ *[{i}/{len(proposed)}]* {p['bid_id']} — send FAILED")

        if i < args.start_from + len(todo) - 1:
            log(f"   sleeping {args.spacing // 60} min until next send...")
            time.sleep(args.spacing)

    log(f"\nDONE — sent {sent}/{len(todo)}, failed {len(failed)}")
    if not args.dry_run:
        tg_ping(f"🏁 *Chase batch complete*\n"
                f"Sent {sent}/{len(todo)}"
                + (f"\nFailed: {', '.join(failed[:5])}" if failed else ""))
    return 0 if not failed else 2


if __name__ == "__main__":
    # RETIRED 2026-06-16 (god-level rebuild): collapsed to ONE pipeline.
    import sys as _sys
    if "--force-legacy" not in _sys.argv:
        print("RETIRED: use the single pipeline — morning_chase_report.py (decide) -> "
              "chase_executor.py (send, APPROVAL-GATED). This legacy sender no longer "
              "fires; pass --force-legacy only if you know why.")
        raise SystemExit(0)
    sys.exit(main())
