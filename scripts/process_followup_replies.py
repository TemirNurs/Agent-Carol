#!/usr/bin/env python3
r"""
process_followup_replies.py — Auto-classify Inbox replies to our follow-ups
and update the CRM Bid Log.

How it works:
  1. Searches Gmail Inbox for replies to our follow-up emails (RE: subject pattern,
     or replies to threads where we wrote "Follow-Up:")
  2. For each reply, extracts the BID-NNNN reference from subject/body
  3. Uses Gemini 2.5 Flash to classify:
       LOST          — GC says we didn't get it / went with another / unawarded
       WON           — GC says we won / awarded / signed
       PRICING       — GC says our number was high/low; project still alive
       STILL_AWAITING — generic "no update yet"
       OUT_OF_OFFICE — auto-reply
       UNCLEAR       — anything ambiguous
  4. Updates the CRM:
       LOST  → Status="Lost",  Loss Reason=<reason>,  Notes+=excerpt
       WON   → Status="Won",   Notes+=excerpt (Contract Value left blank for user)
       PRICING/STILL_AWAITING/OUT_OF_OFFICE/UNCLEAR → Notes only (no Status change)
  5. Tracks processed messages in state.json so replies aren't re-processed
  6. Telegram ping summarizing changes

Safety: Status changes (Lost/Won) only on HIGH confidence from the classifier.
Loss Reason is APPENDED to existing reasons, never overwritten.

Usage:
  python scripts/process_followup_replies.py
  python scripts/process_followup_replies.py --since-days 7
  python scripts/process_followup_replies.py --dry-run
  python scripts/process_followup_replies.py --quiet
"""

import argparse
import email as email_lib
import imaplib
import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from email.header import decode_header
from email.utils import parsedate_to_datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

STATE_FILE = ROOT / "data" / "memory" / "followup_replies_state.json"
LOG_FILE   = ROOT / "data" / "logs" / "followup_replies.log"

GMAIL_USER = os.environ.get("GMAIL_USER", "estimates@carolinacommercialfinishes.com")
GMAIL_PASS = os.environ.get("GMAIL_APP_PASSWORD", "")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.environ.get("USER_TELEGRAM_CHAT_ID", "")


CLASSIFY_SYSTEM_PROMPT = """You are a CRM update assistant for Carolina Commercial Finishes (a commercial painting subcontractor). You read inbound replies from General Contractors to follow-up emails we sent about open bids, and you classify each reply.

Your job: classify the reply and extract a structured summary. Reply with ONLY valid JSON.

Categories:
  LOST          — GC explicitly says WE (the painter) did NOT win the painting scope. Examples: "We weren't awarded this project (i.e. didn't pick CCF)", "We went with another sub", "We selected a different painting bid", "Awarded the painting to X".
  WON           — GC explicitly says WE (the painter / CCF) won / are selected / are awarded the painting scope. Examples: "Congrats, you got it", "We'd like to award you the project", "We're going with your bid".

  ⚠️ CRITICAL DISTINCTION — pronouns are tricky. Read the WHOLE message:
  (a) GC says they (the GC) won the prime contract and gives no other update:
      → "MBI was awarded this project" / "We were awarded the project" / "We won the bid" / "We got the prime" / "Owner selected us" / "give us a few weeks to sort this out" / "now picking subs" / "We should receive the award any day now"
      → Classify STILL_AWAITING (painting sub-award is still pending).
  (b) COMPOUND replies — GC won prime BUT we (CCF) lost the painting scope. Classify LOST:
      → "We have been awarded this project, although we decided to go with another contractor for the painting scope"
      → "We won the prime but went with a different painter"
      → "Project is moving forward but we awarded the painting to X"
      → Bid tab / leveling sheet showing another sub as selected painter → LOST.
  (c) GC says THEY (the GC) did NOT win the prime → STILL classify as LOST (no sub-award possible):
      → "We were not awarded this project" / "We didn't get this one" / "We weren't selected for the project" / "Owner went with another GC" / "Project went to another contractor"
      → This applies BEFORE asking about painting specifically — if the GC themselves is out, our painting bid is dead too.
  (d) GC project AWARDED but to a DIFFERENT GC than the one inviting us (rare since the inviting GC IS the one who'd hire us):
      → "Project has been Awarded to [Company X]" — if X is the inviting GC's company name, that's STILL_AWAITING for our sub-bid. If X is a DIFFERENT company, that's LOST for the inviting GC AND for us.
  (e) GC says they're still pre-contract / project status moving but not awarded:
      → "Still working towards a contract" / "Still reviewing" / "Thank you for submitting a budget. DD level drawings so we..." / "Owner hasn't decided" / "looping in [someone]"
      → STILL_AWAITING.
  (f) Only classify LOST for the painting scope itself when the message clearly indicates THE PAINTING SCOPE went to a DIFFERENT subcontractor or was rejected for cost / scope reasons.
  PRICING       — GC indicates our number is high/low/needs revision but project is still ALIVE. Examples: "Your pricing is 10% higher than budget", "Can you revise?", "We're a bit off on the numbers".
  STILL_AWAITING — Generic "no update yet" / "still waiting on owner" / "decision not made". Project is alive, no new info.
  OUT_OF_OFFICE — Auto-reply / out of office / vacation. No real update.
  UNCLEAR       — Ambiguous, requires human review.

Output JSON schema:
{
  "category": "LOST|WON|PRICING|STILL_AWAITING|OUT_OF_OFFICE|UNCLEAR",
  "confidence": 0.0-1.0,
  "loss_reason": "short reason (only if LOST or PRICING)",
  "summary": "1-line summary of the reply for CRM Notes",
  "key_quote": "verbatim sentence from the reply that justifies the category (max 120 chars)"
}

Rules:
- Only set category=LOST or category=WON if confidence >= 0.85.
- For LOST: write loss_reason as a short canonical phrase ("Came 2nd", "Pricing 10% high", "GC went with another", "GC lost project", "Project cancelled", etc.)
- For PRICING: include the percentage or amount if mentioned.
- summary should be a CRM-friendly 1-liner (max 100 chars), past tense, neutral tone.
- DO NOT invent details. If the reply is short and ambiguous, use UNCLEAR.
"""


def log(msg: str, quiet: bool = False):
    if not quiet:
        print(msg)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now().isoformat(timespec='seconds')}  {msg}\n")


def load_state() -> dict:
    if STATE_FILE.exists():
        try: return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception: pass
    return {"processed_message_ids": []}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def decode_subject(s):
    if not s: return ""
    out = ""
    for p, e in decode_header(str(s)):
        if isinstance(p, bytes):
            try:
                out += p.decode(e or "utf-8", errors="replace")
            except LookupError:
                out += p.decode("utf-8", errors="replace")
        else:
            out += p
    return re.sub(r"\s+", " ", out).strip()


def _html_to_text_local(html: str) -> str:
    s = re.sub(r"<style[^>]*>.*?</style>", " ", html, flags=re.S | re.I)
    s = re.sub(r"<script[^>]*>.*?</script>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</p>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    for ent, rep in [("&nbsp;"," "),("&amp;","&"),("&lt;","<"),("&gt;",">"),("&quot;",'"'),("&#39;","'")]:
        s = s.replace(ent, rep)
    return re.sub(r"\n\s*\n+", "\n\n", s).strip()


def get_body_text(msg) -> str:
    """Extract reply body. Prefers text/plain; falls back to text/html→text
    when plain is empty (Outlook web etc. sometimes send HTML-only)."""
    text_plain = ""
    text_html = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain" and not text_plain:
                payload = part.get_payload(decode=True)
                if payload:
                    try: text_plain = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                    except (LookupError, AttributeError): text_plain = payload.decode("utf-8", errors="replace")
            elif ct == "text/html" and not text_html:
                payload = part.get_payload(decode=True)
                if payload:
                    try: text_html = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                    except (LookupError, AttributeError): text_html = payload.decode("utf-8", errors="replace")
    else:
        ct = msg.get_content_type()
        payload = msg.get_payload(decode=True)
        if payload:
            try: decoded = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
            except (LookupError, AttributeError): decoded = payload.decode("utf-8", errors="replace")
            if ct == "text/html": text_html = decoded
            else: text_plain = decoded
    body = text_plain.strip() or _html_to_text_local(text_html) if text_html else text_plain
    body = re.sub(r"\r\n?", "\n", body)
    # Strip quoted reply blocks (lines starting with >)
    lines = body.split("\n")
    keep = []
    for ln in lines:
        if ln.lstrip().startswith(">"): continue
        # Stop at common quote markers
        if re.match(r"^On .+wrote:$", ln.strip()): break
        if "From:" in ln and "estimates@carolinacommercialfinishes.com" in ln: break
        keep.append(ln)
    body = "\n".join(keep).strip()
    return body[:3000]


def find_bid_id_in_text(text: str) -> str | None:
    """Pull BID-NNNN reference from subject or body."""
    m = re.search(r"\bBID[\s\-]?(\d{3,4})\b", text, re.IGNORECASE)
    if m:
        return f"BID-{int(m.group(1)):04d}"
    return None


def resolve_internal_id(sender: str, subject: str, body: str, cache: dict) -> str | None:
    """STABLE reply→CRM resolver. Returns the Internal ID (UUID) of the bid
    this reply is about — NEVER the Bid#, which shifts every time the sheet
    is re-sorted (that's why manual reconciliation was needed).

    Strategy (most → least reliable):
      1. Match sender email against CRM 'Contact Email' (live data) + verify a
         distinctive project-name token from the subject appears in the row's
         project name. This is fully Bid#-shift immune.
      2. If a BID-NNNN token is in the subject AND that row's project token
         ALSO appears in the subject, accept it (cross-validated).
      3. Single sender-email match with no other candidate → take it.
    Returns internal_id string, or None if nothing trustworthy matched.
    """
    em = re.search(r"<([^>]+)>", sender or "")
    sender_email = (em.group(1) if em else (sender or "")).strip().lower()
    if "@" not in sender_email:
        return None
    sender_domain = sender_email.split("@", 1)[1]
    subj_l = (subject or "").lower()

    STOP = {"food", "lion", "store", "the", "and", "inc", "llc", "corp", "for",
            "follow", "bid", "ccf", "proposal", "painting", "project", "re",
            "fwd", "submission", "quinton", "chester", "building", "buildings"}

    def proj_tokens(name):
        return [t for t in re.findall(r"[a-z0-9]{3,}", (name or "").lower())
                if t not in STOP]

    exact, domain_only = [], []
    for row_idx, rec in cache.get("rows_idx", []):
        iid = str(rec.get("Internal ID") or "").strip()
        if not iid:
            continue
        ce = (rec.get("Contact Email") or "").lower()
        if sender_email and sender_email in ce:
            exact.append((row_idx, rec, iid))
        elif sender_domain and sender_domain in ce:
            domain_only.append((row_idx, rec, iid))

    pool = exact or domain_only
    if not pool:
        return None
    if len(pool) == 1:
        return pool[0][2]

    # Disambiguate by a distinctive project token appearing in the subject
    for row_idx, rec, iid in pool:
        for tok in proj_tokens(rec.get("Project Name", "")):
            if tok in subj_l:
                return iid
    # Cross-validate with a stale BID# token in subject (only if project matches)
    m = re.search(r"\bBID[\s\-]?(\d{3,4})\b", subject or "", re.I)
    if m:
        stale_bid = f"BID-{int(m.group(1)):04d}"
        rec = cache.get("bid_to_record", {}).get(stale_bid)
        if rec:
            iid = str(rec.get("Internal ID") or "").strip()
            for tok in proj_tokens(rec.get("Project Name", "")):
                if tok in subj_l and iid:
                    return iid
    # Last resort: most-recent exact-email candidate (highest row = newest after sort)
    return sorted(pool, key=lambda x: x[0])[0][2]


def find_bid_id_by_sender(sender: str, subject: str) -> str | None:
    """Fallback: match reply to a CRM bid by sender email + project name keyword.

    For replies like 'Re: Follow-Up: Project Name' that don't include the BID#,
    look up the sender's email in the CRM Contact Email column AND verify a
    project-name token from the subject appears in the matched bid's name.
    """
    if not sender: return None
    em_match = re.search(r"<([^>]+)>", sender)
    sender_email = (em_match.group(1) if em_match else sender).strip().lower()
    if "@" not in sender_email: return None
    try:
        from crm_lib import get_sheet
    except ImportError:
        return None
    try:
        ws = get_sheet("Bid Log")
        recs = ws.get_all_records()
    except Exception:
        return None
    # Find rows where Contact Email contains the sender (handles space-separated multi-email cells)
    candidates = []
    for r in recs:
        contact_email = (r.get("Contact Email") or "").lower()
        if sender_email in contact_email:
            candidates.append(r)
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0].get("Bid #")
    # Multiple matches — disambiguate by project-name token in subject
    subj_lower = (subject or "").lower()
    for r in candidates:
        proj = (r.get("Project Name") or "").lower()
        # Pick a distinctive token from the project name (4+ chars, not a stop word)
        for tok in re.findall(r"[a-z0-9]+", proj):
            if len(tok) >= 4 and tok in subj_lower:
                return r.get("Bid #")
    # Last resort: most recently submitted candidate
    return candidates[0].get("Bid #")


def fetch_replies(days: int = 14) -> list[dict]:
    """Pull recent inbound replies that look like responses to our follow-ups."""
    M = imaplib.IMAP4_SSL("imap.gmail.com")
    M.login(GMAIL_USER, GMAIL_PASS)
    M.select("INBOX")
    since = (date.today() - timedelta(days=days)).strftime("%d-%b-%Y")
    # Replies to follow-ups: subjects with "Follow-Up" + BID reference
    queries = [
        f'(SINCE "{since}" SUBJECT "Follow-Up")',
        f'(SINCE "{since}" SUBJECT "Follow-up")',
        f'(SINCE "{since}" SUBJECT "BID-")',
    ]
    seen_ids = set()
    out = []
    for q in queries:
        typ, data = M.search(None, q)
        if typ != "OK" or not data[0]:
            continue
        for mid in data[0].split():
            if mid in seen_ids: continue
            seen_ids.add(mid)
            typ, raw = M.fetch(mid, '(BODY.PEEK[])')
            if typ != "OK" or not raw or not raw[0]: continue
            msg = email_lib.message_from_bytes(raw[0][1])
            subject = decode_subject(msg.get("Subject"))
            sender = decode_subject(msg.get("From"))
            # Skip our own outgoing copies
            if "carolinacommercialfinishes.com" in sender.lower(): continue
            # Skip mail delivery subsystem
            if "mailer-daemon" in sender.lower() or "mail delivery" in sender.lower(): continue
            mid_str = msg.get("Message-ID", "").strip("<>") or f"x-{mid.decode()}"
            body = get_body_text(msg)
            try:
                dt = parsedate_to_datetime(msg.get("Date", "")) if msg.get("Date") else None
            except Exception:
                dt = None
            bid_id = (find_bid_id_in_text(subject)
                      or find_bid_id_in_text(body)
                      or find_bid_id_by_sender(sender, subject))
            out.append({
                "message_id": mid_str,
                "subject": subject,
                "from": sender,
                "date": dt,
                "body": body,
                "bid_id": bid_id,
            })
    M.logout()
    return out


def classify_with_gemini(reply: dict) -> dict:
    try:
        import litellm
    except ImportError:
        return {"category": "UNCLEAR", "confidence": 0.0, "summary": "litellm not installed"}
    if not os.environ.get("GEMINI_API_KEY"):
        return {"category": "UNCLEAR", "confidence": 0.0, "summary": "GEMINI_API_KEY not set"}

    user_msg = (
        f"Subject: {reply['subject']}\n"
        f"From: {reply['from']}\n"
        f"Bid #: {reply.get('bid_id', 'unknown')}\n\n"
        f"REPLY BODY:\n{reply['body'][:2000]}\n\n"
        "Classify per the system prompt."
    )
    try:
        r = litellm.completion(
            model="gemini/gemini-2.5-flash",
            max_tokens=400,
            temperature=0.1,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": CLASSIFY_SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
        )
        text = r.choices[0].message.content.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            m = re.search(r"\{[\s\S]*\}", text)
            return json.loads(m.group(0)) if m else {"category": "UNCLEAR", "confidence": 0.0}
    except Exception as e:
        return {"category": "UNCLEAR", "confidence": 0.0, "summary": f"classifier error: {e}"}


def build_crm_cache():
    """Read Bid Log once per script run. Returns a cache dict used by update_crm
    and flushed to Sheets at end of run via flush_crm_updates."""
    from crm_lib import _retry, get_sheet
    ws = get_sheet("Bid Log")
    headers = _retry(ws.row_values, 1)
    records = _retry(ws.get_all_records)
    # AGENTS_LESSONS.md R1: NEVER use Bid# as a key — it's a row-number formula
    # that shifts on every sort. Use Internal ID UUID (stable). Lookups are
    # by `iid_to_row` and `rows_idx` only.
    iid_to_row = {}        # Internal ID -> sheet row (STABLE across sorts)
    iid_to_record = {}
    rows_idx = []          # [(row_idx, rec)] for the stable resolver
    for i, rec in enumerate(records):
        row_idx = i + 2    # +2: header is row 1, records 0-indexed
        iid = str(rec.get("Internal ID") or "").strip()
        if iid:
            iid_to_row[iid] = row_idx
            iid_to_record[iid] = rec
        rows_idx.append((row_idx, rec))
    return {
        "ws": ws,
        "headers": headers,
        "iid_to_row": iid_to_row,
        "iid_to_record": iid_to_record,
        "rows_idx": rows_idx,
        "pending_updates": [],
    }


def flush_crm_updates(cache, dry_run: bool = False):
    """Apply all accumulated cell updates in one batched API call."""
    if dry_run or not cache.get("pending_updates"):
        return 0
    from crm_lib import _retry
    _retry(cache["ws"].batch_update, cache["pending_updates"],
           value_input_option="USER_ENTERED")
    n = len(cache["pending_updates"])
    cache["pending_updates"] = []
    return n


def update_crm(internal_id: str, classification: dict, reply_date: datetime | None,
               cache: dict, dry_run: bool = False) -> dict:
    """Queue CRM updates based on classification into the cache. Returns what changed.
    Keyed by Internal ID (UUID) — stable across sheet sorts. Updates are
    flushed to Sheets in one batch by flush_crm_updates."""
    from gspread.utils import rowcol_to_a1
    headers = cache["headers"]
    status_col = headers.index("Status") + 1
    notes_col = headers.index("Notes") + 1
    loss_reason_col = headers.index("Loss Reason") + 1 if "Loss Reason" in headers else None
    win_loss_col = headers.index("Win/Loss") + 1 if "Win/Loss" in headers else None

    if internal_id not in cache["iid_to_row"]:
        return {"error": f"internal_id {internal_id[:8]} not found in CRM"}
    row_idx = cache["iid_to_row"][internal_id]
    rec = cache["iid_to_record"][internal_id]
    bid_id = str(rec.get("Bid #") or internal_id[:8])  # display label only
    cur_status = str(rec.get("Status") or "")
    cur_notes = str(rec.get("Notes") or "")
    cur_loss_reason = str(rec.get("Loss Reason") or "") if loss_reason_col else ""

    cat = classification.get("category", "UNCLEAR")
    conf = classification.get("confidence", 0.0)
    summary = (classification.get("summary") or "")[:200]
    loss_reason_text = classification.get("loss_reason", "") or ""
    quote = classification.get("key_quote", "") or ""

    # Build the Notes addendum
    date_str = reply_date.strftime("%m/%d") if reply_date else datetime.now().strftime("%m/%d")
    notes_line = f"[{date_str} reply: {cat}] {summary}"
    if quote:
        notes_line += f" — \"{quote[:100]}\""

    changes = {"bid_id": bid_id, "category": cat, "confidence": conf}
    cell_updates = cache["pending_updates"]

    # Notes — always append (deduped)
    if notes_line not in cur_notes:
        new_notes = (cur_notes + "\n" + notes_line).strip() if cur_notes else notes_line
        cell_updates.append({
            "range": rowcol_to_a1(row_idx, notes_col),
            "values": [[new_notes]],
        })
        rec["Notes"] = new_notes
        changes["notes_appended"] = notes_line

    # Status change — terminal categories on high confidence, plus the
    # Bid Submitted → Awaiting Decision transition when GC confirms project
    # is alive but undecided ("we can keep you posted", "still waiting on owner").
    new_status = None
    if cat == "LOST" and conf >= 0.85 and cur_status not in ("Lost", "Won"):
        new_status = "Lost"
    elif cat == "WON" and conf >= 0.85 and cur_status not in ("Won",):
        new_status = "Won"
    elif cat == "STILL_AWAITING" and conf >= 0.85 and cur_status == "Bid Submitted":
        # GC confirmed receipt + decision still pending → mature to Awaiting Decision
        new_status = "Awaiting Decision"
    elif cat == "PRICING" and conf >= 0.85 and cur_status == "Bid Submitted":
        # GC engaging on pricing → also moves to Awaiting Decision (project alive)
        new_status = "Awaiting Decision"
        pass
    if cat == "WON" and conf >= 0.85:
        # On WIN: also auto-stage a thank-you + next-steps email for user approval
        try:
            import subprocess
            r_draft = subprocess.run(
                [sys.executable, str(Path(__file__).resolve().parent / "draft_email.py"),
                 "--bid", bid_id, "--type", "win-acceptance", "--json"],
                capture_output=True, text=True, encoding="utf-8", timeout=60,
            )
            if r_draft.returncode == 0:
                draft = json.loads(r_draft.stdout)
                if "error" not in draft:
                    pending_dir = Path(__file__).resolve().parent.parent / "data" / "pending_followups"
                    pending_dir.mkdir(parents=True, exist_ok=True)
                    record = {
                        "bid_id": bid_id, "amount": 0,
                        "fu_type": "win-acceptance",
                        "reason": "auto-staged on WIN classification",
                        "to": draft.get("to"),
                        "subject": draft.get("subject"),
                        "body": draft.get("body"),
                        "staged_at": datetime.now().isoformat(timespec="seconds"),
                    }
                    (pending_dir / f"{bid_id}_win.json").write_text(
                        json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
                    changes["win_acceptance_staged"] = True
        except Exception:
            pass
    if new_status:
        cell_updates.append({
            "range": rowcol_to_a1(row_idx, status_col),
            "values": [[new_status]],
        })
        rec["Status"] = new_status
        changes["status_change"] = f"{cur_status} -> {new_status}"
        # Win/Loss column — only set for terminal outcomes
        if win_loss_col and new_status in ("Won", "Lost"):
            wl = "WIN" if new_status == "Won" else "LOSS"
            cell_updates.append({
                "range": rowcol_to_a1(row_idx, win_loss_col),
                "values": [[wl]],
            })
            rec["Win/Loss"] = wl

    # Auto-stage acknowledgment reply for STILL_AWAITING / PRICING / LOST
    # (WON already handled above with win-acceptance template)
    auto_ack_categories = {
        "STILL_AWAITING": "still-awaiting-ack",
        "PRICING": "pricing-engagement",
        "LOST": "loss-feedback-request",
    }
    if cat in auto_ack_categories and conf >= 0.85:
        try:
            import subprocess
            ack_type = auto_ack_categories[cat]
            r_draft = subprocess.run(
                [sys.executable, str(Path(__file__).resolve().parent / "draft_email.py"),
                 "--bid", bid_id, "--type", ack_type, "--json"],
                capture_output=True, text=True, encoding="utf-8", timeout=60,
            )
            if r_draft.returncode == 0:
                draft = json.loads(r_draft.stdout)
                if "error" not in draft:
                    pending_dir = Path(__file__).resolve().parent.parent / "data" / "pending_followups"
                    pending_dir.mkdir(parents=True, exist_ok=True)
                    record = {
                        "bid_id": bid_id, "amount": 0,
                        "fu_type": ack_type,
                        "reason": f"auto-staged on {cat} classification",
                        "to": draft.get("to"),
                        "subject": draft.get("subject"),
                        "body": draft.get("body"),
                        "staged_at": datetime.now().isoformat(timespec="seconds"),
                    }
                    (pending_dir / f"{bid_id}_{ack_type}.json").write_text(
                        json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
                    changes[f"{ack_type}_staged"] = True
        except Exception:
            pass

    # Loss Reason — only if LOST and column empty (don't overwrite user's existing reason)
    if cat == "LOST" and conf >= 0.85 and loss_reason_col and not cur_loss_reason:
        new_reason = loss_reason_text or "Auto: " + summary[:60]
        cell_updates.append({
            "range": rowcol_to_a1(row_idx, loss_reason_col),
            "values": [[new_reason]],
        })
        rec["Loss Reason"] = new_reason
        changes["loss_reason_set"] = loss_reason_text

    if dry_run:
        changes["dry_run"] = True

    return changes


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since-days", type=int, default=7)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--no-telegram", action="store_true")
    args = ap.parse_args()

    state = load_state()
    processed = set(state.get("processed_message_ids", []))

    log(f"[replies] scanning Inbox past {args.since_days} days for follow-up replies...", args.quiet)
    replies = fetch_replies(days=args.since_days)
    log(f"[replies] {len(replies)} inbound matches found", args.quiet)

    new_replies = [r for r in replies if r["message_id"] not in processed]
    already = len(replies) - len(new_replies)
    log(f"[replies] {len(new_replies)} NEW unprocessed | {already} already processed earlier | {len(replies)} TOTAL inbound matches", args.quiet)
    if not new_replies:
        log(f"[replies] NOTE: 'new' = 0 does NOT mean 'no GC has replied'. "
            f"For a live count of all replies (including already-processed), "
            f"run: python scripts/check_replies.py --days {args.since_days}", args.quiet)
        return 0

    crm_cache = build_crm_cache()

    summary_lines = []
    crm_changes = []
    for r in new_replies:
        # STABLE resolution: match this reply to a CRM row by sender-email +
        # project-keyword against LIVE data → Internal ID (UUID). The reply's
        # subject Bid# is stale after sheet re-sorts, so we no longer trust it
        # as the key. This is the fix that makes LOST/WON auto-reconcile.
        internal_id = resolve_internal_id(
            r.get("from", ""), r.get("subject", ""), r.get("body", ""), crm_cache)
        bid_id = r.get("bid_id")  # kept for logging only
        if not internal_id:
            log(f"  [skip] could not resolve reply to a CRM row: {r['subject'][:60]}", args.quiet)
            processed.add(r["message_id"])  # don't re-scan
            continue
        # Resolve a human-readable label from the live row
        _rec = crm_cache["iid_to_record"].get(internal_id, {})
        disp = (_rec.get("Bid #") or bid_id or internal_id[:8])
        log(f"\n  {disp}  {r['subject'][:65]}", args.quiet)
        log(f"    from: {r['from'][:60]}  iid={internal_id[:8]}", args.quiet)
        cls = classify_with_gemini(r)
        cat = cls.get("category", "UNCLEAR")
        conf = cls.get("confidence", 0.0)
        log(f"    classified: {cat}  (conf={conf:.2f})", args.quiet)
        log(f"    summary: {cls.get('summary','')[:120]}", args.quiet)
        if cat in ("LOST", "WON") and conf >= 0.85:
            log(f"    -> CRM Status will change", args.quiet)

        changes = update_crm(internal_id, cls, r["date"], crm_cache, dry_run=args.dry_run)
        crm_changes.append({
            "bid_id": bid_id,
            "subject": r["subject"][:60],
            "from": r["from"][:50],
            "category": cat,
            "confidence": conf,
            "changes": changes,
        })
        if not args.dry_run:
            processed.add(r["message_id"])

        # Build telegram-friendly line
        flag_emoji = {
            "LOST": "❌", "WON": "✅", "PRICING": "💰",
            "STILL_AWAITING": "⏳", "OUT_OF_OFFICE": "🌴", "UNCLEAR": "❓",
        }.get(cat, "❓")
        line = f"{flag_emoji} *{bid_id}* — {cat}"
        if "status_change" in changes:
            line += f"  → CRM: {changes['status_change']}"
        line += f"\n  _{cls.get('summary','')[:120]}_"
        summary_lines.append(line)

    if not args.dry_run:
        flushed = flush_crm_updates(crm_cache, dry_run=args.dry_run)
        log(f"[replies] flushed {flushed} cell updates to CRM", args.quiet)
        state["processed_message_ids"] = list(processed)
        save_state(state)

    # Telegram summary — ONLY for actionable categories. UNCLEAR/OUT_OF_OFFICE
    # are noise and shouldn't ping the user (owner said: "what the fuck is this"
    # about a stream of "❓ BID-XXXX — UNCLEAR" pings — manual review can stay
    # silent in CRM Notes).
    actionable = [c for c in crm_changes
                  if c.get("category") not in ("UNCLEAR", "OUT_OF_OFFICE")]
    actionable_lines = []
    for line, c in zip(summary_lines, crm_changes):
        if c.get("category") not in ("UNCLEAR", "OUT_OF_OFFICE"):
            actionable_lines.append(line)
    if actionable_lines and not args.no_telegram:
        title = "📨 *Follow-up replies processed*"
        if args.dry_run:
            title += " (DRY RUN)"
        body = (title + f"\n{len(actionable)} actionable reply/replies "
                f"({len(crm_changes) - len(actionable)} noise filtered):\n\n"
                + "\n\n".join(actionable_lines[:10]))
        tg_send(body)

    log(f"\n[replies] processed {len(crm_changes)} replies"
        + (" (dry-run)" if args.dry_run else ""), args.quiet)
    for c in crm_changes:
        sc = c["changes"].get("status_change", "")
        log(f"  {c['bid_id']}  {c['category']}  conf={c['confidence']:.2f}  {sc}", args.quiet)

    # Activity log — each meaningful classification gets its own line
    if not args.dry_run:
        try:
            from log_activity import log_activity
            for c in crm_changes:
                cat = c["category"]
                if cat in ("OUT_OF_OFFICE", "UNCLEAR"):
                    continue  # noise — skip
                bid_id = c["bid_id"]
                conf = c["confidence"]
                sc = c["changes"].get("status_change", "")
                log_activity(
                    "📨 Reply classified",
                    f"{bid_id} — {cat} (conf {conf:.2f}){' · ' + sc if sc else ''}"
                )
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
