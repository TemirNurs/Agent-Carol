#!/usr/bin/env python3
r"""
followup_batch.py — Send follow-up emails to ALL bids matching a status, in one call.

This is the script Carol should run when the user says "follow up all X status projects"
or "follow up all Bid Submitted projects" or "follow up all the rest."

Why this exists: Carol's small model is unreliable at multi-step orchestration
(loop through 12 bids → draft each → send each → report). One single-call script
turns 28 steps into 1 step, which the model can handle.

What it does:
  1. Pulls all bids from CRM matching --status (default 'Bid Submitted')
  2. Optionally exclude specific bid IDs via --skip BID-NNNN,BID-MMMM
  3. For each: draft via Gemini Flash → send via SMTP
  4. Report summary

Usage:
  python scripts/followup_batch.py --status "Bid Submitted"
  python scripts/followup_batch.py --status "Awaiting Decision" --skip BID-0005,BID-0006
  python scripts/followup_batch.py --status "Bid Submitted" --dry-run
  python scripts/followup_batch.py --status "Bid Submitted" --year 2026

By default RUNS the sends. Pass --dry-run to only show what would happen.
"""

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

PYEXE = sys.executable


def get_bids_by_status(status: str, year: int | None = None) -> list[dict]:
    """Pull live bids from CRM matching status."""
    from crm_stats import submitted_bid_stats
    s = submitted_bid_stats(year=year, status_filter=status)
    return s.get("submitted_in_year", [])


def _find_contact_from_gmail(project_name: str) -> dict | None:
    """Search Gmail Inbox for the project name; return most recent non-internal sender."""
    import imaplib, email as elib, os
    if not project_name: return None
    GMAIL_USER = os.environ.get("GMAIL_USER", "estimates@carolinacommercialfinishes.com")
    GMAIL_PASS = os.environ.get("GMAIL_APP_PASSWORD", "")
    try:
        M = imaplib.IMAP4_SSL("imap.gmail.com")
        M.login(GMAIL_USER, GMAIL_PASS)
        M.select("INBOX")
    except Exception:
        return None
    # Use a single distinctive token from the project name
    tokens = [t for t in re.findall(r"[A-Za-z0-9]+", project_name) if len(t) >= 5]
    if not tokens:
        try: M.logout()
        except Exception: pass
        return None
    keyword = tokens[0]
    try:
        typ, data = M.search(None, "X-GM-RAW", keyword)
    except Exception:
        try: M.logout()
        except Exception: pass
        return None
    if typ != "OK" or not data[0]:
        try: M.logout()
        except Exception: pass
        return None
    # Look at the most recent matches; pick first non-internal sender
    ids = data[0].split()[-15:]
    for mid in reversed(ids):
        typ, raw = M.fetch(mid, '(BODY.PEEK[])')
        if typ != "OK" or not raw or not raw[0]: continue
        msg = elib.message_from_bytes(raw[0][1])
        fr = msg.get("From", "") or ""
        fl = fr.lower()
        if any(s in fl for s in ("estimates@carolinacommercial", "cs@carolinacommercial",
                                  "mailer-daemon", "noreply", "donotreply",
                                  "isqftmail", "buildingconnected.com",
                                  "constructconnect")):
            continue
        # Extract name + email
        m = re.match(r'\s*"?(.+?)"?\s*<([^>]+)>\s*$', fr)
        if m:
            name, email = m.group(1).strip(), m.group(2).strip()
        else:
            email = fr.strip()
            name = ""
        # Validate
        if "@" not in email: continue
        domain = email.rsplit("@", 1)[-1]
        if "." not in domain or len(domain.rsplit(".", 1)[-1]) < 2: continue
        try: M.logout()
        except Exception: pass
        return {"name": name, "email": email}
    try: M.logout()
    except Exception: pass
    return None


def has_valid_email(addr: str) -> bool:
    if not addr: return False
    a = addr.strip()
    if "@" not in a: return False
    domain = a.rsplit("@", 1)[-1]
    return "." in domain and len(domain.rsplit(".", 1)[-1]) >= 2


def draft_one(bid_id: str, email_type: str = "follow-up") -> dict:
    """Run draft_email.py for one bid, return parsed JSON."""
    r = subprocess.run(
        [PYEXE, str(ROOT / "scripts" / "draft_email.py"),
         "--bid", bid_id, "--type", email_type, "--json"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    if r.returncode != 0:
        return {"error": "draft script failed", "stderr": (r.stderr or "")[:200]}
    try:
        return json.loads(r.stdout)
    except Exception as e:
        return {"error": f"draft JSON parse failed: {e}", "raw": r.stdout[:200]}


def send_one(to: str, subject: str, body: str) -> dict:
    """Run send_email.py for one draft, return parsed JSON."""
    # Normalize multi-email field (space-separated → comma-separated)
    to_clean = re.sub(r"\s+", ",", to.strip()).strip(",")
    r = subprocess.run(
        [PYEXE, str(ROOT / "scripts" / "send_email.py"),
         "--to", to_clean, "--subject", subject, "--body", body],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    out = r.stdout or ""
    if '"status": "sent"' in out:
        return {"status": "sent", "to": to_clean}
    if "mangled_dollars" in out:
        return {"status": "refused", "reason": "mangled_dollars", "to": to_clean}
    if "bad_recipient" in out:
        return {"status": "refused", "reason": "bad_recipient", "to": to_clean}
    return {"status": "error", "raw": out[:200], "to": to_clean}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", required=True,
                    help="Status to filter (e.g. 'Bid Submitted', 'Awaiting Decision')")
    ap.add_argument("--year", type=int, default=None,
                    help="Optional year filter (matches Bid Submitted Date)")
    ap.add_argument("--skip", default="",
                    help="Comma-separated Bid IDs to skip (e.g. BID-0005,BID-0006)")
    ap.add_argument("--type", default="follow-up",
                    choices=["follow-up", "clarification", "thank-you", "revised-proposal"])
    ap.add_argument("--dry-run", action="store_true",
                    help="Show drafts without sending")
    args = ap.parse_args()

    skip_set = {s.strip().upper() for s in args.skip.split(",") if s.strip()}

    bids = get_bids_by_status(args.status, year=args.year)
    print(f"[batch] {len(bids)} bid(s) with status '{args.status}'"
          + (f" in {args.year}" if args.year else ""))
    if skip_set:
        bids = [b for b in bids if (b.get("bid_id") or "").upper() not in skip_set]
        print(f"[batch] {len(bids)} after skipping {sorted(skip_set)}")

    # Pre-validate contact emails so we know how many will be sent
    # If the CRM has no Contact Email, fall back to searching Gmail for the
    # original ITB sender. Don't lazy-skip.
    valid_bids = []
    skipped_no_email = []
    crm_fixes = []
    for b in bids:
        from crm_lib import get_sheet
        if not hasattr(main, "_recs"):
            main._recs = get_sheet("Bid Log").get_all_records()
        row = next((r for r in main._recs if r.get("Bid #") == b.get("bid_id")), None)
        contact_email = (row.get("Contact Email") or "").strip() if row else ""
        first_email = re.split(r"\s+", contact_email)[0] if contact_email else ""
        if has_valid_email(first_email):
            valid_bids.append(b)
            continue

        # Fallback: search Gmail for the project name and pull the most recent
        # inbound non-internal sender as the contact.
        found = _find_contact_from_gmail(b.get("name", ""))
        if found:
            print(f"[batch] {b.get('bid_id')} had no Contact Email; found {found['email']} in Gmail history")
            crm_fixes.append((b.get("bid_id"), found["name"], found["email"]))
            valid_bids.append(b)
        else:
            skipped_no_email.append(b)

    # Apply any CRM fixes discovered (so CRM stays correct for next run)
    if crm_fixes:
        try:
            from gspread.utils import rowcol_to_a1
            ws = get_sheet("Bid Log")
            headers = ws.row_values(1)
            bid_col = headers.index("Bid #") + 1
            name_col = headers.index("Contact Name") + 1
            email_col = headers.index("Contact Email") + 1
            bid_ids_col = ws.col_values(bid_col)
            updates = []
            for bid, name, email in crm_fixes:
                row = bid_ids_col.index(bid) + 1
                updates.append({"range": rowcol_to_a1(row, name_col),  "values": [[name]]})
                updates.append({"range": rowcol_to_a1(row, email_col), "values": [[email]]})
            if updates:
                ws.batch_update(updates, value_input_option="USER_ENTERED")
                print(f"[batch] CRM updated with {len(crm_fixes)} discovered contact(s)")
                # refresh cached records
                main._recs = ws.get_all_records()
        except Exception as e:
            print(f"[batch] CRM update failed: {e}")

    if skipped_no_email:
        print(f"[batch] {len(skipped_no_email)} bid(s) STILL have no contact (Gmail search also failed):")
        for b in skipped_no_email:
            print(f"     - {b.get('bid_id')} {(b.get('name') or '')[:50]}")

    if not valid_bids:
        print("[batch] nothing to send.")
        return 0

    print(f"[batch] processing {len(valid_bids)} bid(s){' (DRY RUN)' if args.dry_run else ''}")

    results = []
    for i, b in enumerate(valid_bids, 1):
        bid_id = b.get("bid_id", "")
        name = (b.get("name") or "")[:40]
        print(f"\n[{i}/{len(valid_bids)}] {bid_id}  {name}")
        draft = draft_one(bid_id, args.type)
        if "error" in draft:
            print(f"   DRAFT FAIL: {draft.get('error')}")
            results.append({"bid_id": bid_id, "status": "draft_failed", "error": draft.get("error")})
            continue
        print(f"   to: {draft.get('to','?')}")
        print(f"   subj: {draft.get('subject','')[:65]}")
        if args.dry_run:
            results.append({"bid_id": bid_id, "status": "dry_run",
                            "to": draft.get("to"), "subject": draft.get("subject")})
            continue
        send_result = send_one(draft["to"], draft["subject"], draft["body"])
        marker = "[OK]" if send_result.get("status") == "sent" else "[FAIL]"
        print(f"   {marker} {send_result.get('status')}"
              + (f" — {send_result.get('reason')}" if send_result.get("reason") else ""))
        results.append({"bid_id": bid_id, **send_result})
        time.sleep(1)  # polite pacing

    # Summary
    print("\n" + "=" * 50)
    print("SUMMARY")
    print("=" * 50)
    sent = sum(1 for r in results if r.get("status") == "sent")
    refused = sum(1 for r in results if r.get("status") == "refused")
    failed = sum(1 for r in results if r.get("status") in ("draft_failed", "error"))
    dry = sum(1 for r in results if r.get("status") == "dry_run")
    if not args.dry_run:
        print(f"  Sent: {sent}/{len(valid_bids)}")
        if refused: print(f"  Refused (preflight): {refused}")
        if failed:  print(f"  Failed: {failed}")
    else:
        print(f"  Drafted (dry-run): {dry}/{len(valid_bids)}")
    if skipped_no_email:
        print(f"  Skipped (no email): {len(skipped_no_email)}")
    for r in results:
        flag = "[OK]" if r.get("status") == "sent" else \
               "[--]" if r.get("status") == "dry_run" else "[X]"
        print(f"  {flag} {r['bid_id']}  {r.get('status','?')}"
              + (f" ({r.get('reason','')})" if r.get("reason") else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
