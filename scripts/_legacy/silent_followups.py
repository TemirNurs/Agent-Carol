#!/usr/bin/env python3
"""Identify the non-responders from yesterday's FU batch + sort by value."""
import sys, imaplib, email, re
from datetime import date

sys.path.insert(0, "scripts")
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except: pass
from email.header import decode_header
from crm_lib import get_sheet


SENT = ['BID-0004','BID-0005','BID-0006','BID-0008','BID-0009','BID-0011','BID-0013',
        'BID-0014','BID-0015','BID-0021','BID-0022','BID-0023','BID-0024','BID-0025',
        'BID-0026','BID-0027','BID-0028','BID-0030','BID-0035','BID-0037','BID-0038',
        'BID-0039','BID-0040','BID-0043','BID-0047','BID-0051','BID-0057','BID-0068',
        'BID-0070']


def decode_h(s):
    out = ''
    for p, e in decode_header(s or ''):
        out += p.decode(e or 'utf-8', errors='replace') if isinstance(p, bytes) else p
    return out


def amt(s):
    try: return int(str(s).replace('$', '').replace(',', '').split('.')[0])
    except: return 0


def main():
    M = imaplib.IMAP4_SSL("imap.gmail.com")
    M.login("estimates@carolinacommercialfinishes.com", "")
    M.select("INBOX")
    st, ids = M.search(None, '(SINCE "11-May-2026")')
    replied = set()
    for mid in (ids[0].split() if ids[0] else []):
        st, data = M.fetch(mid, '(BODY.PEEK[HEADER])')
        msg = email.message_from_bytes(data[0][1])
        fr = decode_h(msg.get("From", "")).lower()
        if any(x in fr for x in ("carolinacommercial", "mailer-daemon", "noreply",
                                  "no-reply", "team@buildingconnected", "notifications@")):
            continue
        subj = decode_h(msg.get("Subject", ""))
        m = re.search(r"(BID-\d+)", subj)
        if m: replied.add(m.group(1))
    M.logout()

    no_reply = [b for b in SENT if b not in replied]
    print(f"Sent yesterday: {len(SENT)}")
    print(f"Replied:        {len(replied)} — {sorted(replied)}")
    print(f"NO REPLY:       {len(no_reply)}")
    print()

    sh = get_sheet("Bid Log")
    hdrs = sh.row_values(1)
    rows = sh.get_all_values()
    bid_to_row = {}
    for r in rows[1:]:
        if r and r[0].strip():
            bid_to_row[r[0].strip()] = dict(zip(hdrs, r))

    details = []
    for bid in no_reply:
        d = bid_to_row.get(bid, {})
        emails_cell = (d.get("Contact Email", "") or "").strip()
        first_email = emails_cell.split()[0] if emails_cell else ""
        details.append({
            "bid": bid,
            "project": d.get("Project Name", "")[:44],
            "gc": d.get("GC / Client", "")[:26],
            "contact": d.get("Contact Name", "")[:18],
            "email": first_email,
            "amount": d.get("Bid Amount ($)", ""),
            "submitted": d.get("Bid Submitted Date", "")[:14],
        })

    details.sort(key=lambda x: -amt(x["amount"]))
    total = sum(amt(s["amount"]) for s in details)
    print(f"Combined value: ${total:,}")
    print()
    for s in details:
        amt_str = ("$" + s["amount"]) if s["amount"] and not s["amount"].startswith("$") else s["amount"]
        print(f"  {s['bid']:<10} {amt_str:<9}  {s['project']:<46}  GC: {s['gc']:<26}  contact: {s['contact']:<18}  sub: {s['submitted']}")


if __name__ == "__main__":
    main()
