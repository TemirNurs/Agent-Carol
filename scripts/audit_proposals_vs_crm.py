#!/usr/bin/env python3
r"""
audit_proposals_vs_crm.py — Ground-truth reconciliation.

For every email under the Gmail 'Proposals Sent' label, determine whether it
is faithfully represented in the CRM Bid Log: present, correct amount, correct
recipient. Emits a punch list:

  OK            — in CRM, amount matches
  MISSING       — real proposal, NOT in CRM
  AMOUNT_DIFF   — in CRM but Bid Amount differs from the email's TOTAL
  NOISE         — not a real GC proposal (supplier quote-request / internal
                  self-send) — correctly absent, no action

Usage:
  python scripts/audit_proposals_vs_crm.py
  python scripts/audit_proposals_vs_crm.py --json
"""
from __future__ import annotations
import argparse, imaplib, email as email_lib, os, re, sys, json
from email.header import decode_header
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

from audit_combined_proposals import get_body
from track_submissions import extract_project_from_subject, extract_bid_total
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

GMAIL_USER = "estimates@carolinacommercialfinishes.com"
GMAIL_PASS = os.environ.get("GMAIL_APP_PASSWORD", "")
# Recipients that mean "not a real outbound GC proposal"
NOISE_RECIPIENTS = ("randallbrothers.com",)          # material supplier (we ask THEM for quotes)
INTERNAL = (("carolinacommercialfinishes.com",)
            + tuple(d for d in os.environ.get("CCF_OWN_DOMAINS", "carolinacommercialfinishes").split(",") if d)
            + tuple(a for a in os.environ.get("OWNER_ALIAS_EMAILS", "").split(",") if a))
FU_MARK = ("follow-up", "status check", "quick check", "calling tomorrow",
           "checking in", "circling back", "any update", "closing out")


def _amt_int(s):
    if not s: return None
    s = re.sub(r"[^\d.]", "", str(s).split(".")[0])
    try: return int(s)
    except Exception: return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    M = imaplib.IMAP4_SSL("imap.gmail.com")
    M.login(GMAIL_USER, GMAIL_PASS)
    sel = None
    for lbl in ('"Proposals Sent"', '"proposals-sent"'):
        st, _ = M.select(lbl, readonly=True)
        if st == "OK":
            sel = lbl; break
    st, ids = M.search(None, "ALL")
    allids = ids[0].split() if ids and ids[0] else []
    proposals = []
    for mid in allids:
        st, data = M.fetch(mid, '(BODY.PEEK[])')
        if st != "OK": continue
        msg = email_lib.message_from_bytes(data[0][1])
        subj = ""
        for p, e in decode_header(msg.get("Subject", "")):
            subj += p.decode(e or "utf-8", errors="replace") if isinstance(p, bytes) else p
        subj = subj.strip()
        sl = subj.lower()
        if sl.startswith(("re:", "fwd:")) or any(k in sl for k in FU_MARK):
            continue
        to = msg.get("To", "")
        m = re.search(r"<([^>]+)>", to)
        to_email = (m.group(1) if m else to).split(",")[0].strip().lower()
        body = get_body(msg)
        proposals.append({
            "subject": subj,
            "project": extract_project_from_subject(subj),
            "to": to_email,
            "amount": extract_bid_total(body),
            "date": msg.get("Date", "")[:25],
        })
    M.logout()

    # Load CRM
    sh = get_sheet("Bid Log")
    hdrs = sh.row_values(1)
    rows = sh.get_all_values()
    crm = []
    for r in rows[1:]:
        d = {h: (r[i] if i < len(r) else "") for i, h in enumerate(hdrs)}
        crm.append(d)

    STOPW = {"food", "lion", "store", "proposal", "bid", "ccf", "painting",
             "quinton", "chester", "building", "buildings", "revised", "the",
             "and", "for", "inc", "llc", "corp", "renovation", "renovations",
             "install", "installation", "wallcovering", "addition", "additions",
             "project", "facility", "center"}

    def core(s):
        s = (s or "").lower()
        num = ""
        mm = re.search(r"#?\s*(\d{3,5})", s)
        if mm:
            num = mm.group(1).lstrip("0") or "0"
        toks = [t for t in re.findall(r"[a-z]{4,}", s) if t not in STOPW]
        return num, set(toks)

    def same_project(a, b):
        """a,b = core() tuples. Match if store numbers agree (when both have
        one) OR there is strong keyword overlap."""
        (na, ta), (nb, tb) = a, b
        if na and nb:
            if na != nb:
                return False
            return True  # same store number = same project
        if not ta or not tb:
            return False
        ov = len(ta & tb)
        return ov >= 2 or (ov >= 1 and (len(ta) <= 2 or len(tb) <= 2))

    results = []
    for p in proposals:
        to = p["to"]
        if any(n in to for n in NOISE_RECIPIENTS):
            results.append(("NOISE", p, None, "material supplier quote-request"))
            continue
        if any(x in to for x in INTERNAL) and "estimates@" in to:
            results.append(("NOISE", p, None, "internal self-send (review copy)"))
            continue
        # Match to CRM by project-core (+ prefer same recipient domain)
        pc = core(p["project"])
        to_dom = to.split("@", 1)[1] if "@" in to else ""
        best = None
        for d in crm:
            if same_project(core(d.get("Project Name", "")), pc):
                cd = (d.get("Contact Email", "") or "").lower()
                if to_dom and to_dom in cd:
                    best = d; break          # exact GC match wins
                if best is None:
                    best = d                  # keep first project match as fallback
        if not best:
            results.append(("MISSING", p, None, "no CRM row for this project"))
            continue
        # Amount check
        em_amt = _amt_int(p["amount"])
        crm_amt = _amt_int(best.get("Bid Amount ($)", ""))
        if em_amt and crm_amt and abs(em_amt - crm_amt) > max(50, 0.02 * em_amt):
            results.append(("AMOUNT_DIFF", p, best,
                            f"email ${em_amt:,} vs CRM ${crm_amt:,}"))
        else:
            results.append(("OK", p, best, best.get("Status", "")))

    # Report
    order = {"MISSING": 0, "AMOUNT_DIFF": 1, "NOISE": 2, "OK": 3}
    results.sort(key=lambda x: (order[x[0]], x[1]["project"]))
    from collections import Counter
    c = Counter(r[0] for r in results)
    print(f"=== Proposals-Sent vs CRM — {len(proposals)} original proposals ===")
    print(f"  OK={c['OK']}  MISSING={c['MISSING']}  AMOUNT_DIFF={c['AMOUNT_DIFF']}  NOISE={c['NOISE']}")
    print()
    for tag, p, d, note in results:
        if tag == "OK": continue
        bid = (d.get("Bid #") if d else "") or "-"
        print(f"[{tag:<11}] {p['project'][:40]:<40} ${str(p['amount'] or '?'):<10} "
              f"-> {p['to'][:28]:<28} {bid}  {note}")
    print()
    print("OK rows (in CRM, amount matches):")
    for tag, p, d, note in results:
        if tag != "OK": continue
        print(f"  {d.get('Bid #','-'):<9} {p['project'][:38]:<38} ${str(p['amount'] or '?'):<10} [{note}]")

    if args.json:
        Path(ROOT / "data" / "logs" / "proposals_audit.json").write_text(
            json.dumps([{"tag": t, **p, "crm_bid": (d.get("Bid #") if d else None),
                         "note": n} for t, p, d, n in results], indent=2),
            encoding="utf-8")


if __name__ == "__main__":
    main()
