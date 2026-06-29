#!/usr/bin/env python3
"""
LLM-based due-date backfill for email-sourced bids that have no due_date.

For each bid in active_bids.json where source="email" and due_date is empty,
re-fetches the original email body from Gmail (by matching subject) and asks
Gemini 2.5 Flash to extract the bid due date. Updates active_bids.json in-place.

Also captures SF (square footage), location, and project type when Gemini sees
them clearly.

Usage:
  python scripts/llm_extract_due_dates.py              # backfill all missing
  python scripts/llm_extract_due_dates.py --dry-run    # show what would change
  python scripts/llm_extract_due_dates.py --limit 5    # process only first 5

Cost: ~$0.002 per email (Gemini 2.5 Flash free/paid tier).
"""

import argparse
import imaplib
import email as email_lib
import json
import os
import re
import sys
from datetime import date, datetime
from email.header import decode_header
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

BASE = Path(__file__).resolve().parent.parent
BIDS_FILE = BASE / "data" / "memory" / "active_bids.json"
GMAIL_USER = os.environ.get("GMAIL_USER", "estimates@carolinacommercialfinishes.com")
GMAIL_PASS = os.environ.get("GMAIL_APP_PASSWORD", "")

SYSTEM_PROMPT = """You are a construction bid coordinator. Extract structured fields
from a bid invitation email. Return ONLY valid JSON, no markdown. If a field isn't
present, use null. Fields:

{
  "due_date": "MM/DD/YYYY",          // bid/proposal submission deadline
  "sf": 12345,                        // total square footage if stated
  "city": "City name",
  "state": "ST",                     // 2-letter state code
  "project_type": "short label",     // e.g. "Retail", "Hotel", "School"
  "scope_notes": "one sentence"      // paint/finish scope if clearly stated
}

Rules:
- due_date: preserve exact MM/DD/YYYY. Convert phrases like "by 4/24" to "4/24/2026".
- If the email is a FORWARD, find the innermost original invitation and use THAT due date.
- If multiple dates are mentioned, pick the BID SUBMISSION deadline, not pre-bid meetings.
- If the email body is empty or clearly not a bid invite, return all nulls.
"""


def _fetch_email_body(imap, subject):
    """Find the email by subject and return the first 10000 chars of plain text body.
    Uses a short distinctive token from the subject rather than the full string —
    IMAP subject matches are substring-based and long strings with special chars fail.
    """
    # Strip colons, dashes at end, normalize whitespace
    s = re.sub(r"\s+", " ", subject or "").strip().rstrip(":- ")
    # Use first distinctive chunk (first 3-4 words or up to first colon/dash)
    # to widen the match window
    m = re.match(r"([\w&#'.]+(?:\s+[\w&#'.]+){0,4})", s)
    token = m.group(1) if m else s[:30]
    token = re.sub(r'["\\]', ' ', token)[:40].strip()
    if not token:
        return ""
    q = f'(SUBJECT "{token}")'
    try:
        status, ids = imap.search(None, q)
        if status != "OK" or not ids[0]:
            return ""
        # Pick most recent
        mid = ids[0].split()[-1]
        status, data = imap.fetch(mid, '(BODY.PEEK[])')
        if status != "OK":
            return ""
        msg = email_lib.message_from_bytes(data[0][1])
        body = ""
        html_body = ""
        if msg.is_multipart():
            for p in msg.walk():
                ct = p.get_content_type()
                if ct == "text/plain" and not body:
                    try:
                        body = p.get_payload(decode=True).decode("utf-8", errors="replace")
                    except Exception:
                        pass
                elif ct == "text/html" and not html_body:
                    try:
                        html_body = p.get_payload(decode=True).decode("utf-8", errors="replace")
                    except Exception:
                        pass
        else:
            try:
                payload = msg.get_payload(decode=True).decode("utf-8", errors="replace")
                if msg.get_content_type() == "text/html":
                    html_body = payload
                else:
                    body = payload
            except Exception:
                pass
        # Fallback: many ITB emails (iSqFt, BuildingConnected) are HTML-only.
        # If we have no plaintext but have HTML, strip tags + use that.
        if not body and html_body:
            html_body = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ",
                               html_body, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r"<[^>]+>", " ", html_body)
            text = re.sub(r"&nbsp;", " ", text)
            text = re.sub(r"&amp;", "&", text)
            text = re.sub(r"&#\d+;", "", text)
            text = re.sub(r"\s+", " ", text).strip()
            body = text
        return body[:10000]
    except Exception:
        return ""


def llm_extract(project_name, email_body):
    """Call LLM (Groq primary, Gemini fallback), return dict of extracted fields."""
    try:
        import litellm
    except ImportError:
        print("[error] pip install litellm", file=sys.stderr)
        return {}

    # Primary: Groq llama-3.3-70b (free, fast, higher TPM than Gemini free tier)
    # Fallback: Gemini 2.5 Flash free tier
    candidates = []
    if os.environ.get("GROQ_API_KEY"):
        candidates.append("groq/llama-3.3-70b-versatile")
    if os.environ.get("GEMINI_API_KEY"):
        candidates.append("gemini/gemini-2.5-flash")
    if not candidates:
        print("[error] no API key for Groq or Gemini", file=sys.stderr)
        return {}

    # Truncate body to fit Groq's 12k TPM cap (roughly 6k tokens of body = 24k chars)
    body_trimmed = email_body[:6000]
    user_msg = f"Project name: {project_name}\n\nEmail body:\n{body_trimmed}"

    for model in candidates:
        try:
            r = litellm.completion(
                model=model,
                max_tokens=300,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
            )
            text = r.choices[0].message.content.strip()
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
            m = re.search(r"\{[\s\S]*\}", text)
            if m:
                return json.loads(m.group(0))
            return {}
        except Exception as e:
            err = str(e).lower()
            # Retry on next model only for rate-limit / quota errors
            if any(t in err for t in ("429", "rate limit", "quota", "too many", "overloaded", "capacity")):
                print(f"[llm] {model} rate-limited, trying fallback", file=sys.stderr)
                continue
            print(f"[llm] {model}: {e}", file=sys.stderr)
            return {}
    return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="Only process first N (0=all)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    bids = json.load(open(BIDS_FILE, encoding="utf-8"))
    candidates = [b for b in bids if not b.get("due_date") and b.get("source") == "email"]
    print(f"[backfill] {len(candidates)} email-sourced bids need due dates")
    if args.limit:
        candidates = candidates[:args.limit]
        print(f"[backfill] limiting to first {len(candidates)}")

    if not candidates:
        print("[backfill] nothing to do.")
        return

    # Open single IMAP connection
    imap = imaplib.IMAP4_SSL("imap.gmail.com")
    imap.login(GMAIL_USER, GMAIL_PASS)
    imap.select('"[Gmail]/All Mail"')

    import time as _time
    updated = 0
    skipped = 0
    last_call = 0
    MIN_INTERVAL = 3.0  # seconds between LLM calls to stay under Groq 12k TPM
    for b in candidates:
        name = b.get("project_name", "")
        body = _fetch_email_body(imap, name)
        if not body:
            print(f"  SKIP (no email body found): {name[:55]}")
            skipped += 1
            continue
        # Throttle to Groq's rate
        elapsed = _time.time() - last_call
        if elapsed < MIN_INTERVAL:
            _time.sleep(MIN_INTERVAL - elapsed)
        last_call = _time.time()
        data = llm_extract(name, body)
        due = data.get("due_date")
        # Validate date shape
        if due and not re.match(r"^\d{1,2}/\d{1,2}/\d{4}$", due):
            due = None
        if not due:
            if args.verbose:
                print(f"  (no date extracted): {name[:55]}  LLM_returned={data}")
            skipped += 1
            continue
        # Also backfill city/state/sf if missing
        changes = [f"due={due}"]
        if not b.get("city") and data.get("city"):
            b["city"] = data["city"]; changes.append(f"city={data['city']}")
        if not b.get("state") and data.get("state"):
            b["state"] = data["state"]; changes.append(f"state={data['state']}")
        if not b.get("sf") and data.get("sf"):
            b["sf"] = data["sf"]; changes.append(f"sf={data['sf']}")
        b["due_date"] = due
        updated += 1
        print(f"  OK  {name[:55]:55}  {'  '.join(changes)}")

    imap.logout()

    print(f"\n[backfill] updated={updated}  skipped={skipped}")
    if args.dry_run:
        print("[dry-run] no changes written.")
        return

    if updated:
        with open(BIDS_FILE, "w", encoding="utf-8") as f:
            json.dump(bids, f, indent=2, ensure_ascii=False)
        print(f"[backfill] saved to {BIDS_FILE.name}")


if __name__ == "__main__":
    main()
