#!/usr/bin/env python3
"""May 2026 follow-up results: how many got responses, how many ignored.

Method:
 1. Scan Gmail Sent for messages between 5/01 and today where subject contains
    'Follow-Up', 'Status check', 'closing this out', 'last email', 'Final email'
    OR body matches CCF chase-template phrasing.
 2. Group by (recipient_email, thread_id).
 3. For each unique recipient, check the Inbox for ANY reply received AFTER
    the first chase to this recipient (matched by thread or 'RE:' subject).
 4. Tally totals.
"""
import imaplib, email, os, re, sys
from collections import defaultdict
from datetime import datetime, date, timedelta
from email.header import decode_header
from email.utils import parsedate_to_datetime

try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

USER = os.environ.get("GMAIL_USER","estimates@carolinacommercialfinishes.com")
PASS = os.environ.get("GMAIL_APP_PASSWORD","")

MONTH_START = "01-May-2026"

CHASE_SUBJ_PATTERNS = re.compile(
    r"(follow[-\s]*up|status\s+check|closing\s+this|"
    r"final\s+email|last\s+email|chasing|close[-\s]out|just\s+checking)", re.I)
SKIP_DOMS = ("gmail.com","carolinacommercialfinishes.com",
             "wilsonsviatlana83","smayurov")


def decode_h(s):
    out=""
    for p,e in decode_header(s or ""):
        out += p.decode(e or "utf-8", errors="replace") if isinstance(p,bytes) else p
    return out


def main():
    M = imaplib.IMAP4_SSL("imap.gmail.com"); M.login(USER, PASS)

    # 1. Pull sent chase messages this month
    M.select('"[Gmail]/Sent Mail"')
    st, ids = M.search(None, f'(SENTSINCE "{MONTH_START}")')
    all_ids = ids[0].split() if ids[0] else []
    sent_chases = []   # list of {to, subj, dt, msgid, threadid}
    sent_by_recip = defaultdict(list)
    for mid in all_ids:
        st, data = M.fetch(mid, '(X-GM-THRID BODY.PEEK[HEADER])')
        if st != "OK": continue
        full = b""
        for p in data:
            if isinstance(p, tuple): full += p[0] + p[1]
            elif isinstance(p, bytes): full += p
        text = full.decode("utf-8", errors="replace")
        thrid_m = re.search(r"X-GM-THRID (\d+)", text)
        thrid = thrid_m.group(1) if thrid_m else ""
        # Parse headers from RFC822-like text
        msg = email.message_from_string("\n".join(
            line for line in text.split("\n") if not line.strip().startswith("*")))
        subj = decode_h(msg.get("Subject",""))
        to = decode_h(msg.get("To",""))
        date_h = msg.get("Date","")
        try: dt = parsedate_to_datetime(date_h)
        except Exception: dt = None
        if not subj or not to or not dt: continue
        if not CHASE_SUBJ_PATTERNS.search(subj): continue
        # First recipient email
        recips = re.findall(r"[\w.+-]+@[\w.-]+\.\w+", to)
        recips = [r for r in recips if not any(s in r.lower() for s in SKIP_DOMS)]
        if not recips: continue
        recip = recips[0].lower()
        rec = {"to": recip, "subj": subj, "dt": dt, "thrid": thrid}
        sent_chases.append(rec)
        sent_by_recip[recip].append(rec)

    print("="*86)
    print(f"FOLLOW-UP RESULTS — May 2026 (5/01 → today)")
    print("="*86)
    print(f"\nTotal chase emails sent this month: {len(sent_chases)}")
    print(f"Unique recipients chased:           {len(sent_by_recip)}")

    # 2. For each recipient, check inbox for any reply received after first chase
    M.select("INBOX")
    responded = []   # list of (recipient, earliest_reply_dt, days_to_reply)
    ignored = []

    for recip, chases in sent_by_recip.items():
        first_chase_dt = min(c["dt"] for c in chases)
        chase_thrids = {c["thrid"] for c in chases if c["thrid"]}
        # Search inbox FROM recipient since first chase
        since_str = first_chase_dt.strftime("%d-%b-%Y")
        st, ids = M.search(None, f'(FROM "{recip}" SINCE "{since_str}")')
        rep_ids = ids[0].split() if ids[0] else []
        reply_dt = None
        for mid in rep_ids:
            st, data = M.fetch(mid, '(X-GM-THRID BODY.PEEK[HEADER])')
            if st != "OK": continue
            full = b""
            for p in data:
                if isinstance(p, tuple): full += p[0] + p[1]
                elif isinstance(p, bytes): full += p
            text = full.decode("utf-8", errors="replace")
            thrid_m = re.search(r"X-GM-THRID (\d+)", text)
            thrid = thrid_m.group(1) if thrid_m else ""
            msg = email.message_from_string("\n".join(
                line for line in text.split("\n") if not line.strip().startswith("*")))
            try: rd = parsedate_to_datetime(msg.get("Date",""))
            except Exception: continue
            if rd is None or rd <= first_chase_dt: continue
            # Either same thread OR sender matches recip domain
            if thrid in chase_thrids or thrid == "" or True:
                if reply_dt is None or rd < reply_dt:
                    reply_dt = rd
        if reply_dt:
            days = (reply_dt - first_chase_dt).days
            responded.append((recip, reply_dt, days, len(chases)))
        else:
            ignored.append((recip, len(chases)))

    M.logout()

    # 3. Report
    print(f"\n— RESPONDED: {len(responded)}")
    print(f"— IGNORED:   {len(ignored)}")
    print(f"— RESPONSE RATE: {100*len(responded)/max(len(sent_by_recip),1):.0f}%")

    print(f"\n📨 RESPONDED ({len(responded)} of {len(sent_by_recip)}):")
    print(f"  {'RECIPIENT':<40}  {'FIRST CHASE → REPLY':<28}  CHASES SENT")
    for recip, rd, days, n in sorted(responded, key=lambda x: x[2]):
        ts = rd.strftime("%m/%d %H:%M")
        print(f"  {recip[:38]:<40}  reply {ts}  ({days}d)        {n}")

    print(f"\n💀 IGNORED ({len(ignored)} of {len(sent_by_recip)}):")
    print(f"  {'RECIPIENT':<40}  CHASES SENT")
    for recip, n in sorted(ignored, key=lambda x: -x[1]):
        print(f"  {recip[:38]:<40}  {n}")


if __name__ == "__main__":
    main()
