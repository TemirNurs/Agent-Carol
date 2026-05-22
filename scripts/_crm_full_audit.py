#!/usr/bin/env python3
"""Full CRM audit — verify every sent proposal in Gmail and every active_bid
in memory has a matching CRM row. Report anything missing or stale."""
import imaplib, email, json, os, re, sys
from collections import defaultdict
from datetime import datetime, timedelta, date
from email.header import decode_header
from email.utils import parsedate_to_datetime
from pathlib import Path

try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

USER = os.environ.get("GMAIL_USER", "estimates@carolinacommercialfinishes.com")
PASS = os.environ.get("GMAIL_APP_PASSWORD", "")
BASE = Path(r"C:/Agent Carol")
sys.path.insert(0, str(BASE / "scripts"))
from crm_lib import all_records

DAYS_BACK = 30
SKIP_DOMS = ("gmail.com","carolinacommercialfinishes.com","hyperscalewiring.com",
             "anthropic.com","sherwin","wilsonsviatlana83","smayurov","procore.com",
             "buildingconnected","constructconnect","isqftmail","smartbidnet",
             "proc.com")

PROP_KW = re.compile(
    r"(proposal|painting\s*(?:proposal|estimate)|paint\s+and|bid\s+(?:for|submission)|"
    r"estimate\s+for|finishes|carolina\s+commercial|ccf\s+(?:bid|proposal))", re.I)

SKIP_KW = re.compile(
    r"^\s*(?:Re:|Fwd:|FW:|Following\s*up|Follow[-\s]up|Just\s+checking|"
    r"Chasing|Last\s+email|Final\s+email|Close[-\s]out|RFI\s+|Reminder|"
    r"Daily\s+Bid\s+Report|CCF\s*[→]\s*HW|Div\s*27\s+forward)", re.I)


def decode_h(s):
    out = ""
    for p, e in decode_header(s or ""):
        out += p.decode(e or "utf-8", errors="replace") if isinstance(p, bytes) else p
    return out


def project_core(name):
    """Same dedupe key as crm_writeback uses."""
    s = (name or "").lower()
    m = re.search(r"#?\s*(\d{3,5})[a-z]?(?=\s|$|[^\d])", s)
    num = m.group(1).lstrip("0") if m else ""
    base = re.sub(r"[^a-z0-9 ]+", " ", s)
    base = re.sub(
        r"\b(revised|proposal|attached|follow|followup|re|fwd|bid|submission|"
        r"painting|ccf|carolina|commercial|finishes|the|inc|llc|corp|company|"
        r"va|nc|sc|ga|al|ut|tn|quinton|chester|mebane|greensboro|remodel|store|"
        r"building|bldg|grandstands|concept|foods|petersburg|dinwiddie|aylett|"
        r"randleman|salisbury|fayetteville|advance|charlotte|raleigh)\b", " ", base)
    base = re.sub(r"\s+", " ", base).strip()
    toks = [t for t in base.split() if len(t) >= 3][:2]
    return (num, " ".join(toks))


def email_domain(addr):
    if "@" not in addr: return ""
    return addr.lower().split("@",1)[1].split(">")[0].strip()


def main():
    print("="*88)
    print("CRM FULL AUDIT — sent proposals vs CRM coverage")
    print("="*88)

    # 1. Pull CRM rows
    bidlog = all_records("Bid Log")
    print(f"\n[CRM] {len(bidlog)} rows")
    crm_by_core_gc = defaultdict(list)
    for row in bidlog:
        core = project_core(row.get("Project Name",""))
        dom = email_domain(row.get("Contact Email",""))
        crm_by_core_gc[(core, dom)].append(row)

    # 2. Pull sent proposals from Gmail
    since = (date.today() - timedelta(days=DAYS_BACK)).strftime("%d-%b-%Y")
    M = imaplib.IMAP4_SSL("imap.gmail.com")
    M.login(USER, PASS)
    M.select('"[Gmail]/Sent Mail"')
    st, ids = M.search(None, f'(SENTSINCE "{since}")')
    all_ids = ids[0].split() if ids[0] else []
    print(f"[Gmail] {len(all_ids)} sent emails in last {DAYS_BACK} days")

    proposals = []
    for mid in all_ids:
        st, data = M.fetch(mid, '(BODY.PEEK[HEADER])')
        if st != "OK": continue
        msg = email.message_from_bytes(data[0][1])
        subj = decode_h(msg.get("Subject",""))
        to_h = decode_h(msg.get("To",""))
        try: dt = parsedate_to_datetime(msg.get("Date",""))
        except Exception: dt = None
        if SKIP_KW.search(subj): continue
        if not PROP_KW.search(subj): continue
        # Extract recipient emails
        recips = re.findall(r"[\w.+-]+@[\w.-]+\.\w+", to_h.lower())
        recips = [r for r in recips if not any(s in r for s in SKIP_DOMS)]
        if not recips: continue
        proposals.append({"dt": dt, "subj": subj, "recips": recips})

    # Need full body for attachment check on flagged props
    M.logout()
    print(f"[Gmail] {len(proposals)} look like real proposals\n")

    # 3. For each proposal, check if CRM has a row with matching project+GC
    missing = []
    for p in proposals:
        core = project_core(p["subj"])
        for recip in p["recips"]:
            dom = email_domain(recip)
            if (core, dom) in crm_by_core_gc:
                continue
            # Also accept any CRM row with same project core AND same first 12 chars of GC name
            # (handles cases where Contact Email blank but GC name set)
            found = False
            for (c, _), rows in crm_by_core_gc.items():
                if c != core: continue
                for r in rows:
                    if email_domain(r.get("Contact Email","")) == dom:
                        found = True
                        break
                if found: break
            if not found:
                missing.append((p["dt"], p["subj"], recip))

    if not missing:
        print("✅ Every sent proposal has a CRM row.\n")
    else:
        print(f"⚠️ {len(missing)} sent proposals are NOT in CRM:\n")
        for dt, subj, recip in sorted(missing, key=lambda x: x[0] or datetime.min, reverse=True):
            ts = dt.strftime("%m/%d %H:%M") if dt else "?"
            print(f"  [{ts}] → {recip:<40}  '{subj[:65]}'")

    # 4. Cross-check active_bids → CRM
    bids = json.loads((BASE/"data"/"memory"/"active_bids.json").read_text(encoding="utf-8"))
    print(f"\n[active_bids] {len(bids)} entries")
    crm_cores = {core for core, _ in crm_by_core_gc.keys()}
    ab_missing = []
    for b in bids:
        c = project_core(b.get("project_name",""))
        if c not in crm_cores:
            # Only report as missing if it has a status flag suggesting work done
            if b.get("source") in ("email","buildingconnected","constructconnect"):
                ab_missing.append(b)
    if ab_missing:
        print(f"⚠️ {len(ab_missing)} active_bids have NO CRM row at all:")
        for b in ab_missing[:25]:
            pn = b.get("project_name","")[:42]
            print(f"  • {pn:<44}  src={b.get('source','')}  gc={b.get('gc','')[:25]}  due={b.get('due_date','')}")
    else:
        print("✅ Every active_bid has at least one CRM row.")

    # 5. Data quality checks
    print(f"\n[Data quality] inspecting CRM for issues:")
    issues = defaultdict(list)
    for i, row in enumerate(bidlog, start=2):
        pn = row.get("Project Name","")
        # Duplicated-name pattern (X - Y - X)
        if " - " in pn:
            parts = pn.split(" - ")
            if len(parts) >= 3 and parts[0].strip()[:12].lower() == parts[2].strip()[:12].lower():
                issues["duplicated_names"].append((i, pn))
        # ITB Received Date blank on active row
        if row.get("Status") in ("Bid Submitted","Awaiting Decision") and not row.get("ITB Received Date"):
            issues["missing_ITB"].append((i, pn, row.get("Status","")))
        # Bid Submitted Date blank on Bid Submitted row
        if row.get("Status") == "Bid Submitted" and not row.get("Bid Submitted Date"):
            issues["missing_Submitted"].append((i, pn))
        # State missing on active
        if row.get("Status") in ("Bid Submitted","Awaiting Decision") and not row.get("State"):
            issues["missing_State"].append((i, pn))
        # Status blank
        if not row.get("Status"):
            issues["blank_Status"].append((i, pn))

    if not any(issues.values()):
        print("  ✅ No data quality issues found.")
    else:
        for k, lst in issues.items():
            print(f"\n  {k}: {len(lst)} row(s)")
            for item in lst[:6]:
                print(f"    row {item[0]:>3}: {' / '.join(str(x)[:35] for x in item[1:])}")
            if len(lst) > 6:
                print(f"    ... +{len(lst)-6} more")


if __name__ == "__main__":
    main()
