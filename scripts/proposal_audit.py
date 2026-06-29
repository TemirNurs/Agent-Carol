#!/usr/bin/env python3
"""
Audit sent proposals against the CRM Bid Log.

For each proposal email in Gmail Sent folder (past N days):
  1. Extract project name, date, recipient, attachment-with-amount
  2. Try to extract bid amount from email body and/or attached PDF
  3. Match to CRM Bid Log row by project name (fuzzy)
  4. If matched and CRM Bid Amount is blank → write amount to CRM
  5. If no CRM match → report as "missing from CRM"

Usage:
  python scripts/proposal_audit.py --days 60 --dry-run     # preview
  python scripts/proposal_audit.py --days 60 --apply       # write back
  python scripts/proposal_audit.py --apply --quiet         # daemon mode
"""

import argparse
import difflib
import imaplib
import email as elib
import json
import os
import re
import sys
import tempfile
from datetime import date, datetime, timedelta
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
GMAIL_USER = os.environ.get("GMAIL_USER", "estimates@carolinacommercialfinishes.com")
GMAIL_PASS = os.environ.get("GMAIL_APP_PASSWORD", "")


def normalize(s):
    if not s: return ""
    s = re.sub(r"#\s*\d+", "", str(s).lower())
    s = re.sub(r"[(),\-/_]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def match_score(a, b):
    na, nb = normalize(a), normalize(b)
    if not na or not nb: return 0.0
    base = difflib.SequenceMatcher(None, na, nb).ratio()
    tokens_a = {t for t in na.split() if len(t) >= 5}
    tokens_b = {t for t in nb.split() if len(t) >= 5}
    if tokens_a and tokens_b:
        ov = len(tokens_a & tokens_b) / max(len(tokens_a), len(tokens_b))
        base = (base + 0.5 * ov) / 1.5
    nums_a = set(re.findall(r"\d{3,}", str(a) if a else ""))
    nums_b = set(re.findall(r"\d{3,}", str(b) if b else ""))
    if nums_a and nums_b and (nums_a & nums_b):
        base += 0.15
    return min(base, 1.0)


def extract_project_name(subj):
    s = re.sub(r"\s+", " ", subj or "").strip()
    s = re.sub(r"^(Fwd:|RE:|FW:|Re:|Fw:)\s*", "", s, flags=re.I).strip()
    # Match em-dash (—), en-dash (–), regular hyphen (-), and pipe (|)
    DASH = r"[—–\-|]"
    # Strip leading "Painting Proposal —" / "Painting & Wallcovering Proposal —"
    s = re.sub(rf"^(Painting|Wallcovering)(?:\s*&\s*\w+)?\s+Proposal\s*{DASH}\s*", "", s, flags=re.I)
    s = re.sub(rf"^Proposal\s*{DASH}\s*", "", s, flags=re.I)
    # Strip trailing "— Painting Proposal" / "— CCF" / "— Project XXX"
    s = re.sub(rf"\s*{DASH}\s*Painting\s+(?:&\s+\w+\s+)?(?:&\s+)?Wallcovering?\s*Proposal.*$", "", s, flags=re.I)
    s = re.sub(rf"\s*{DASH}\s*Painting\s+(?:&\s+\w+\s+)?Proposal.*$", "", s, flags=re.I)
    s = re.sub(rf"\s*{DASH}\s*(?:Painting|Proposal|Bid|Quote|Estimate)(?!\w).*$", "", s, flags=re.I)
    s = re.sub(rf"\s*{DASH}\s*Carolina\s+Commercial.*$", "", s, flags=re.I)
    s = re.sub(rf"\s*{DASH}\s*Project\s+\S+.*$", "", s, flags=re.I)
    s = re.sub(r"\s*\|\s*[\d\w\s,]+ St,.*$", "", s, flags=re.I)  # Strip street address
    return s.strip("- |").strip()


# Patterns to find a $ amount that's likely the BID TOTAL
AMOUNT_PATTERNS = [
    re.compile(r"(?:Total|Bid Total|Grand Total|Bid Amount|Proposal Total|Lump Sum|Contract Amount)[\s:]*\$?\s*([\d,]+(?:\.\d{2})?)", re.I),
    re.compile(r"\$\s*([\d,]+(?:\.\d{2})?)\s*(?:total|lump sum|bid|proposal)", re.I),
    re.compile(r"(?:we propose|our bid|bid\s+is|total of|amount of)[\s\w]{0,30}?\$\s*([\d,]+(?:\.\d{2})?)", re.I),
]


def extract_amount_from_text(text):
    """Try multiple patterns; return largest plausible amount or None."""
    if not text: return None
    candidates = []
    for pat in AMOUNT_PATTERNS:
        for m in pat.finditer(text):
            try:
                v = float(m.group(1).replace(",", ""))
                if 1_000 <= v <= 10_000_000:
                    candidates.append(v)
            except ValueError:
                pass
    # Fallback: look for any $X,XXX-$XXX,XXX in body
    if not candidates:
        for m in re.finditer(r"\$\s*([\d]{1,3}(?:,\d{3})+(?:\.\d{2})?)", text):
            try:
                v = float(m.group(1).replace(",", ""))
                if 5_000 <= v <= 1_000_000:
                    candidates.append(v)
            except ValueError:
                pass
    if not candidates:
        return None
    # Return the largest — usually the bid total
    return max(candidates)


def extract_amount_from_pdf(pdf_bytes):
    """Try to extract a bid amount from PDF bytes."""
    try:
        import pdfplumber, io
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            text = ""
            for page in pdf.pages:
                t = page.extract_text() or ""
                text += t + "\n"
        return extract_amount_from_text(text)
    except Exception:
        return None


def fetch_proposals(days=60, quiet=True):
    """Find outbound proposal emails from CCF in Sent folder."""
    since = (date.today() - timedelta(days=days)).strftime("%d-%b-%Y")
    M = imaplib.IMAP4_SSL("imap.gmail.com")
    M.login(GMAIL_USER, GMAIL_PASS)
    M.select('"[Gmail]/Sent Mail"')
    # Search for proposal emails (CCF outbound + has attachment + subject contains proposal/painting)
    queries = [
        f'(SINCE "{since}" SUBJECT "proposal")',
        f'(SINCE "{since}" SUBJECT "painting")',
        f'(SINCE "{since}" SUBJECT "bid")',
    ]
    seen, results = set(), []
    for q in queries:
        st, ids = M.search(None, q)
        if st != "OK" or not ids[0]: continue
        for mid in ids[0].split():
            if mid in seen: continue
            seen.add(mid)
            st, data = M.fetch(mid, '(BODY.PEEK[])')
            if st != "OK": continue
            msg = elib.message_from_bytes(data[0][1])
            subj = ""
            for p, e in decode_header(msg.get("Subject", "")):
                subj += p.decode(e or "utf-8", errors="replace") if isinstance(p, bytes) else p
            subj = subj.strip()
            # Skip Re:/Fwd:/Follow-Up: — only initial proposal sends
            if re.match(r"^(Re|Fwd|RE|FW|Re |Fwd ):", subj, re.I): continue
            if re.search(r"follow[\s\-]*up", subj, re.I): continue
            # Skip CCF internal noise (daily reports, etc.)
            if re.search(r"daily\s+bid\s+report|carol\s+test|sent\s+proposals", subj, re.I): continue
            # Get body and attachments
            body = ""
            pdfs = []
            for part in msg.walk():
                ctype = part.get_content_type()
                disp = str(part.get("Content-Disposition") or "")
                if ctype == "text/plain" and "attachment" not in disp:
                    try:
                        body += part.get_payload(decode=True).decode("utf-8", errors="replace")
                    except Exception: pass
                elif ctype == "application/pdf" or part.get_filename() and part.get_filename().lower().endswith(".pdf"):
                    try:
                        pdfs.append({
                            "filename": part.get_filename() or "attachment.pdf",
                            "bytes": part.get_payload(decode=True),
                        })
                    except Exception: pass
            results.append({
                "subject": subj,
                "to":      msg.get("To", "")[:120],
                "date":    msg.get("Date", "")[:30],
                "body":    body[:8000],
                "pdfs":    pdfs,
            })
    M.logout()
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--add-orphans", action="store_true",
                    help="Also append new CRM rows for proposals with amounts that don't match any CRM row")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--threshold", type=float, default=0.55)
    args = ap.parse_args()

    def log(*a, **k):
        if not args.quiet: print(*a, **k)

    sys.path.insert(0, str(BASE / "scripts"))
    from crm_lib import all_records, get_sheet, batch_update_rows, append_rows, next_bid_id

    log(f"[audit] scanning Sent folder past {args.days} days for proposals...")
    proposals = fetch_proposals(days=args.days, quiet=args.quiet)
    log(f"[audit] found {len(proposals)} initial-send proposal emails")

    # Read CRM Bid Log
    crm = []
    bid_sheet = get_sheet("Bid Log")
    headers = bid_sheet.row_values(1)
    rows = bid_sheet.get_all_values()
    for r_idx, row in enumerate(rows[1:], start=2):
        d = {h: (row[i] if i < len(row) else "") for i, h in enumerate(headers)}
        if d.get("Bid #") and d.get("Project Name"):
            crm.append({"row_idx": r_idx, "data": d})
    log(f"[audit] CRM Bid Log rows: {len(crm)}")

    # Process each proposal — multiple proposals can match the same CRM row
    # (resends, revisions). Latest by date wins.
    proposals.sort(key=lambda p: p.get("date", ""), reverse=False)
    matches = []
    no_match = []
    for p in proposals:
        proj = extract_project_name(p["subject"])
        if not proj or len(proj) < 5: continue
        # Find best CRM match (no dedup — multiple proposals OK)
        best_idx, best_score = None, 0
        for i, c in enumerate(crm):
            s = match_score(proj, c["data"].get("Project Name") or "")
            if s > best_score:
                best_score, best_idx = s, i
        # Extract amount
        amount = extract_amount_from_text(p["body"])
        amount_source = "body" if amount else None
        if not amount:
            for pdf in p["pdfs"]:
                amount = extract_amount_from_pdf(pdf["bytes"])
                if amount:
                    amount_source = pdf["filename"]
                    break
        if best_score >= args.threshold and best_idx is not None:
            matches.append({
                "proposal": p, "crm_row": crm[best_idx],
                "score": best_score, "amount": amount,
                "amount_source": amount_source,
            })
        else:
            no_match.append({"proposal": p, "amount": amount, "extracted_name": proj})

    # Dedup matches: keep latest match per CRM row (proposals already sorted asc by date)
    by_crm = {}
    for m in matches:
        idx = m["crm_row"]["row_idx"]
        # Only keep the latest with a non-None amount; if no amount, still record it
        prev = by_crm.get(idx)
        if not prev or m["amount"]:
            by_crm[idx] = m
    matches = list(by_crm.values())

    log(f"\n=== MATCHED to CRM ({len(matches)}) ===")
    updates = []  # (row_idx, col_name, value)
    amount_writes = 0
    for m in matches[:30]:
        d = m["crm_row"]["data"]
        existing_amount = d.get("Bid Amount ($)", "")
        a_str = f"${m['amount']:,.0f}" if m["amount"] else "?"
        ex_str = existing_amount if existing_amount else "blank"
        log(f"  [{m['score']:.2f}] {d.get('Bid #'):<10} {d.get('Project Name','')[:35]:<35} CRM={ex_str:>10} | extracted={a_str:>10} ({m['amount_source'] or 'no source'})")
        # Write amount if CRM is blank and we have an amount
        if not existing_amount and m["amount"]:
            updates.append((m["crm_row"]["row_idx"], "Bid Amount ($)", m["amount"]))
            amount_writes += 1
    if len(matches) > 30:
        log(f"  ... + {len(matches)-30} more")

    log(f"\n=== UNMATCHED (sent but no CRM row) ({len(no_match)}) ===")
    for nm in no_match[:20]:
        a_str = f"${nm['amount']:,.0f}" if nm["amount"] else "?"
        log(f"  {nm['proposal']['date'][:16]:<16} {a_str:>10}  {nm['extracted_name'][:60]}")
    if len(no_match) > 20:
        log(f"  ... + {len(no_match)-20} more")

    log(f"\n=== SUMMARY ===")
    log(f"  Total proposals scanned:     {len(proposals)}")
    log(f"  Matched to CRM:              {len(matches)}")
    log(f"  Bid Amount writes pending:   {amount_writes}")
    log(f"  Sent but missing from CRM:   {len(no_match)}")

    if not args.apply:
        log(f"\n[dry-run] use --apply to write {amount_writes} amounts to CRM")
        return

    if updates:
        try:
            n = batch_update_rows("Bid Log", updates)
            log(f"\n[audit] wrote {n} bid amounts to CRM")
        except Exception as e:
            log(f"\n[audit] write FAILED: {e}")
    else:
        log("\n[audit] no amounts to write")

    # === Add orphan rows — DISABLED by default ===
    # CRITICAL: NEVER auto-add rows for "outbound proposal-like" emails. Reasons:
    #   1. Internal team emails (CCF estimates@ -> cs@) match the same patterns
    #   2. Daily bid reports, draft proposal forwards, etc. all leak through
    #   3. The user's CRM Bid Log is a CURATED list — only user-confirmed bids
    # User must explicitly add new rows. Carol only fills blank fields on
    # rows that already exist.
    appended = 0
    # (--add-orphans flag is now a no-op; kept for backward compat)

    if not args.quiet:
        log("\nNote: 'unmatched' proposals weren't auto-added to CRM — review them manually.")
        log("Some may be follow-ups, addenda, or proposals on bids you've since archived.")


if __name__ == "__main__":
    main()
