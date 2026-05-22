#!/usr/bin/env python3
"""
check_replies.py - Live Gmail scan for replies to our follow-up emails.

Carol runs this when asked "how many GCs replied / any responses / who replied".
Reads LIVE from the Gmail Inbox (not the activity log) — always current.

Usage:
  python scripts/check_replies.py                # today's follow-ups
  python scripts/check_replies.py --days 7       # past 7 days
  python scripts/check_replies.py --since 2026-05-10
"""
from __future__ import annotations
import argparse
import imaplib
import email as email_lib
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

GMAIL_USER = os.environ.get("GMAIL_USER", "estimates@carolinacommercialfinishes.com")
GMAIL_PASS = os.environ.get("GMAIL_APP_PASSWORD", "")


def decode_h(value):
    if not value: return ""
    out = ""
    for p, e in decode_header(value):
        out += p.decode(e or "utf-8", errors="replace") if isinstance(p, bytes) else p
    return out


def _html_to_text(html: str) -> str:
    """Strip HTML to plain text — basic but enough for classification."""
    import re as _re
    s = _re.sub(r"<style[^>]*>.*?</style>", " ", html, flags=_re.S | _re.I)
    s = _re.sub(r"<script[^>]*>.*?</script>", " ", s, flags=_re.S | _re.I)
    s = _re.sub(r"<br\s*/?>", "\n", s, flags=_re.I)
    s = _re.sub(r"</p>", "\n", s, flags=_re.I)
    s = _re.sub(r"<[^>]+>", "", s)
    # Unescape common HTML entities
    s = s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
    s = s.replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'")
    s = _re.sub(r"\n\s*\n+", "\n\n", s)
    return s.strip()


def get_body(msg):
    """Extract message body. Prefers text/plain, falls back to HTML→text."""
    text_plain = ""
    text_html = ""
    try:
        if msg.is_multipart():
            for part in msg.walk():
                ct = part.get_content_type()
                if ct == "text/plain" and not text_plain:
                    try:
                        text_plain = part.get_payload(decode=True).decode("utf-8", errors="replace")
                    except Exception: pass
                elif ct == "text/html" and not text_html:
                    try:
                        text_html = part.get_payload(decode=True).decode("utf-8", errors="replace")
                    except Exception: pass
        else:
            ct = msg.get_content_type()
            payload = msg.get_payload(decode=True)
            if payload:
                decoded = payload.decode("utf-8", errors="replace")
                if ct == "text/html":
                    text_html = decoded
                else:
                    text_plain = decoded
    except Exception:
        pass
    # Prefer text/plain if it has real content, else HTML→text
    if text_plain.strip():
        return text_plain
    if text_html.strip():
        return _html_to_text(text_html)
    return ""


# Quick heuristic classifier (no LLM call — fast & deterministic)
def quick_classify(body: str, subject: str) -> str:
    if not body:
        return "EMPTY (auto-ack?)"
    b = body.lower()
    # LOST
    if any(p in b for p in [
        "went with another", "another contractor", "another sub",
        "not been selected", "weren't selected", "wasn't selected",
        "not the lowest", "different painter", "different sub",
        "not awarded to you", "not awarded to ccf",
    ]):
        return "LOST"
    # WON
    if any(p in b for p in [
        "congratulations", "you've been awarded", "you have been awarded",
        "selected your bid", "going with your bid", "awarded to you",
        "awarded to ccf", "we'd like to award",
    ]):
        return "WON"
    # NOT BIDDING (they're not participating)
    if any(p in b for p in [
        "not bidding", "not pursuing", "passing on this",
        "decided not to bid", "won't be bidding",
    ]):
        return "NOT BIDDING"
    # OUT OF OFFICE
    if any(p in b for p in [
        "out of office", "out of the office", "automatic reply",
        "auto-reply", "vacation", "i'll be back", "currently away",
    ]):
        return "OUT OF OFFICE"
    # PRICING
    if any(p in b for p in [
        "your price is high", "too high", "over budget", "your number is high",
        "can you revise", "revise pricing", "lower your", "10% high",
        "20% high", "% over", "% above", "compared to others",
    ]):
        return "PRICING"
    # STILL AWAITING (most common signal)
    if any(p in b for p in [
        "still pending", "still reviewing", "no update", "no decision",
        "still awaiting", "still waiting", "still under review",
        "owners review", "owner's review", "owner review",
        "in review", "evaluating", "haven't decided", "have not decided",
        "decision hasn't been made", "check back", "give us a few weeks",
        "looping in", "review with the team", "early to provide",
        "as pending", "not yet awarded", "yet to award",
        "share bid results", "share the bid results",
        "once we have received", "once we receive",
    ]):
        return "STILL_AWAITING"
    return "UNCLEAR"


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--days", type=int, default=1,
                   help="Look back N days for inbound replies (default 1 = today)")
    g.add_argument("--since", help="YYYY-MM-DD")
    args = ap.parse_args()

    if args.since:
        since = datetime.strptime(args.since, "%Y-%m-%d").date()
    else:
        since = date.today() - timedelta(days=max(0, args.days - 1))

    since_imap = since.strftime("%d-%b-%Y")

    M = imaplib.IMAP4_SSL("imap.gmail.com")
    M.login(GMAIL_USER, GMAIL_PASS)
    M.select("INBOX")

    # Pull all inbound emails since the cutoff
    st, ids = M.search(None, f'(SINCE "{since_imap}")')
    if st != "OK" or not ids[0]:
        print(f"No inbound mail since {since_imap}.")
        M.logout(); return

    replies = []
    for mid in ids[0].split():
        st, data = M.fetch(mid, '(BODY.PEEK[])')
        if st != "OK": continue
        msg = email_lib.message_from_bytes(data[0][1])
        fr = decode_h(msg.get("From", ""))
        if "mailer-daemon" in fr.lower() or "postmaster" in fr.lower(): continue
        if "carolinacommercialfinishes" in fr.lower(): continue
        subj = decode_h(msg.get("Subject", ""))
        # Must reference a BID# AND be a reply (Re:/RE: or include "Follow-Up")
        if "BID-" not in subj: continue
        if not re.match(r"^(re|fwd|fw):", subj, re.I) and "Follow-Up" not in subj:
            continue
        m = re.search(r"(BID-\d+)", subj)
        bid = m.group(1) if m else "?"
        body = get_body(msg)
        # Strip quoted prior message for cleaner classification
        body_clean = re.split(r"(?:On .* wrote:|-----Original Message)", body)[0]
        cat = quick_classify(body_clean, subj)
        # Pull sender name
        m2 = re.match(r'\s*"?([^"<]+?)"?\s*<', fr)
        sender_name = m2.group(1).strip() if m2 else fr.split("@")[0]
        # Pull project from subject
        proj = re.sub(r"^(Re:|RE:|Fwd:|FW:|Fw:)\s*", "", subj, flags=re.I).strip()
        proj = re.sub(r"^Follow-Up:\s*", "", proj).strip()
        proj = re.sub(r"\s*\(BID-\d+\)$", "", proj).strip()
        replies.append({
            "bid": bid,
            "project": proj,
            "sender": sender_name,
            "from_email": fr,
            "date_raw": msg.get("Date", ""),
            "category": cat,
            "preview": body_clean.strip()[:140].replace("\n", " "),
        })
    M.logout()

    if not replies:
        days_str = f"past {args.days} day(s)" if args.days > 0 else f"since {since}"
        print(f"No GC replies in the {days_str}.")
        return

    # Sort by date
    from email.utils import parsedate_to_datetime
    def _d(r):
        try: return parsedate_to_datetime(r["date_raw"])
        except Exception: return datetime.min
    replies.sort(key=_d)

    # Bucket counts
    by_cat = {}
    for r in replies:
        by_cat[r["category"]] = by_cat.get(r["category"], 0) + 1

    print(f"=== {len(replies)} GC replies since {since.isoformat()} ===")
    print(f"Breakdown: " + " · ".join(f"{c}: {n}" for c, n in sorted(by_cat.items(), key=lambda x: -x[1])))
    print()
    for r in replies:
        try:
            d = parsedate_to_datetime(r["date_raw"])
            d_str = d.strftime("%a %m/%d %H:%M")
        except Exception:
            d_str = r["date_raw"][:20]
        print(f"  [{r['category']:<14}] {r['bid']:<10} {d_str:<18} from {r['sender'][:25]:<25}")
        print(f"    Project: {r['project'][:60]}")
        if r["preview"]:
            print(f"    \"{r['preview'][:120]}...\"")
        print()


if __name__ == "__main__":
    main()
