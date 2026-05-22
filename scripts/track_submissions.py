#!/usr/bin/env python3
"""
Auto-detect submitted bids by scanning Gmail Sent folder for proposal emails.

Logic:
  1. Search Sent folder for emails matching proposal patterns (subject contains
     "proposal", "bid", "painting"; from cs@/estimates@carolinacommercialfinishes).
  2. For each match, extract project name from subject.
  3. Fuzzy-match to active_bids.json projects.
  4. Update bid_status.json: status="submitted" + submitted_at timestamp +
     recipient + subject. Don't overwrite "won"/"lost"/"no_bid".
  5. Also detect win/loss from incoming replies (responses on submitted bids).

Usage:
  python scripts/track_submissions.py                     # scan past 30 days
  python scripts/track_submissions.py --days 14
  python scripts/track_submissions.py --dry-run
  python scripts/track_submissions.py --quiet             # one-line summary
"""

import argparse
import difflib
import imaplib
import email as email_lib
import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from email.header import decode_header
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = Path(__file__).resolve().parent.parent
ACTIVE_BIDS  = BASE / "data" / "memory" / "active_bids.json"
STATUS_FILE  = BASE / "data" / "memory" / "bid_status.json"
LOG_FILE     = BASE / "data" / "logs" / "track_submissions.log"

GMAIL_USER = os.environ.get("GMAIL_USER", "estimates@carolinacommercialfinishes.com")
GMAIL_PASS = os.environ.get("GMAIL_APP_PASSWORD", "")

# Status values that should NOT be overwritten by submitted-detection
TERMINAL = {"won", "lost", "no_bid", "declined"}


def normalize(s):
    if not s: return ""
    s = re.sub(r"#\s*\d+", "", s.lower())
    s = re.sub(r"[(),\-/_]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def slugify(name):
    if not name: return ""
    s = re.sub(r"[^a-z0-9\s-]", "", name.lower())
    s = re.sub(r"\s+", "-", s).strip("-")
    return re.sub(r"-+", "-", s)[:80]


def match_score(a, b):
    na, nb = normalize(a), normalize(b)
    if not na or not nb: return 0.0
    base = difflib.SequenceMatcher(None, na, nb).ratio()
    tokens_a = {t for t in na.split() if len(t) >= 5}
    tokens_b = {t for t in nb.split() if len(t) >= 5}
    if tokens_a and tokens_b:
        ov = len(tokens_a & tokens_b) / max(len(tokens_a), len(tokens_b))
        base = (base + 0.5 * ov) / 1.5
    nums_a = set(re.findall(r"\d{3,}", a or ""))
    nums_b = set(re.findall(r"\d{3,}", b or ""))
    if nums_a and nums_b:
        if nums_a & nums_b:
            base += 0.15
        else:
            # CRITICAL: Both names have 3+ digit numbers (store #, project #,
            # building #) and they don't overlap → this is the bug behind
            # Food Lion #1336 Quinton being merged with Food Lion #2235 Quinton.
            # Hard reject: different store numbers = different projects.
            return 0.0
    return min(base, 1.0)


def extract_project_from_subject(subject):
    """Pull a project-name-like phrase from a proposal email subject.
    Handles separators: em-dash (—), hyphen (-), and pipe (|).
    Strips trailing CCF/proposal/painting/scope boilerplate."""
    s = re.sub(r"\s+", " ", (subject or "")).strip()
    s = re.sub(r"^(Fwd:|RE:|FW:|Re:|Fw:)\s*", "", s, flags=re.I).strip()
    # Strip LEADING CCF/bid/proposal prefixes:
    #   "CCF Bid — Midtown East (Buildings 1,2&3)" -> "Midtown East (...)"
    #   "CCF Proposal: Foo"                        -> "Foo"
    #   "Bid Submission — Bar"                     -> "Bar"
    s = re.sub(r"^\s*(?:CCF\s+)?(?:Bid|Proposal|Quote|Estimate|Bid\s+Submission|"
               r"Painting\s+Proposal|Revised\s+Proposal)\s*[—\-–:|]\s*",
               "", s, flags=re.I).strip()
    s = re.sub(r"^\s*CCF\s*[—\-–:|]\s*", "", s, flags=re.I).strip()
    # Cut at the FIRST separator that introduces boilerplate. Subjects look like:
    #   "TMSA Concord Grandstands — CCF Proposal — Painting"
    #   "Chewy Vet Care – Salt Lake City | CCF Painting, Wallcovering"
    #   "<Project> — Painting Proposal — Carolina Commercial Finishes"
    # Strip anything from a separator followed by CCF / proposal-ish words.
    s = re.sub(r"\s*[—\-–|]\s*CCF\b.*$", "", s, flags=re.I)
    s = re.sub(r"\s*[—\-–|]\s*Painting\s+(?:&\s+\w+\s+)?Proposal.*$", "", s, flags=re.I)
    s = re.sub(r"\s*[—\-–|]\s*(?:Painting|Proposal|Bid|Quote|Estimate|Wallcovering)\b.*$", "", s, flags=re.I)
    s = re.sub(r"\s*[—\-–|]\s*Carolina\s+Commercial.*$", "", s, flags=re.I)
    # If a pipe remains with trailing scope text, keep only the head segment
    if "|" in s:
        s = s.split("|", 1)[0].strip()
    return s.strip("-–— |").strip()


def extract_bid_total(body: str):
    """Return the proposal's TOTAL bid amount as '$X,XXX', or None.

    Proposals format the total like:
      'TOTAL BID PRICE ................................... $75,000'
      'TOTAL COMBINED CCF SCOPE $94,568'
      'GRAND TOTAL: $1,234,567.00'
      'TOTAL PROPOSAL — $48,500'
    The OLD code grabbed the first $-number anywhere (usually a line item),
    which is why TMSA logged $21,950 instead of $75,000. Strategy:
      1. Find $-amounts that follow an explicit total label (dot-leaders /
         dashes / colons / newlines allowed between label and number).
      2. If any found, return the LARGEST (grand total > sub-totals).
      3. Fallback: largest plausible $ amount in the whole body.
    """
    if not body:
        return None
    body = body.replace(" ", " ")
    TOTAL_LABEL = (r"(?:GRAND\s+TOTAL|TOTAL\s+BID\s+PRICE|TOTAL\s+COMBINED"
                   r"[\w\s]*|TOTAL\s+PROPOSAL|PROJECT\s+TOTAL|TOTAL\s+CCF"
                   r"[\w\s]*|BID\s+PRICE|PROPOSAL\s+TOTAL|TOTAL\s+PRICE"
                   r"|CONTRACT\s+(?:SUM|PRICE)|TOTAL)")
    candidates = []
    # Label, then up to 60 chars of leaders/punct/space/newline, then $amount
    for m in re.finditer(
            TOTAL_LABEL + r"[\s\.\:\-–—=>]{0,60}?\$\s*([\d,]+(?:\.\d{2})?)",
            body, re.I | re.S):
        try:
            val = int(m.group(1).replace(",", "").split(".")[0])
        except ValueError:
            continue
        if 1000 <= val <= 20_000_000:
            candidates.append((val, m.group(1)))
    if candidates:
        val, raw = max(candidates, key=lambda x: x[0])
        return "$" + raw
    # Fallback: largest plausible $ anywhere
    fallback = []
    for m in re.finditer(r"\$\s*([\d,]+(?:\.\d{2})?)", body):
        try:
            val = int(m.group(1).replace(",", "").split(".")[0])
        except ValueError:
            continue
        if 5000 <= val <= 5_000_000:
            fallback.append((val, m.group(1)))
    if fallback:
        return "$" + max(fallback, key=lambda x: x[0])[1]
    return None


def split_combined_stores(subject, body):
    """If subject mentions 2+ store numbers (#NNNN), split into separate per-store
    proposals. Returns [{"store": "#NNNN", "amount": "$X,XXX"}] or [] if not combined.

    Matches the same logic as audit_combined_proposals.py — only flags as combined
    when 2+ stores appear in the SUBJECT (body-only refs are historical context).
    """
    subj = subject or ""
    body = body or ""
    # Unique 3-4 digit store numbers in subject only
    nums = set()
    for pat in [re.compile(r"#\s*(\d{3,4})[\-A-Z]?", re.I),
                re.compile(r"(?:store|#)\s*[#:]?\s*(\d{4})", re.I)]:
        for m in pat.finditer(subj):
            n = m.group(1)
            if 3 <= len(n) <= 5:
                nums.add(n)
    if len(nums) < 2:
        return []
    out = []
    for sn in nums:
        amount = None
        for m in re.finditer(rf"#\s*0*{sn}.{{0,200}}?\$\s*([\d,]+(?:\.\d{{2}})?)",
                              body, re.S):
            val = int(m.group(1).replace(",", "").split(".")[0])
            if 5000 <= val <= 5_000_000:
                amount = "$" + m.group(1)
                break
        out.append({"store": "#" + sn.zfill(4), "amount": amount})
    return out


def fetch_sent_proposals(days=30):
    """Search Sent folder for outbound proposal-like emails."""
    since = (date.today() - timedelta(days=days)).strftime("%d-%b-%Y")
    queries = [
        f'(SINCE "{since}" SUBJECT "proposal")',
        f'(SINCE "{since}" SUBJECT "bid")',
        f'(SINCE "{since}" SUBJECT "painting" SUBJECT "proposal")',
    ]
    M = imaplib.IMAP4_SSL("imap.gmail.com")
    M.login(GMAIL_USER, GMAIL_PASS)
    M.select('"[Gmail]/Sent Mail"')
    seen, results = set(), []
    for q in queries:
        st, ids = M.search(None, q)
        if st != "OK" or not ids[0]: continue
        for mid in ids[0].split():
            if mid in seen: continue
            seen.add(mid)
            st, data = M.fetch(mid, '(BODY.PEEK[])')
            if st != "OK": continue
            msg = email_lib.message_from_bytes(data[0][1])
            subj = ""
            for p, e in decode_header(msg.get("Subject", "")):
                subj += p.decode(e or "utf-8", errors="replace") if isinstance(p, bytes) else p
            # Pull text body for amount extraction
            body_text = ""
            try:
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            try:
                                body_text = part.get_payload(decode=True).decode("utf-8", errors="replace")
                                break
                            except Exception: pass
                else:
                    body_text = msg.get_payload(decode=True).decode("utf-8", errors="replace")
            except Exception:
                pass
            # Stable identifier so we don't re-log the same send every run
            mid_hdr = msg.get("Message-ID", "") or ""
            results.append({
                "subject":    subj.strip(),
                "from":       msg.get("From", ""),
                "to":         msg.get("To", ""),
                "date":       msg.get("Date", ""),
                "body":       body_text[:8000],
                "message_id": mid_hdr.strip("<> "),
            })
    M.logout()
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--threshold", type=float, default=0.55)
    args = ap.parse_args()

    def log(*a, **k):
        if not args.quiet: print(*a, **k)

    log(f"[track] scanning Sent past {args.days} days for proposal emails...")
    proposals = fetch_sent_proposals(days=args.days)
    log(f"[track] found {len(proposals)} sent proposals")

    active_bids = json.load(open(ACTIVE_BIDS, encoding="utf-8"))

    # Load existing overrides
    if STATUS_FILE.exists():
        data = json.load(open(STATUS_FILE, encoding="utf-8"))
    else:
        data = {"overrides": {}, "history": []}
    overrides = data.setdefault("overrides", {})
    history   = data.setdefault("history", [])

    matched, updated = 0, 0
    new_sends = []   # itemized list of new submissions detected this run
    # Track which Gmail message_ids we've already logged to activity_log so we
    # don't re-emit "Carvana 4/21 sent" every 59 min. State persists in
    # bid_status.json under "logged_activity_message_ids" (set semantics).
    logged_ids = set(data.get("logged_activity_message_ids", []))

    # Subject markers that mean "this is a follow-up / chase / reply, NOT an
    # original proposal send". Capturing these as new submissions is what
    # created the duplicate Food Lion rows (chase emails to bids@fiicgc.com
    # got logged as fresh proposals). An ORIGINAL proposal subject says
    # "Proposal" / "Bid Submission" / "Painting Bid" and never these.
    FU_MARKERS = (
        "follow-up", "follow up", "status check", "status update on",
        "quick check", "checking in", "calling tomorrow", "calling today",
        "phone outreach", "closing out", "final email", "last email on",
        "moving to phone", "before close-out", "any update", "circling back",
    )

    def _is_followup_subject(subj: str) -> bool:
        s = (subj or "").strip().lower()
        if s.startswith(("re:", "re :", "fwd:", "fw:")):
            return True
        return any(mk in s for mk in FU_MARKERS)

    for p in proposals:
        # Skip follow-up / chase / reply emails — only ORIGINAL proposals count
        if _is_followup_subject(p.get("subject", "")):
            continue
        # Normalize the email Date header to mm/dd/yyyy (used by all branches)
        sub_date_str = p["date"][:30]
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(p["date"])
            if dt:
                sub_date_str = dt.strftime("%m/%d/%Y")
        except Exception:
            pass
        # Extract bare email + display name from "Name <email@x>" style To
        to_field = p["to"][:200]
        m = re.search(r"<([^>]+)>", to_field)
        recipient_email = (m.group(1) if m else to_field).strip().lower()
        m2 = re.match(r'^\s*"?([^"<]+?)"?\s*<', to_field)
        recipient_display = m2.group(1).strip() if m2 else ""

        # === Combined-email check: subject mentions 2+ #NNNN stores ===
        # Each store becomes its own logical proposal with its own active_bid
        # match and its own amount, so the CRM writer creates one row per store.
        combined = split_combined_stores(p["subject"], p.get("body", ""))
        if combined:
            for cs in combined:
                store_num = cs["store"].lstrip("#").lstrip("0") or "0"
                # Fuzzy-match limited to bids whose project_name references this store
                best_bid, best_score = None, 0
                for ab in active_bids:
                    pn = ab.get("project_name", "") or ""
                    nums = set(re.findall(r"\d{3,4}", pn))
                    if store_num not in nums and store_num.lstrip("0") not in {n.lstrip("0") for n in nums}:
                        continue  # different store
                    s = match_score(p["subject"], pn)
                    if s > best_score:
                        best_score, best_bid = s, ab
                if not best_bid:
                    log(f"  [skip combined] {cs['store']} — no matching active_bid")
                    continue
                matched += 1
                slug = slugify(best_bid.get("project_name", ""))
                cur = overrides.get(slug, {})
                if cur.get("status") in TERMINAL:
                    continue
                submissions = cur.setdefault("submissions", []) if cur else []
                sub_key = (recipient_email, sub_date_str)
                if any(((s.get("to") or "").strip().lower(), s.get("at")) == sub_key
                       for s in submissions):
                    continue
                new_sub = {
                    "to": recipient_email,
                    "to_display": recipient_display,
                    "at": sub_date_str,
                    "subject": p["subject"][:200],
                    "store": cs["store"],   # tag this submission with its store
                }
                if cs.get("amount"):
                    new_sub["amount"] = cs["amount"]
                submissions.append(new_sub)
                prev_status = cur.get("status", "(no override)")
                overrides[slug] = {
                    **cur,
                    "status": "submitted",
                    "submitted_at": sub_date_str,
                    "submitted_subject": p["subject"][:200],
                    "submitted_to": recipient_email,
                    "submissions": submissions,
                    "reason": cur.get("reason") or "Auto-detected (combined email)",
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                }
                history.append({
                    "slug": slug, "from": prev_status, "to": "submitted",
                    "trigger": "track_submissions",
                    "to_email": recipient_email,
                    "subject": f"[{cs['store']}] " + p["subject"][:70],
                    "at": datetime.now().isoformat(timespec="seconds"),
                })
                updated += 1
                new_sends.append({
                    "project": best_bid.get("project_name", "")[:60],
                    "store": cs["store"],
                    "amount": cs.get("amount", ""),
                    "recipient": recipient_email,
                    "date": sub_date_str,
                    "combined": True,
                })
                log(f"  COMBINED [{best_score:.2f}] {cs['store']} {cs.get('amount','?')} "
                    f"-> {best_bid.get('project_name','')[:35]} -> {recipient_email[:30]}")
            continue  # done with this combined email

        # === Single-store path (original behavior) ===
        proj_name = extract_project_from_subject(p["subject"])
        if not proj_name or len(proj_name) < 5:
            continue
        # Find best active_bid match
        best_bid, best_score = None, 0
        for ab in active_bids:
            s = match_score(proj_name, ab.get("project_name", ""))
            if s > best_score:
                best_score, best_bid = s, ab

        # Grab the bid TOTAL from the body (prioritizes "TOTAL BID PRICE"
        # over line items — needed for orphan gate too)
        amount = None
        try:
            amount = extract_bid_total(p.get("body") or "")
        except Exception:
            pass

        is_orphan = best_score < args.threshold or not best_bid
        if is_orphan:
            # === ORPHAN CAPTURE ===
            # No scraped bid-invitation matched this sent proposal. Historically
            # we dropped it here — that's why proposals we sent for projects the
            # scrapers never saw (or named differently) NEVER reached the CRM.
            # Now: still capture it so EVERY sent proposal lands in the Bid Log.
            #
            # Safety gate (avoid logging non-proposal noise): require a plausible
            # $ amount in the body AND an external recipient AND a proposal-ish
            # subject. Internal-only sends already filtered upstream.
            subj_l = (p["subject"] or "").lower()
            # Permissive: ANY of these signals a CCF bid/proposal. Critically
            # includes bare "bid" + "ccf" so "CCF Bid — Midtown East" counts
            # (the old narrow list silently dropped a $307K proposal).
            looks_like_proposal = any(k in subj_l for k in (
                "proposal", "bid", "ccf", "quote", "estimate", "submission",
                "pricing", "painting", "wallcovering", "scope"))
            ext_recipient = recipient_email and not any(
                x in recipient_email.lower() for x in
                ("carolinacommercial", "wilsonsviatlana83", "smayurov@gmail",
                 "mailer-daemon", "noreply", "no-reply"))
            # Fallback: a strong $ total to an external party is a proposal even
            # if the subject keyword check somehow misses (defense in depth).
            strong_total = False
            try:
                strong_total = amount and int(
                    str(amount).replace("$", "").replace(",", "").split(".")[0]
                ) >= 10000
            except Exception:
                pass
            if not (amount and ext_recipient and (looks_like_proposal or strong_total)):
                continue  # genuinely not a capturable proposal — skip
            effective_project = proj_name[:80]
            slug = slugify(effective_project)
            best_bid = None  # explicit: orphan has no source invitation
        else:
            matched += 1
            effective_project = best_bid.get("project_name", "")
            slug = slugify(effective_project)

        cur = overrides.get(slug, {})
        if cur.get("status") in TERMINAL:
            continue  # don't overwrite final outcome

        # NEW: per-GC submission tracking. Each unique (recipient_email,
        # submitted_at) pair is its own submission record so the CRM writer
        # can create one row per GC the proposal was sent to.
        submissions = cur.setdefault("submissions", []) if cur else []
        sub_key = (recipient_email, sub_date_str)
        already_recorded = any(
            ((s.get("to") or "").strip().lower(), s.get("at")) == sub_key
            for s in submissions
        )
        if already_recorded:
            continue
        new_sub = {
            "to": recipient_email,
            "to_display": recipient_display,
            "at": sub_date_str,
            "subject": p["subject"][:200],
        }
        if amount:
            new_sub["amount"] = amount
        submissions.append(new_sub)

        prev_status = cur.get("status", "(no override)")
        overrides[slug] = {
            **cur,
            "status": "submitted",
            # Keep legacy single-recipient fields pointing to LATEST submission
            "submitted_at": sub_date_str,
            "submitted_subject": p["subject"][:200],
            "submitted_to": recipient_email,
            "submissions": submissions,
            # project_name + orphan flag let crm_writeback synthesize a CRM row
            # even when there's no scraped active_bid for this proposal.
            "project_name": effective_project,
            "orphan": is_orphan,
            "reason": cur.get("reason") or (
                "Auto-detected (orphan — no scraped invitation)" if is_orphan
                else "Auto-detected from Sent folder"),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        history.append({
            "slug": slug, "from": prev_status, "to": "submitted",
            "trigger": "track_submissions",
            "to_email": recipient_email,
            "subject": p["subject"][:80],
            "at": datetime.now().isoformat(timespec="seconds"),
        })
        updated += 1
        new_sends.append({
            "project": effective_project[:60],
            "amount": amount or "",
            "recipient": recipient_email,
            "date": sub_date_str,
            "combined": False,
            "orphan": is_orphan,
        })
        tag = "ORPHAN-CAPTURE" if is_orphan else f"MATCH [{best_score:.2f}]"
        log(f"  {tag} {prev_status} -> submitted: {effective_project[:40]} -> {recipient_email[:30]}")

    if args.dry_run:
        log(f"[track] dry-run: would update {updated} statuses ({matched} matches)")
        return

    if updated:
        STATUS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    summary = f"track_submissions {datetime.now().strftime('%H:%M:%S')}: scanned={len(proposals)} matched={matched} status_updates={updated}"
    if args.quiet:
        print(summary)
    else:
        log(f"\n[track] {summary}")
        log(f"[track] saved overrides → {STATUS_FILE.name}")

    # Append to log
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now().isoformat(timespec='seconds')}  {summary}\n")

    # Activity log — only for sends whose Gmail message_id we HAVEN'T logged
    # before. Prevents the daily activity_log from filling with the same
    # "Carvana 4/21 sent" line every 59 minutes (the daemon cadence).
    truly_new = [s for s in new_sends if s.get("message_id") and s["message_id"] not in logged_ids]
    if truly_new:
        try:
            from log_activity import log_activity
            for s in truly_new:
                proj = s["project"]
                if s.get("combined") and s.get("store"):
                    proj = f"{proj} {s['store']}"
                amt = s.get("amount", "") or "(no $ extracted)"
                line = (f"{proj} → {s['recipient']} — {amt} "
                        f"(submitted {s.get('date','?')})")
                if s.get("combined"):
                    line = "[COMBINED EMAIL] " + line
                log_activity("📤 Proposal sent", line)
                logged_ids.add(s["message_id"])
            log_activity(
                "📊 Submission summary",
                f"track_submissions: {len(truly_new)} NEW proposal sends logged "
                f"(out of {updated} status updates, {matched} matches, "
                f"{len(proposals)} candidates scanned)"
            )
        except Exception:
            pass
    # Persist the logged-ID set so future runs skip them. Cap at last 5000 ids
    # to keep the file bounded (≈5MB of message-IDs).
    data["logged_activity_message_ids"] = list(logged_ids)[-5000:]


if __name__ == "__main__":
    main()
