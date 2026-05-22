#!/usr/bin/env python3
r"""
followup_intelligence_brief.py — Morning per-bid follow-up intelligence.

For every active bid (Bid Submitted / Awaiting Decision), report:
  • how many chases sent, to whom, when (last 60 days)
  • the LATEST reply from that GC contact (date + classified content)
  • what they said (one-liner)
  • the next-action recommendation (chase / wait / escalate / close-out /
    redirect to PM)

Output:
  - Markdown to stdout (for email)
  - Telegram-ready compressed summary
  - data/memory/followup_intel_YYYY-MM-DD.json (auditable record)

Run from daemon at 05:00 ET daily.

Rules followed (AGENTS_LESSONS.md):
  - Uses Internal ID as primary key (not Bid#)
  - All Gmail FETCHes use BODY.PEEK[] (won't mark Read)
  - has_replied_recently semantics — recognize reply content as intel
  - Filters UNCLEAR/OUT_OF_OFFICE noise from the recommendation engine

Usage:
  python scripts/followup_intelligence_brief.py            # print + telegram
  python scripts/followup_intelligence_brief.py --no-telegram
  python scripts/followup_intelligence_brief.py --email    # also email it
  python scripts/followup_intelligence_brief.py --days 60  # chase history window
"""
from __future__ import annotations
import argparse, imaplib, email as email_lib, json, os, re, subprocess, sys
from collections import defaultdict, Counter
from datetime import datetime, date, timedelta
from email.header import decode_header
from email.utils import parsedate_to_datetime
from pathlib import Path

try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from crm_lib import get_sheet

GMAIL_USER = os.environ.get("GMAIL_USER", "estimates@carolinacommercialfinishes.com")
GMAIL_PASS = os.environ.get("GMAIL_APP_PASSWORD", "")
BS_FILE = ROOT / "data" / "memory" / "bid_status.json"
ACTIVE_STATUSES = ("Bid Submitted", "Awaiting Decision")

# ────────────────────────────────────────────────────────────────────────────
# Reply content classifiers — semantic intel, not just "did they reply"
# ────────────────────────────────────────────────────────────────────────────
REPLY_PATTERNS = [
    ("AWARDED_TO_GC",   re.compile(r"(?:project|this)\s+(?:has\s+been\s+)?awarded\s+to", re.I),
     "GC won the project — sub-award decision pending. Switch contact to PM."),
    ("AWARDED_TO_US",   re.compile(r"(?:awarded?\s+to\s+you|congrat|you\s+(?:are|got|won)\s+(?:the|our)\s+(?:bid|job|project))", re.I),
     "We won — switch to project kickoff workflow."),
    ("LOST",            re.compile(r"(?:we\s+(?:went|chose|selected|awarded)\s+(?:another|other|different)|"
                                    r"not\s+(?:selected|low\s+bidder|chosen|going\s+forward|awarded)|"
                                    r"(?:we|you)\s+(?:were|was)\s+not\s+(?:awarded|selected|the)|"
                                    r"did\s+not\s+consider\s+your\s+bid|"
                                    r"incorrect\s+(?:scope|flooring|trade|division)|"
                                    r"wrong\s+(?:scope|trade)|"
                                    r"appreciate.*but)", re.I),
     "We lost — close out."),
    ("PRICING_PUSHBACK",re.compile(r"(?:thank\s+you\s+for\s+(?:the\s+)?(?:breakdown|detail|info)|"
                                    r"can\s+you\s+(?:break|provide|share)|"
                                    r"clarif(?:y|ication)|"
                                    r"questions?\s+(?:on|about|regarding))", re.I),
     "GC engaged — answering their question is the next move."),
    ("REDIRECT_PM",     re.compile(r"(?:please\s+(?:reach\s+out\s+to|contact)|reach\s+out\s+to\s+\w+|contact\s+(?:the|our)\s+(?:pm|project\s+manager))", re.I),
     "Contact redirected us to PM. Update CRM contact + chase the PM instead."),
    ("STILL_PENDING",   re.compile(r"(?:still\s+(?:showing\s+as\s+)?pending|not\s+yet\s+awarded|no\s+(?:news|update|decision)\s+yet|owner\s+(?:hasn'?t|has\s+not)|stand\s*by|under\s+review)", re.I),
     "Still pending — quiet wait. Re-check in 2 weeks."),
    ("CHECK_BACK_LATER",re.compile(r"(?:check\s+back\s+in|circle\s+back|reach\s+out\s+(?:again\s+)?in|let\s+(?:us|you)\s+know\s+(?:once|when))", re.I),
     "Asked us to wait — note their suggested timeline."),
    ("PRICING_QUESTION",re.compile(r"(?:price\s+(?:is\s+)?(?:too\s+)?high|sharpen.*pencil|revise.*pric|reduce.*price|drop.*number|come\s+down)", re.I),
     "Pricing pushback — consider revising or alternates."),
    ("OUT_OF_OFFICE",   re.compile(r"(?:out\s+of\s+(?:the\s+)?office|on\s+vacation|on\s+leave|auto-?reply)", re.I),
     "Auto-reply — ignore, contact will respond later."),
]

# Construction-context whitelist (so we don't classify spam as intel)
GC_DOMAINS = re.compile(
    r"(@[\w.-]*(?:construction|builders|cm\.com|construc|engineering|"
    r"contract|builds|cm-|cm\.|pkwycon|fiicgc|lfjennings|monteith|"
    r"newco|salcoacontracting|csgcharleston|metrolinabuilders|"
    r"pathcc|integrity-cm|mreconstructionllc|horizonretail|"
    r"flblum|williamsco|provost|rcsconstruction|vertexconstruction|"
    r"hbquickbuild|sauer|catamount|monteithco|wcconstructionco|"
    r"benchmarkbuilding|wedconstruction|wedentmonconst|windlecc|"
    r"rickshipman|delauterinc|valiantconstruct|wimcocorp|"
    r"actionrcs|cmcbuildinginc|criticalpathsolutions|baytobayprop))",
    re.I,
)


def decode_h(s):
    out = ""
    for p, e in decode_header(s or ""):
        out += p.decode(e or "utf-8", errors="replace") if isinstance(p, bytes) else p
    return out


def body_text(msg):
    if msg.is_multipart():
        for p in msg.walk():
            if p.get_content_type() == "text/plain":
                try: return p.get_payload(decode=True).decode("utf-8", errors="replace")
                except Exception: pass
        for p in msg.walk():
            if p.get_content_type() == "text/html":
                try:
                    html = p.get_payload(decode=True).decode("utf-8", errors="replace")
                    return re.sub(r"<[^>]+>", " ", html)
                except Exception: pass
    else:
        try: return msg.get_payload(decode=True).decode("utf-8", errors="replace")
        except Exception: pass
    return ""


def clean_reply_body(text):
    """Strip quoted reply chain + signatures. Keep just the actual new content."""
    if not text: return ""
    lines = []
    for line in text.split("\n"):
        s = line.strip()
        if not s: continue
        # Stop at quoted block
        if s.startswith(">"): break
        if re.match(r"^On\s+\w+,?\s+\w+\s+\d+,?\s+\d+", s): break
        if re.match(r"^From:\s+", s): break
        if re.match(r"^-{2,}\s*(?:Original\s+Message|Forwarded)", s, re.I): break
        # Stop at signature
        if re.match(r"^(Best|Thanks|Regards|Sincerely|Cheers|Sent\s+from)", s, re.I): break
        lines.append(s)
    return " ".join(lines)[:400]


def classify_reply(body):
    """Classify a reply body into a category + recommended action.
       Returns (category, recommendation, one_liner)."""
    clean = clean_reply_body(body)
    if not clean:
        return ("UNCLEAR", "Re-read manually — empty body after strip.", "")
    for cat, pat, rec in REPLY_PATTERNS:
        if pat.search(clean):
            # First sentence as the intel one-liner
            one_liner = re.split(r"(?<=[.!?])\s+", clean)[0][:200]
            return (cat, rec, one_liner)
    one_liner = re.split(r"(?<=[.!?])\s+", clean)[0][:200]
    return ("UNCLEAR", "Manual review — doesn't match known intel patterns.", one_liner)


def first_name(s):
    s = (s or "").strip()
    if not s: return ""
    return s.split()[0].rstrip(",;:.")


def fmt_dollars(amt):
    try:
        n = int(re.sub(r"[^\d]", "", str(amt))) if amt else 0
        return f"${n:,}" if n else "—"
    except Exception: return "—"


def parse_amt(s):
    if not s: return 0
    try: return int(re.sub(r"[^\d]", "", str(s)))
    except Exception: return 0


def parse_d(s):
    if not s: return None
    s = str(s).strip()
    # BUG FIX: don't truncate to len(fmt) — "%m/%d/%Y" is 8 chars but
    # "05/04/2026" is 10. Truncation parsed "05/04/20" as year 2020 → "2247d open".
    for fmt in ("%m/%d/%Y","%m/%d/%y","%Y-%m-%d"):
        try: return datetime.strptime(s, fmt).date()
        except Exception: pass
    # Last-resort: strip everything after the year and re-try
    m = re.match(r"^(\d{1,2}/\d{1,2}/\d{2,4})", s)
    if m:
        for fmt in ("%m/%d/%Y","%m/%d/%y"):
            try: return datetime.strptime(m.group(1), fmt).date()
            except Exception: pass
    return None


# ────────────────────────────────────────────────────────────────────────────
# Main intelligence assembly
# ────────────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60,
                    help="Chase + reply history window (default 60 days)")
    ap.add_argument("--no-telegram", action="store_true")
    ap.add_argument("--email", action="store_true",
                    help="Also email the brief to cs@carolinacommercialfinishes.com")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    today = date.today()
    cutoff = today - timedelta(days=args.days)

    # 1. Pull active CRM rows
    ws = get_sheet("Bid Log")
    rows = ws.get_all_values()
    hdr = rows[0]
    H = {h: i for i, h in enumerate(hdr)}
    crm_active = []
    for r in rows[1:]:
        if len(r) < len(hdr): continue
        status = r[H["Status"]] if "Status" in H else ""
        if status not in ACTIVE_STATUSES: continue
        crm_active.append({
            "internal_id": r[H["Internal ID"]] if "Internal ID" in H else "",
            "bid_id":      r[H["Bid #"]] if "Bid #" in H else "",
            "project":     r[H["Project Name"]] if "Project Name" in H else "",
            "gc":          r[H["GC / Client"]] if "GC / Client" in H else "",
            "contact":     r[H["Contact Name"]] if "Contact Name" in H else "",
            "email":       (r[H["Contact Email"]] if "Contact Email" in H else "").strip().lower(),
            "submitted":   parse_d(r[H["Bid Submitted Date"]] if "Bid Submitted Date" in H else ""),
            "amount":      parse_amt(r[H["Bid Amount ($)"]] if "Bid Amount ($)" in H else ""),
            "status":      status,
            "state":       r[H["State"]] if "State" in H else "",
            "notes":       r[H["Notes"]] if "Notes" in H else "",
        })

    # 2. Pull chase history from bid_status.json
    bs = json.loads(BS_FILE.read_text(encoding="utf-8")) if BS_FILE.exists() else {"history":[]}
    history = bs.get("history", [])
    chases_by_recip = defaultdict(list)  # email → list of (dt, subject, bid_id_label)
    for h in history:
        if h.get("trigger") not in ("chase","chase_silent","chase_today",
                                     "chase_silent_followups","followup"):
            continue
        recip = (h.get("to_email") or "").strip().lower()
        if not recip: continue
        try: dt = datetime.fromisoformat(h.get("at",""))
        except Exception: continue
        if dt.date() < cutoff: continue
        chases_by_recip[recip].append((dt, h.get("subject","") or "", h.get("bid_id","") or ""))

    # 3. Pull replies from Gmail Inbox per recipient
    M = imaplib.IMAP4_SSL("imap.gmail.com")
    M.login(GMAIL_USER, GMAIL_PASS)
    M.select("INBOX")
    since = cutoff.strftime("%d-%b-%Y")
    replies_by_recip = defaultdict(list)  # email → list of (dt, subject, body)
    for em in set(b["email"] for b in crm_active if b["email"]):
        try:
            st, ids = M.search(None, f'(FROM "{em}" SINCE "{since}")')
            mids = ids[0].split() if ids[0] else []
            for mid in mids[-5:]:  # last 5 replies max
                st, data = M.fetch(mid, '(BODY.PEEK[])')
                if st != "OK": continue
                msg = email_lib.message_from_bytes(data[0][1])
                try: rdt = parsedate_to_datetime(msg.get("Date",""))
                except Exception: continue
                if rdt is None: continue
                subj = decode_h(msg.get("Subject",""))
                bd = body_text(msg)
                replies_by_recip[em].append((rdt, subj, bd))
        except Exception:
            continue
    M.logout()

    # 4. Build per-bid intelligence records, grouped by recipient
    intel_by_recip = defaultdict(list)
    for b in crm_active:
        em = b["email"]
        if not em:
            intel_by_recip["(no contact email)"].append({**b, "chases": [], "reply": None})
            continue
        # Chases that match this bid (by project keyword OR by bid_id label)
        bid_chases = []
        proj_kw = re.sub(r"[^a-z0-9 ]","", b["project"].lower()).split()
        proj_kw = [w for w in proj_kw if len(w) >= 4][:3]
        for dt, subj, bid_label in chases_by_recip.get(em, []):
            sl = subj.lower()
            if b["bid_id"] and b["bid_id"] in subj:
                bid_chases.append((dt, subj))
            elif any(k in sl for k in proj_kw):
                bid_chases.append((dt, subj))
        # Most recent reply from this recipient about this project (after first chase)
        recent_reply = None
        first_chase_dt = min((c[0] for c in bid_chases), default=None)
        for rdt, rsubj, rbody in replies_by_recip.get(em, []):
            if first_chase_dt and rdt.replace(tzinfo=None) < first_chase_dt.replace(tzinfo=None):
                continue
            sl = rsubj.lower()
            # Match by project keyword in reply subject (RE: Follow-Up: …)
            if any(k in sl for k in proj_kw) or (b["bid_id"] and b["bid_id"] in rsubj):
                if recent_reply is None or rdt > recent_reply[0]:
                    recent_reply = (rdt, rsubj, rbody)
        intel_by_recip[em].append({
            **b,
            "chases": bid_chases,
            "reply": recent_reply,
        })

    # 5. Per-bid next-action recommendation
    def next_action(rec):
        if rec["reply"]:
            _rdt, _rs, rbody = rec["reply"]
            cat, action_text, one_liner = classify_reply(rbody)
            return cat, action_text, one_liner, _rdt
        # No reply
        if not rec["chases"]:
            sub_age = (today - rec["submitted"]).days if rec["submitted"] else 0
            if sub_age >= 3:
                return ("NO_CHASE_YET", "Send first follow-up — submitted "
                        f"{sub_age}d ago with no chase recorded.", "", None)
            return ("FRESH", "Just submitted — wait 72h before first chase.", "", None)
        n = len(rec["chases"])
        last_chase_dt = max(c[0] for c in rec["chases"]).date()
        days_since = (today - last_chase_dt).days
        sub_age = (today - rec["submitted"]).days if rec["submitted"] else 0
        if sub_age >= 60 and n >= 5:
            return ("CLOSE_OUT", f"Open {sub_age}d, {n} chases, zero reply — "
                    "send close-out note or move to phone.", "", None)
        if n >= 5 and days_since < 5:
            return ("WAIT", f"Already chased {n}× in {args.days}d, last {days_since}d ago. "
                    "Hold next chase 5+ days.", "", None)
        if days_since >= 14:
            return ("CHASE_DUE", f"{days_since}d since last chase, no reply. "
                    "Send next chase (escalating tone).", "", None)
        return ("WAIT", f"Chased {n}× recently — wait for cadence.", "", None)

    # ───────────────────────── Output ─────────────────────────
    lines_md = []
    lines_md.append(f"# 📊 Follow-Up Intelligence Brief — {today.strftime('%A, %B %d, %Y')}\n")

    # Summary
    total_active = len(crm_active)
    has_reply = sum(1 for items in intel_by_recip.values() for it in items if it.get("reply"))
    no_chase  = sum(1 for items in intel_by_recip.values() for it in items
                    if not it.get("chases") and not it.get("reply"))
    chase_due = 0
    close_out = 0
    redirect  = 0
    next_today = []
    by_recip_summary = []
    for em, items in intel_by_recip.items():
        for it in items:
            cat, action_text, one_liner, rdt = next_action(it)
            if cat == "CHASE_DUE" or cat == "NO_CHASE_YET": chase_due += 1
            if cat == "CLOSE_OUT": close_out += 1
            if cat == "REDIRECT_PM": redirect += 1
            if cat in ("CHASE_DUE","NO_CHASE_YET","CLOSE_OUT"):
                next_today.append((it["amount"], em, it))

    lines_md.append(f"## Snapshot\n")
    lines_md.append(f"- Active submitted bids:   **{total_active}**")
    lines_md.append(f"- Have reply on record:     **{has_reply}**")
    lines_md.append(f"- Never chased yet:         **{no_chase}**")
    lines_md.append(f"- Chase due today:          **{chase_due}**")
    lines_md.append(f"- Recommend close-out:      **{close_out}**")
    lines_md.append(f"- Contact redirect needed:  **{redirect}**")
    lines_md.append("")

    # Per-contact intelligence
    lines_md.append("## 📞 Per-contact intelligence")
    sorted_recipients = sorted(intel_by_recip.items(),
                               key=lambda kv: -max((b["amount"] for b in kv[1]), default=0))
    for em, items in sorted_recipients:
        total_amt = sum(b["amount"] for b in items)
        gcs = sorted({b["gc"] for b in items if b["gc"]})
        contact = items[0].get("contact","") or em.split("@")[0]
        lines_md.append(f"\n### {first_name(contact)} <{em}> — {gcs[0] if gcs else '?'}")
        lines_md.append(f"  _{len(items)} bid(s), total {fmt_dollars(total_amt)}_")
        for b in sorted(items, key=lambda x: -x["amount"]):
            cat, action_text, one_liner, rdt = next_action(b)
            # Bid header
            sub_age = (today - b["submitted"]).days if b["submitted"] else 0
            n_chase = len(b["chases"])
            ch_str = (f"chased {n_chase}× (last {max(c[0] for c in b['chases']).strftime('%m/%d')})"
                      if b["chases"] else "**never chased**")
            lines_md.append(
                f"- **{b['bid_id']}** {b['project'][:46]} — {fmt_dollars(b['amount'])}  "
                f"({sub_age}d open, {ch_str})"
            )
            if b["reply"]:
                lines_md.append(f"    💬 *Reply {rdt.strftime('%m/%d')}*: "
                                f"{one_liner[:160] or '(empty)'} — `{cat}`")
            lines_md.append(f"    👉 *Next:* {action_text}")

    # Today's plan
    lines_md.append("\n## 🎯 Today's action plan")
    chase_today = [t for t in sorted(next_today, key=lambda x: -x[0])
                   if next_action(t[2])[0] in ("CHASE_DUE","NO_CHASE_YET")]
    close_today = [t for t in sorted(next_today, key=lambda x: -x[0])
                   if next_action(t[2])[0] == "CLOSE_OUT"]
    if chase_today:
        lines_md.append(f"\n**Chase ({len(chase_today)} recipients):**")
        for amt, em, it in chase_today[:15]:
            lines_md.append(f"  • {em}  →  {it['project'][:40]}  "
                            f"({fmt_dollars(amt)}, {(today - it['submitted']).days if it['submitted'] else '?'}d)")
    if close_today:
        lines_md.append(f"\n**Close-out ({len(close_today)} recipients):**")
        for amt, em, it in close_today[:8]:
            lines_md.append(f"  • {em}  →  {it['project'][:40]}  "
                            f"({fmt_dollars(amt)}, ignored {len(it['chases'])}× chases)")
    if not chase_today and not close_today:
        lines_md.append("\n_No immediate chase actions needed today._")

    # ───────────────────────── Persist + emit ─────────────────────────
    out_text = "\n".join(lines_md)
    print(out_text)

    # Save audit record
    audit_file = ROOT / "data" / "memory" / f"followup_intel_{today.isoformat()}.json"
    audit_file.write_text(json.dumps({
        "date": today.isoformat(),
        "total_active": total_active,
        "has_reply": has_reply,
        "no_chase": no_chase,
        "chase_due": chase_due,
        "close_out": close_out,
        "redirect": redirect,
        "by_recipient": {
            em: [{"bid_id": b["bid_id"], "internal_id": b["internal_id"],
                  "project": b["project"], "amount": b["amount"],
                  "chases": len(b["chases"]),
                  "next_action": next_action(b)[0],
                  "intel": next_action(b)[2]}
                 for b in items]
            for em, items in intel_by_recip.items()
        },
    }, indent=2), encoding="utf-8")

    # Telegram (Markdown compressed)
    if not args.no_telegram:
        try:
            import urllib.request, urllib.parse
            tok = os.environ.get("TELEGRAM_BOT_TOKEN",
                                  "")
            chat = os.environ.get("USER_TELEGRAM_CHAT_ID", "")
            tg_lines = [
                f"📊 *Follow-Up Brief — {today.strftime('%b %d')}*",
                f"Active: {total_active}  ·  Reply on file: {has_reply}",
                f"Chase due: {chase_due}  ·  Close-out: {close_out}",
            ]
            if chase_today:
                tg_lines.append(f"\n*Chase today (top 8):*")
                for amt, em, it in chase_today[:8]:
                    tg_lines.append(f"  • {it['project'][:35]} — {fmt_dollars(amt)}  ({em[:30]})")
            if close_today:
                tg_lines.append(f"\n*Close-out today:*")
                for amt, em, it in close_today[:5]:
                    tg_lines.append(f"  • {it['project'][:35]} — {fmt_dollars(amt)}")
            body = "\n".join(tg_lines)
            data = urllib.parse.urlencode({
                "chat_id": chat, "text": body, "parse_mode": "Markdown"
            }).encode("utf-8")
            urllib.request.urlopen(urllib.request.Request(
                f"https://api.telegram.org/bot{tok}/sendMessage", data=data), timeout=10)
        except Exception as e:
            print(f"  [telegram err] {e}", file=sys.stderr)

    # Email (optional)
    if args.email:
        try:
            cmd = [sys.executable, str(ROOT / "scripts" / "send_email.py"),
                   "--to", "cs@carolinacommercialfinishes.com",
                   "--subject", f"Carol Follow-Up Brief — {today.strftime('%b %d, %Y')}",
                   "--body", out_text,
                   "--no-signature"]
            subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
