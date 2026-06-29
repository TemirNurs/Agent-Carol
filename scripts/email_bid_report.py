#!/usr/bin/env python3
"""
Send daily bid report email. No arguments needed - reads bids and sends automatically.

Usage:
  python scripts/email_bid_report.py                              # send to default (Nursultan)
  python scripts/email_bid_report.py --to someone@email.com       # send to specific address
"""

import argparse
import json
import smtplib
import sys
from datetime import datetime, date, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
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

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent.parent
BIDS_FILE = BASE_DIR / "data" / "memory" / "active_bids.json"

SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SENDER_EMAIL = "estimates@carolinacommercialfinishes.com"
APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
DEFAULT_TO = "cs@carolinacommercialfinishes.com"


# Shared with bids_today.py — keep behavior consistent across all bid views.
_ST = {"north carolina":"NC","south carolina":"SC","virginia":"VA","west virginia":"WV",
       "georgia":"GA","tennessee":"TN","kentucky":"KY","alabama":"AL","florida":"FL",
       "maryland":"MD","ohio":"OH","pennsylvania":"PA","texas":"TX","new york":"NY"}
def _st(v):
    v = (v or "").strip()
    return _ST.get(v.lower(), (v[:2].upper() if v else ""))

def _clean_proj(name):
    """Strip the 'Food Lion 2118B - Dinwiddie - Food Lion 2118B' duplication
    that the CC scraper produced."""
    n = (name or "").strip()
    # If "X - Y - X" or "X - X" pattern, keep the first segment + any middle
    parts = [p.strip() for p in n.split(" - ")]
    if len(parts) >= 2 and parts[0].lower() == parts[-1].lower():
        parts = parts[:-1]
    return " - ".join(parts)[:60]

# Scopes that are almost always under the $50K minimum — skip in primary view
_SKIP_PATTERNS = [
    ("restroom", "restroom-only renovation"),
    ("toilet partitions", "toilet partitions only"),
    ("gatehouse", "single gatehouse"),
    ("metal roof retrofit", "roof-only — minimal paint"),
    ("trash enclosure", "trash enclosure only"),
    ("dunkin remodel", "fast-food remodel ~$25-40K"),
    ("taco bell", "ground-up fast food ~$25-40K"),
    ("7 brew", "small drive-thru"),
    ("wawa", "small ground-up — typically <$50K paint"),
    ("sheetz", "gas station — small"),
    ("autozone", "auto parts retail — small"),
    ("sally beauty", "small retail remodel"),
    ("circle k", "convenience store — small"),
    ("bojangles", "fast food — small"),
    ("rtop", "Ft. Bragg gov repair order — typically <$50K"),
    ("cook cdc", "gov gatehouse/utility — small"),
    ("reverse osmosis", "water-treatment specialty — minimal paint"),
    ("hvac replacement", "HVAC scope — no paint"),
]

def _classify(b):
    """Return ('SWEET' | 'FAR' | 'SKIP', reason)."""
    pn = (b.get("project_name") or "").lower()
    st = _st(b.get("state") or "")
    mi = b.get("distance_miles")
    for kw, why in _SKIP_PATTERNS:
        if kw in pn:
            return "SKIP", why
    if isinstance(mi, (int, float)) and mi > 150:
        return "FAR", f"{mi:.0f}mi past 2hr sweet spot"
    if st in ("NC", "SC") and (not isinstance(mi, (int, float)) or mi <= 120):
        return "SWEET", "local NC/SC"
    if isinstance(mi, (int, float)) and mi <= 120:
        return "SWEET", f"{mi:.0f}mi, in 2hr radius"
    return "FAR", "out-of-state, distance unknown"


def dedup_bids(bids):
    """Strict dedup: same project-core (store/bldg number + first distinctive
    token) + same GC company. Replaces the old loose fuzzy match that
    produced garbled 'X - Y - X' names by accident."""
    import re
    def core(name):
        s = (name or "").lower()
        num = re.search(r"#?\s*(\d{3,5})", s)
        toks = [t for t in re.findall(r"[a-z]{4,}", s)
                if t not in ("food","lion","store","center","plaza","retail",
                              "construction","renovation","building","buildings",
                              "remodel","facility","center")][:2]
        return ((num.group(1) if num else ""), tuple(toks))
    def gc_key(b):
        g = (b.get("gc_name") or b.get("gc") or "").lower()
        return re.sub(r"[^a-z0-9]+", "", g)[:18]

    groups = {}
    for b in bids:
        k = (core(b.get("project_name", "")), gc_key(b))
        if k not in groups:
            groups[k] = {"base": b, "gcs": [], "sources": set()}
        gc = (b.get("gc_name") or b.get("gc") or "").strip()
        src = b.get("source", "")
        if all(gc.lower()[:18] != gg.lower()[:18] for gg, _ in groups[k]["gcs"]):
            groups[k]["gcs"].append((gc, src))
        groups[k]["sources"].add(src)

    result = []
    for k, g in groups.items():
        b = g["base"].copy()
        b["project_name"] = _clean_proj(b.get("project_name", ""))
        gc_parts = []
        for gc, src in g["gcs"]:
            tag = "CC" if src == "constructconnect" else (
                "BC" if src == "buildingconnected" else src[:2].upper())
            gc_parts.append(f"{gc} ({tag})")
        b["gc_display"] = " | ".join(gc_parts)
        b["src_display"] = "/".join(sorted(set(
            "CC" if s == "constructconnect" else ("BC" if s == "buildingconnected" else s[:2].upper())
            for s in g["sources"])))
        result.append(b)
    # Sort: SWEET first, then FAR, then SKIP. Within each, by distance.
    bucket = {"SWEET": 0, "FAR": 1, "SKIP": 2}
    result.sort(key=lambda b: (bucket[_classify(b)[0]],
                                b.get("distance_miles") or 999))
    return result


def build_report():
    bids = json.load(open(BIDS_FILE, encoding="utf-8"))
    today = date.today()
    today_str = f"{today.month}/{today.day}/{today.year}"
    tomorrow = today + timedelta(days=1)
    tomorrow_str = f"{tomorrow.month}/{tomorrow.day}/{tomorrow.year}"

    today_bids = dedup_bids([b for b in bids if b.get("due_date") == today_str])
    tomorrow_bids = dedup_bids([b for b in bids if b.get("due_date") == tomorrow_str])

    def bid_row(b):
        src = b.get("src_display", "CC" if b.get("source") == "constructconnect" else "BC")
        d = b.get("distance_miles")
        dist = f"{d:.0f} mi" if isinstance(d, (int, float)) else "? mi"
        gc = b.get("gc_display", b.get("gc", ""))
        cat, why = _classify(b)
        verdict_chip = {
            "SWEET": ("🎯 BID", "#27ae60"),
            "FAR":   ("⚠️ FAR", "#e67e22"),
            "SKIP":  ("⛔ SKIP", "#95a5a6"),
        }[cat]
        chip = (f"<span style='background:{verdict_chip[1]};color:white;"
                f"padding:2px 6px;border-radius:4px;font-size:11px;'>"
                f"{verdict_chip[0]}</span>")
        loc = f"{b.get('city','')}, {_st(b.get('state',''))}".strip(", ")
        proj = b.get("project_name", "")[:55]
        bg = "#fff" if cat == "SWEET" else ("#fef9e7" if cat == "FAR" else "#f4f6f7")
        return (f"<tr style='background:{bg};border-bottom:1px solid #eee;'>"
                f"<td style='padding:4px;'>{chip}</td>"
                f"<td style='padding:4px;'>{src}</td>"
                f"<td style='padding:4px;'>{dist}</td>"
                f"<td style='padding:4px;'><b>{proj}</b><br>"
                f"<span style='color:#7f8c8d;font-size:11px;'>{why}</span></td>"
                f"<td style='padding:4px;'>{loc}</td>"
                f"<td style='padding:4px;font-size:12px;'>{gc[:60]}</td></tr>")

    # Verdict summary across today+tomorrow
    all_today = today_bids + tomorrow_bids
    sweet_n = sum(1 for b in all_today if _classify(b)[0] == "SWEET")
    far_n   = sum(1 for b in all_today if _classify(b)[0] == "FAR")
    skip_n  = sum(1 for b in all_today if _classify(b)[0] == "SKIP")

    th = ('<tr style="background:#2c3e50;color:white;">'
          '<th style="padding:6px;">Verdict</th><th>Src</th><th>Dist</th>'
          '<th>Project</th><th>Location</th><th>GC</th></tr>')

    html = f"""
    <html><body style="font-family: Arial, sans-serif; max-width: 820px;">
    <h2 style="color:#2c3e50;margin-bottom:4px;">CCF Daily Bid Report — {today.strftime('%A, %B %d, %Y')}</h2>
    <p style="color:#7f8c8d;margin-top:0;font-size:13px;">Carol · CCF Estimating · sorted by sweet-spot first</p>
    <p style="background:#ecf0f1;padding:8px 12px;border-radius:6px;font-size:14px;">
      <b>{sweet_n} worth bidding</b> · {far_n} far (case-by-case) · {skip_n} likely-skip (below $50K min)
    </p>
    <h3 style="color:#e74c3c;">Due TODAY · {len(today_bids)} bid{'s' if len(today_bids)!=1 else ''}</h3>
    <table style="border-collapse:collapse;width:100%;font-size:13px;">
    {th}
    {"".join(bid_row(b) for b in today_bids)}
    </table>
    <br>
    <h3 style="color:#f39c12;">Due TOMORROW · {len(tomorrow_bids)} bid{'s' if len(tomorrow_bids)!=1 else ''}</h3>
    <table style="border-collapse:collapse;width:100%;font-size:13px;">
    {th}
    {"".join(bid_row(b) for b in tomorrow_bids)}
    </table>
    <br>
    <p style="color:#7f8c8d;font-size:12px;">
      Total pipeline: {len(bids)} active bids · BC {sum(1 for b in bids if b.get('source')=='buildingconnected')}
      · CC {sum(1 for b in bids if b.get('source')=='constructconnect')}
      · email {sum(1 for b in bids if b.get('source')=='email')}<br>
      🎯 = NC/SC ≤120mi, likely $50K+ scope &nbsp;·&nbsp;
      ⚠️ = past 2hr radius (case-by-case) &nbsp;·&nbsp;
      ⛔ = likely under $50K (restroom/gatehouse/fast-food/etc.)
    </p>
    <hr>
    <p style="color:#95a5a6;font-size:11px;">Carolina Commercial Finishes · estimates@carolinacommercialfinishes.com</p>
    </body></html>
    """

    subject = f"CCF Daily Bid Report - {today.strftime('%B %d, %Y')} ({len(today_bids)} due today)"

    def plain_row(b):
        cat, why = _classify(b)
        tag = {"SWEET":"BID","FAR":"FAR","SKIP":"SKIP"}[cat]
        d = b.get("distance_miles")
        dist = f"{d:.0f}mi" if isinstance(d, (int, float)) else "?mi"
        gc = b.get("gc_display", b.get("gc", ""))
        loc = f"{b.get('city','')}, {_st(b.get('state',''))}".strip(", ")
        return (f"  [{tag:<4}] {dist:<6} {b.get('project_name','')[:42]:<42} "
                f"{loc:<22} {gc[:34]}\n         ↳ {why}\n")

    plain = (f"CCF Daily Bid Report - {today.strftime('%A, %B %d, %Y')}\n\n"
             f"Summary: {sweet_n} worth bidding · {far_n} far · {skip_n} likely-skip\n\n")
    plain += f"DUE TODAY: {len(today_bids)} projects\n"
    for b in today_bids: plain += plain_row(b)
    plain += f"\nDUE TOMORROW: {len(tomorrow_bids)} projects\n"
    for b in tomorrow_bids: plain += plain_row(b)
    plain += f"\nTotal pipeline: {len(bids)} bids\n"

    return subject, plain, html


def send(to, subject, plain, html):
    msg = MIMEMultipart("alternative")
    msg["From"] = f"Carol - CCF Estimating <{SENDER_EMAIL}>"
    msg["To"] = to
    msg["Subject"] = subject
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))

    server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
    server.starttls()
    server.login(SENDER_EMAIL, APP_PASSWORD)
    server.sendmail(SENDER_EMAIL, [to], msg.as_string())
    server.quit()
    return True


def main():
    parser = argparse.ArgumentParser(description="CCF Bid Report Emailer")
    parser.add_argument("--to", default=DEFAULT_TO, help="Recipient email")
    args = parser.parse_args()

    print(f"  Building bid report...")
    subject, plain, html = build_report()
    print(f"  Sending to {args.to}...")

    try:
        send(args.to, subject, plain, html)
        result = {"status": "sent", "to": args.to, "subject": subject}
        print(f"  Email sent successfully!")
    except Exception as e:
        result = {"status": "error", "error": str(e)}
        print(f"  ERROR: {e}")

    print(f"__RESULT__:{json.dumps(result)}")


if __name__ == "__main__":
    main()
