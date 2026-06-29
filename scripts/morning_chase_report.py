#!/usr/bin/env python3
r"""
morning_chase_report.py — Daily 7AM chase recap + today's proposal.

Two-part brief delivered to Telegram every morning:

  PART A: YESTERDAY'S OUTCOMES
    • How many recipients we chased
    • Who replied (with reply classification)
    • Who ignored
    • What the reply said (one-liner)

  PART B: TODAY'S PROPOSED CHASE PLAN
    • Anyone we should chase today (cadence + reply rules)
    • Each entry: recipient, project, $, reason
    • Saved to data/proposed_chases_YYYY-MM-DD.json for execution
    • NO emails sent — user must approve via Telegram first

Cadence rules (per user 5/25):
  - 24h since last chase = eligible for next chase
  - 1-week wait after a STILL_PENDING / UNCLEAR reply
  - Never chase after definitive LOST / WON reply
  - PRICING_PUSHBACK requires custom response, not generic chase

Run:
  python scripts/morning_chase_report.py             # today's report, send Telegram
  python scripts/morning_chase_report.py --no-telegram
  python scripts/morning_chase_report.py --date 2026-05-26  # specific morning
"""
from __future__ import annotations
import argparse, imaplib, email as email_lib, json, os, re, sys
from collections import defaultdict
from datetime import datetime, date, timedelta
from email.header import decode_header
from email.utils import parsedate_to_datetime
from pathlib import Path

try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    pass

BS_FILE = ROOT / "data" / "memory" / "bid_status.json"
PLAN_DIR = ROOT / "data" / "proposed_chases"
LOG_FILE = ROOT / "data" / "logs" / "morning_chase_report.log"

ACTIVE_STATUSES = ("Bid Submitted", "Awaiting Decision")
CHASE_TRIGGERS = {"chase","chase_silent","chase_today","chase_silent_followups",
                  "followup","chase_backfill"}

# Projects the user chases personally — never auto-chase these.
# Match by Internal ID prefix OR a substring of the project name (case-insensitive).
# 6/3 — Midtown East REMOVED: user sent the repricing himself and handed
# chasing back to Carol ("now your job to chase them"). No longer user-handled.
USER_HANDLED_IIDS = set()
USER_HANDLED_NAME_SUBSTR = ()

# WEEKLY cadence (not daily) — bids the user wants chased only once every 7
# days instead of every day (e.g. a contact who's gone silent for a long time
# — daily nudges look desperate). Keyed by Internal ID prefix (8 chars).
WEEKLY_CADENCE_IIDS = {"d2c35dec"}  # a long-silent GC contact (user 6/2)


def decode_h(s: str) -> str:
    out = ""
    for part, enc in decode_header(s or ""):
        if isinstance(part, bytes):
            try: out += part.decode(enc or "utf-8", errors="replace")
            except Exception: out += part.decode("utf-8", errors="replace")
        else:
            out += part
    return out


def log(msg: str, quiet: bool = False):
    if not quiet:
        print(msg)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now().isoformat(timespec='seconds')}  {msg}\n")


ASK_AT_US = re.compile(
    r"can you (?:give|send|provide|price|quote|do|confirm)|give (?:me|us) an? add"
    r"|need (?:a |an )?(?:price|number|proposal|breakdown)|will that reduce"
    r"|please (?:confirm|send|provide|advise)|what (?:is|would) (?:your|the)",
    re.I)

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday",
             "saturday", "sunday"]
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"])}

# 6/16 FIX (Midtown/LFJ): only read the GC's NEW words, never the quoted thread
# below their reply. parse_comeback was scraping a stale "June 15" out of the
# quoted history and inventing a comeback date the GC never gave, which then
# forced a resume-chase the day it passed.
_QUOTE_SPLIT = re.compile(
    r"\n\s*On .{0,120}?wrote:|\r?\n>|\r?\n_{5,}|\r?\nFrom:\s|\r?\n-{3,}\s*Original",
    re.IGNORECASE)
def _new_text(body: str) -> str:
    """Strip quoted reply history — return only the sender's fresh message."""
    return _QUOTE_SPLIT.split(body or "", maxsplit=1)[0]

# 6/16 FIX (Midtown/LFJ): a reply where the GC commits to contacting US ("we'll
# keep you posted / on standby / waiting on the owner / we'll reach out") is a
# HARD ball-in-their-court hold — NEVER auto-chase it on a timer. One GC contact
# said this 3x and we chased him anyway; he replied "on standby until decided."
WILL_INITIATE = re.compile(
    r"keep you posted|keep you (in the loop|updated|informed)|we'?ll reach (out|back)"
    r"|reach back out|we'?ll let you know|be in touch|on standby|stand ?by until"
    r"|waiting on (the )?(owner|decision|gc|client|award)|once we (hear|know|have)"
    r"|as soon as we (hear|have|know)|when we (hear|know)|update you (once|as soon)",
    re.IGNORECASE)


def parse_comeback(body: str, reply_at) -> tuple:
    """If the GC's reply names WHEN news will exist ('we meet the owner
    Thursday', 'check back next week', 'CDs come out mid-June'), return
    (resume_date, label). Meeting/decision events resume the DAY AFTER.
    Returns (None, '') when no date language is found."""
    t = _new_text(body)[:1500].lower()   # 6/16 FIX: GC's new words only, not quoted thread
    rd = reply_at.date() if hasattr(reply_at, "date") else reply_at
    is_event = bool(re.search(r"meet|meeting|call|decision|review|award", t))

    # 6/16 god-level: a bare m/d only counts as a comeback date if a comeback-
    # INTENT word sits within ~50 chars — otherwise an incidental number (a bid #
    # like "6/12 revision", a price, an address, a phone fragment) fabricates a
    # resume date the GC never gave and forces a chase (audit MED + Midtown class).
    _INTENT = ("back", "update", "know", "hear", "meet", "decision", "award",
               "review", "follow", "circle", "check", "expect", "result",
               "announce", "pending", "soon", "week", "reach", "touch", "let you")
    m = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", t)
    if m and not any(w in t[max(0, m.start() - 50):m.end() + 50] for w in _INTENT):
        m = None
    if m:
        try:
            yr = rd.year
            d = date(yr, int(m.group(1)), int(m.group(2)))
            if d < rd:
                d = date(yr + 1, int(m.group(1)), int(m.group(2)))
            if (d - rd).days <= 180:
                return d + timedelta(days=1 if is_event else 0), f"{m.group(0)}"
        except ValueError:
            pass
    for mon, mi in _MONTHS.items():
        mm = re.search(rf"\b(mid|end of|early)?[\s-]*{mon}\b(?:\s+(\d{{1,2}}))?", t)
        if mm and (mm.group(1) or mm.group(2)):
            day = int(mm.group(2)) if mm.group(2) else (
                15 if mm.group(1) == "mid" else 28 if mm.group(1) == "end of" else 5)
            try:
                d = date(rd.year if mi >= rd.month else rd.year + 1, mi, min(day, 28))
                if 0 <= (d - rd).days <= 240:
                    return d + timedelta(days=1 if is_event else 0), mm.group(0)
            except ValueError:
                pass
    if "tomorrow" in t:
        return rd + timedelta(days=1 + (1 if is_event else 0)), "tomorrow"
    for i, wd in enumerate(_WEEKDAYS[:5]):
        if re.search(rf"\b{wd}\b", t):
            ahead = (i - rd.weekday()) % 7 or 7
            return rd + timedelta(days=ahead + (1 if is_event else 0)), wd
    if re.search(r"next week", t):
        return rd + timedelta(days=7), "next week"
    mw = re.search(r"\b(a|one|two|three|few|couple(?:\s+of)?)\s+(?:more\s+)?weeks?\b", t)
    if mw:
        n = {"a": 1, "one": 1, "two": 2, "three": 3,
             "few": 3, "couple": 2, "couple of": 2}.get(mw.group(1), 2)
        return rd + timedelta(days=7 * n), f"{mw.group(1)} weeks"
    if re.search(r"second half of (\d{4})|pushed to (\d{4})", t):
        return rd + timedelta(days=120), "long deferral"
    return None, ""


def classify_reply(subj: str, body: str) -> tuple[str, str]:
    """Light classifier — match keywords. Returns (category, one_liner).

    6/17 FIX: classify ONLY the sender's NEW words (quote-stripped), never the
    quoted thread below. The Old Navy false-WON came from matching the phrase
    "congratulations again on the award" that WE wrote to Weekes (about THEIR GC
    prime award) sitting in the quoted history — classify_reply read the full
    body and called it our win, then wrote a sticky TERMINAL WON. Same defect
    would mis-fire LOST/REVISE off quoted text. Strip quotes first.
    """
    text = f"{subj} {_new_text(body)}".lower()
    # DO-NOT-CONTACT — strongest signal, checked FIRST (6/16 god-level upgrade).
    # A GC asking us to stop is a PERMANENT hard stop, never a soft 5-day-resume
    # reply. Previously these fell through to UNCLEAR and got re-chased on a timer.
    if re.search(r"stop\s+emailing|stop\s+(?:contacting|reaching|sending|following)|"
                 r"do\s*n[o']?t\s+(?:contact|email|reach)|"
                 r"remove\s+(?:me|us|my|our)\b[^.]*\b(?:from|list|email)|"
                 r"take\s+(?:me|us)\s+off|unsubscribe|quit\s+(?:emailing|contacting)|"
                 r"no\s+longer\s+(?:wish|want|interested|bidding|need)", text):
        return "DO_NOT_CONTACT", "GC asked us to STOP — permanent do-not-contact"
    # LOST must be subject-bound to US (we/you/your bid) — generic "PM has not
    # awarded any contracts yet" is PENDING, not lost (5/27 false-positive;
    # patterns ported from process_followup_replies.classify_with_regex_fallback).
    if re.search(
            r"\b(?:we\s+(?:were|are|got)\s+not\s+(?:awarded|selected|chosen)|"
            r"you\s+(?:were|are|got)\s+not\s+(?:awarded|selected|chosen)|"
            r"(?:your|our)\s+bid\s+(?:was|is)\s+not\s+(?:awarded|selected)|"
            r"weren'?t\s+awarded|"
            r"was\s+not\s+awarded\s+(?:this|that|the)|"
            r"were\s+not\s+awarded\s+(?:this|that|the|these)|"
            r"didn'?t\s+get\s+(?:this|that|the|these|them)|"
            r"did\s+not\s+get\s+(?:this|that|the|these|them)|"
            r"lost\s+(?:this|that|the)\s+(?:project|bid)|"
            r"went\s+with\s+(?:another|other|different)|"
            r"awarded\s+to\s+(?:another|someone\s+else|a\s+different)|"
            r"you\s+(?:were|are|came\s+in)\s+(?:high|too\s+high|over)|"
            r"your\s+(?:price|number|bid)\s+(?:was|is)\s+(?:high|too\s+high|over))",
            text):
        return "LOST", "Confirmed not awarded"
    if re.search(r"awarded\s+to\s+you|congratulat|you\s+(?:got|won)\s+(?:the|our|this)|"
                 r"(?:send|issue|cut|get)\s+(?:you\s+)?(?:a\s+|the\s+)?(?:c\.?o\.?|change\s+order|"
                 r"contract|subcontract|award|loi|letter\s+of\s+intent)\b|"
                 r"(?:send|get)\s+(?:you\s+)?(?:a\s+|the\s+)?(?:co|contract)\s+(?:for|on)\s+this", text):
        return "WON", "Awarded to us (contract/CO incoming)"
    if re.search(r"awarded\s+to\s+(?:the\s+)?gc|(?:we|gc)\s+(?:got|won)\s+(?:the\s+)?(?:prime|project)|"
                 r"contact\s+\w+|reach\s+out\s+to\s+\w+", text):
        return "REDIRECT", "GC won prime — switch to PM"
    if re.search(r"thank\s+you\s+for\s+the\s+breakdown|can\s+you\s+(?:provide|share|break)|"
                 r"clarif|question", text):
        return "ENGAGED", "Engaged — pricing/scope question"
    if re.search(r"re-?price|re-?bid|reduced\s+scope|revised?\s+scope|re-?solicit"
                 r"|value\s+engineer|provide\s+(?:a\s+)?revised|send\s+(?:us\s+)?(?:a\s+)?revised", text):
        return "REVISE_REQUESTED", "GC asked us to RE-PRICE — owe revised proposal"
    if re.search(r"too\s+high|sharpen|revise.*pric|come\s+down", text):
        return "PRICING_PUSHBACK", "Pricing pushback"
    if re.search(r"still\s+pending|no\s+(?:news|update)\s+yet|under\s+review|"
                 r"owner.*hands|owner\s+review|still\s+working|"
                 r"hand(?:ing|ed)?\s+(?:this\s+)?(?:it\s+)?(?:off|over)|"
                 r"(?:project\s+management|pm)\s+team|will\s+(?:reach|get|be)\s+(?:back\s+)?"
                 r"(?:out|in\s+touch|to\s+you)|reach\s+back\s+out|be\s+in\s+touch|"
                 r"any\s+day\s+now|award\s+(?:is\s+)?(?:any\s+day|coming|soon|expected)|"
                 r"working\s+on\s+(?:their|the)\s+decision|in\s+the\s+process", text):
        return "STILL_PENDING", "Pending — GC handling internally, said they'll follow up"
    if re.search(r"out\s+of\s+office|on\s+vacation|on\s+leave|auto.?reply", text):
        return "OUT_OF_OFFICE", "Auto-reply"
    return "UNCLEAR", "Unclear — manual review"


def reply_gist(body: str, n: int = 150) -> str:
    """Pull the first meaningful sentence(s) of a reply so the report can show WHAT
    the GC actually said — not just a bucket label like 'UNCLEAR'. Strips greetings,
    quoted history, and signatures."""
    if not body:
        return ""
    # cut quoted history / signatures
    b = re.split(r"On .+wrote:|From:\s|_{4,}|-{4,}\s*Original|Sent from my|"
                 r"\bTEL:|\bPhone:|\bCell:", body)[0]
    lines = [l.strip() for l in b.splitlines() if l.strip()]
    # drop a leading greeting ("Nursultan," / "Hi Carol," / "Good morning,")
    while lines and (re.match(r"^(hi|hello|hey|dear|good\s+(morning|afternoon|evening))\b", lines[0], re.I)
                     or re.match(r"^[A-Za-z][A-Za-z.'-]{1,20},?$", lines[0])):
        lines.pop(0)
    text = re.sub(r"\s+", " ", " ".join(lines)).strip()
    return (text[: n - 1] + "…") if len(text) > n else text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None,
                    help="Report morning of this date (YYYY-MM-DD). "
                         "Default: today. The report SUMMARIZES the PREVIOUS day.")
    # 6/1 — push is now OPT-IN. Default: print only, do NOT send to Telegram.
    # ONLY the daemon's 7 AM run passes --telegram. This prevents the
    # double/triple-send the user saw: when Carol runs the report ON DEMAND
    # to answer "give me a brief", the script used to auto-push AND Carol
    # relayed the same text → user got it twice. Now on-demand runs print
    # only (Carol relays once); accidental/test runs never spam Telegram.
    ap.add_argument("--telegram", action="store_true",
                    help="Actually send the brief to Telegram. Daemon-only; "
                         "on-demand runs should omit this so Carol relays once.")
    ap.add_argument("--no-telegram", action="store_true",
                    help="(legacy no-op; push is opt-in via --telegram now)")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--intel", action="store_true",
                    help="Add a per-bid Follow-Up Intelligence section: for "
                         "every active bid, who replied (date+category) or is "
                         "silent, last chased, and current standing. Grounded "
                         "in real Gmail/CRM data — never invented.")
    args = ap.parse_args()

    today = (datetime.strptime(args.date, "%Y-%m-%d").date()
             if args.date else date.today())
    yesterday = today - timedelta(days=1)

    # ── PART A — yesterday's chases & their outcomes ────────────────────────
    if not BS_FILE.exists():
        log(f"[report] {BS_FILE} not found", args.quiet)
        return 1
    bs = json.loads(BS_FILE.read_text(encoding="utf-8"))

    # Dedupe yesterday's chases by (recipient, normalized subject)
    raw_y = [h for h in bs.get("history", [])
             if h.get("trigger") in CHASE_TRIGGERS
             or (h.get("subject", "").lower().startswith(("follow-up", "follow up", "following up")))]
    seen = set()
    yest_chases = []
    for h in raw_y:
        try: dt = datetime.fromisoformat(h.get("at", ""))
        except Exception: continue
        if dt.date() != yesterday: continue
        recip = (h.get("to_email") or "").strip().lower()
        if not recip: continue
        subj = h.get("subject", "")
        # Skip "Re:" inbound replies that were mislabeled
        if subj.lower().startswith(("re:", "re ", "fw:", "fwd:")): continue
        key = (recip, re.sub(r"\s+", " ", subj).strip().lower()[:80])
        if key in seen: continue
        seen.add(key)
        yest_chases.append({"recip": recip, "subj": subj, "at": dt,
                            "bid_id": h.get("bid_id", "")})

    log(f"[report] yesterday ({yesterday}) had {len(yest_chases)} unique chase send(s)",
        args.quiet)

    # Pull CRM (project + status lookup)
    try:
        from crm_lib import get_sheet
        ws = get_sheet("Bid Log")
        crm_rows = ws.get_all_records()
    except Exception as e:
        log(f"[report] CRM fetch failed: {e}", args.quiet)
        crm_rows = []

    # For each yesterday's chase, check Gmail for replies since.
    # 5/30 fix — search ALL MAIL, not INBOX. A GC's 5/29 win reply
    # ("I will send you a CO for this scope") was auto-labeled out of the
    # INBOX, so the recap reported "Replied: 0" and pushed that FALSE line
    # to the user's Telegram. All Mail sees every message regardless of
    # label/archive state. User: "are they true?" — this is why one wasn't.
    user = os.environ.get("GMAIL_USER", "estimates@carolinacommercialfinishes.com")
    pw   = os.environ.get("GMAIL_APP_PASSWORD", "")
    M = imaplib.IMAP4_SSL("imap.gmail.com")
    M.login(user, pw)
    M.select('"[Gmail]/All Mail"', readonly=True)
    since = yesterday.strftime("%d-%b-%Y")

    # 5/28 fix — was filtering on FROM == exact recip, missing replies from
    # other humans at the same GC (e.g. chased the GC's bids@ alias, but a PM
    # replied from his personal mailbox at the same domain → counted as NO_REPLY
    # for 3 weeks while he actively answered). Switched to DOMAIN-level FROM
    # filter, then disambiguate per-chase using the [ID:xxxxxxxx] Internal
    # ID tag we embed in every outbound subject. Domain-level results are
    # also kept in replies_by_recip for the per-recipient code path so the
    # downstream consumer doesn't need to change shape.
    replies_by_recip: dict[str, list] = defaultdict(list)
    replies_by_domain: dict[str, list] = defaultdict(list)
    chase_domains = {c["recip"].split("@", 1)[1] for c in yest_chases
                     if "@" in c["recip"]}
    for dom in chase_domains:
        try:
            st, ids = M.search(None, f'(FROM "@{dom}" SINCE "{since}")')
            mids = ids[0].split() if ids and ids[0] else []
            for mid in mids[-15:]:  # raised cap; one GC can have many PMs reply
                st, data = M.fetch(mid, "(BODY.PEEK[])")
                if st != "OK" or not data or not data[0]: continue
                msg = email_lib.message_from_bytes(data[0][1])
                try: rdt = parsedate_to_datetime(msg.get("Date", ""))
                except Exception: continue
                if not rdt: continue
                subj = decode_h(msg.get("Subject", ""))
                sender_raw = decode_h(msg.get("From", ""))
                m = re.search(r"<([^>]+)>", sender_raw)
                sender_addr = (m.group(1) if m else sender_raw).strip().lower()
                # Extract text body
                body = ""
                if msg.is_multipart():
                    for p in msg.walk():
                        if p.get_content_type() == "text/plain":
                            try: body = p.get_payload(decode=True).decode(
                                p.get_content_charset() or "utf-8", errors="replace")
                            except Exception: pass
                            break
                else:
                    try: body = msg.get_payload(decode=True).decode(
                        msg.get_content_charset() or "utf-8", errors="replace")
                    except Exception: pass
                rec = {"at": rdt.replace(tzinfo=None), "subj": subj,
                       "body": body, "from": sender_addr}
                replies_by_domain[dom].append(rec)
                # Mirror to per-recipient bucket if From matches exactly,
                # preserves legacy behavior for the exact-match path.
                if sender_addr:
                    replies_by_recip[sender_addr].append(rec)
        except Exception:
            continue
    M.logout()

    # Match an [ID:xxxxxxxx] tag from the chase subject so we can pin a
    # reply to the correct project even when one GC has many open bids.
    def _id_tag(subj: str) -> str:
        m = re.search(r"\[ID:([0-9a-f]{6,8})\]", subj or "", re.I)
        return m.group(1).lower() if m else ""

    _STOP = {"follow", "status", "check", "update", "quick", "final", "last",
             "email", "before", "closing", "out", "project", "phase", "store",
             "building", "center", "campus"}

    # Build outcome per chase
    outcomes = []
    for c in yest_chases:
        dom = c["recip"].split("@", 1)[1] if "@" in c["recip"] else ""
        cand = list(replies_by_domain.get(dom, []))
        # Decide, per candidate reply, whether it is about THIS bid.
        # The [ID:xxxxxxxx] tag is AUTHORITATIVE: if our chase subject is
        # tagged and a reply carries a DIFFERENT tag (or no matching tag),
        # it is NOT this bid. 5/30 bug: previously, when our chase was
        # tagged but no reply matched, cand silently stayed = the whole
        # domain, so one PM's reply bled onto two sibling projects at the same
        # GC (all one domain) and the recap reported 3 replies instead of 1.
        tag = _id_tag(c["subj"])
        words = [w.lower() for w in re.findall(r"[A-Za-z]{4,}", c["subj"])
                 if w.lower() not in _STOP]

        def _about(r, _tag=tag, _words=words):
            rtag = _id_tag(r.get("subj", ""))
            if _tag and rtag:
                return rtag == _tag            # both tagged → must match exactly
            # one side untagged → require ≥2 project-keyword overlaps
            if not _words:
                return False
            hits = sum(1 for w in _words[:6] if w in (r.get("subj", "") or "").lower())
            return hits >= 2

        cand = [r for r in cand if _about(r)]
        recs = [r for r in cand
                if r["at"] >= c["at"].replace(tzinfo=None)]
        recs.sort(key=lambda r: r["at"], reverse=True)
        if recs:
            cat, summary = classify_reply(recs[0]["subj"], recs[0]["body"])
            excerpt = (recs[0]["body"] or "").strip().replace("\n", " ")[:120]
            outcomes.append({**c, "replied": True, "category": cat,
                             "summary": summary, "excerpt": excerpt,
                             "reply_at": recs[0]["at"]})
        else:
            outcomes.append({**c, "replied": False, "category": "NO_REPLY",
                             "summary": "No reply yet"})

    # Summary counts
    n = len(outcomes)
    replied = sum(1 for o in outcomes if o["replied"])
    ignored = n - replied
    by_cat = defaultdict(int)
    for o in outcomes:
        by_cat[o["category"]] += 1

    # ── Build Markdown brief ────────────────────────────────────────────────
    lines = [
        f"📨 *Chase Recap — {yesterday.strftime('%a %b %d, %Y')}*",
        "",
        f"Sent: *{n}* chase(s) · Replied: *{replied}* · Ignored: *{ignored}*",
        "",
        "*Outcomes:*",
    ]
    for cat, count in sorted(by_cat.items(), key=lambda x: -x[1]):
        emoji = {"LOST": "❌", "WON": "🎉", "REDIRECT": "↪️", "ENGAGED": "💬",
                 "STILL_PENDING": "⏳", "PRICING_PUSHBACK": "💰",
                 "OUT_OF_OFFICE": "🌴", "UNCLEAR": "❓", "NO_REPLY": "🔇"}.get(cat, "·")
        lines.append(f"  {emoji} {cat}: {count}")

    # Per-recipient detail
    if outcomes:
        lines.append("")
        lines.append("*Details:*")
        for o in sorted(outcomes, key=lambda x: (not x["replied"], x["recip"]))[:25]:
            mark = "✅" if o["replied"] else "  "
            recip = o["recip"][:32]
            cat = o["category"][:14]
            lines.append(f"{mark} `{cat:<14}` {recip:<33} — {o['summary'][:50]}")

    # ── PART B — today's proposed chase plan ─────────────────────────────────
    # Build a quick CRM lookup
    crm_by_iid = {}
    for r in crm_rows:
        iid = (r.get("Internal ID") or "").strip()
        if iid:
            crm_by_iid[iid] = r

    # For each ACTIVE bid in CRM, decide if it needs a chase today
    history = bs.get("history", [])
    # Get latest chase per (recipient, project_core) from any time
    last_chase_by_recip: dict[str, datetime] = {}
    for h in history:
        recip = (h.get("to_email") or "").strip().lower()
        if not recip: continue
        subj = h.get("subject", "").lower()
        if not (subj.startswith(("follow-up", "follow up", "following up"))
                or h.get("trigger") in CHASE_TRIGGERS):
            continue
        try: dt = datetime.fromisoformat(h.get("at", ""))
        except Exception: continue
        if recip not in last_chase_by_recip or dt > last_chase_by_recip[recip]:
            last_chase_by_recip[recip] = dt

    # Get latest reply per recipient (any time). 5/28 — same fix as Part A:
    # also accept replies from any human at the GC's domain, not just the
    # exact primary contact address. We track latest per-address AND
    # latest-per-domain; the eligibility loop below picks whichever is more
    # recent so a reply from a sibling PM still triggers the soft-reply
    # buffer instead of letting us blast the same GC again.
    inbox_replies_seen: dict[str, dict] = {}
    inbox_replies_by_domain: dict[str, list] = {}  # domain -> [all recent inbound recs]
    M = imaplib.IMAP4_SSL("imap.gmail.com")
    M.login(user, pw)
    M.select('"[Gmail]/All Mail"', readonly=True)  # replies get auto-labeled out of INBOX
    seven_days_ago = (today - timedelta(days=14)).strftime("%d-%b-%Y")
    # Build list of all active CRM contact addresses
    all_addrs = set()
    for r in crm_rows:
        if r.get("Status") not in ACTIVE_STATUSES: continue
        for a in re.split(r"[,;\s]+", r.get("Contact Email", "") or ""):
            if "@" in a: all_addrs.add(a.strip().lower())
        for a in re.split(r"[,;\s]+", r.get("CC Contacts", "") or ""):
            if "@" in a: all_addrs.add(a.strip().lower())
    all_domains = {a.split("@", 1)[1] for a in all_addrs if "@" in a}
    for dom in all_domains:
        try:
            st, ids = M.search(None, f'(FROM "@{dom}" SINCE "{seven_days_ago}")')
            mids = ids[0].split() if ids and ids[0] else []
            if not mids: continue
            # walk recent → older, record latest per exact-sender and per-domain
            dom_list = []
            for mid in mids[-80:][::-1]:   # chatty GC domains (WC's ITB blasts) need a deep window
                st, data = M.fetch(mid, "(BODY.PEEK[HEADER.FIELDS (DATE SUBJECT FROM)])")
                if st != "OK" or not data or not data[0]: continue
                msg = email_lib.message_from_bytes(data[0][1])
                try: rdt = parsedate_to_datetime(msg.get("Date", ""))
                except Exception: continue
                if not rdt: continue
                sender_raw = decode_h(msg.get("From", ""))
                m = re.search(r"<([^>]+)>", sender_raw)
                sender_addr = (m.group(1) if m else sender_raw).strip().lower()
                subj = decode_h(msg.get("Subject", ""))
                rec = {"at": rdt.replace(tzinfo=None), "subj": subj,
                       "from": sender_addr, "body": ""}
                # For reply-like messages only (Re:/[ID:]/follow-up), pull a body
                # snippet so classify_reply can see "re-price / reduced scope" etc.
                # (the request lives in the BODY, not the subject). Skip automated
                # ITB/reminder blasts — they aren't replies and bloat the scan.
                sl = subj.lower()
                if "[id:" in sl or sl.startswith(("re:", "re ", "fw:", "fwd:")):
                    st2, d2 = M.fetch(mid, "(BODY.PEEK[])")
                    if st2 == "OK" and d2 and d2[0]:
                        try:
                            full = email_lib.message_from_bytes(d2[0][1])
                            btxt = ""
                            parts = full.walk() if full.is_multipart() else [full]
                            for part in parts:
                                if part.get_content_type() == "text/plain":
                                    btxt += (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
                            if not btxt:  # HTML-only — strip tags
                                for part in (full.walk() if full.is_multipart() else [full]):
                                    if part.get_content_type() == "text/html":
                                        h = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
                                        btxt += re.sub(r"<[^>]+>", " ", h)
                            rec["body"] = btxt[:2000]
                        except Exception: pass
                # Exact-sender track (latest per address)
                prev = inbox_replies_seen.get(sender_addr)
                if not prev or rec["at"] > prev["at"]:
                    inbox_replies_seen[sender_addr] = rec
                # Domain track — keep ALL recent inbound, not just the latest. A GC
                # domain (e.g. wcconstructionco.com) blasts automated ITBs that would
                # otherwise clobber the one real reply about a specific bid; the
                # eligibility loop filters this list down to the reply about THIS bid.
                dom_list.append(rec)
            if dom_list:
                inbox_replies_by_domain[dom] = dom_list
        except Exception:
            continue
    M.logout()

    # Decide chase eligibility per active bid
    proposed = []
    skipped = []   # 6/1 — record WHY each active bid was excluded, so the
                   # brief can show the real skip list. Without this the script
                   # output only the eligible bids, and Carol confabulated a
                   # fake skip breakdown (invented "Dutch Bros / SCSU / Cigar"
                   # bids that don't exist). Ground truth beats a guess.
    def _skip(r, reason):
        skipped.append({
            "proj": r.get("Project Name", ""),
            "bid_id": r.get("Bid #", ""),
            "reason": reason,
        })
    intel = []   # 6/1 — per-bid follow-up intelligence (who replied/silent),
                 # grounded in real chase + reply data. Answers the user's
                 # "who answered? who ignored?" without inventing anything.
    def _intel(r, last_chase, reply, standing):
        rcat = ""
        if reply:
            rcat, _ = classify_reply(reply.get("subj", ""), "")
        intel.append({
            "bid_id": r.get("Bid #", ""),
            "proj": r.get("Project Name", ""),
            "gc": r.get("GC / Client", ""),
            "last_chase": last_chase.strftime("%m/%d") if last_chase else None,
            "reply_date": reply["at"].strftime("%m/%d") if reply and hasattr(reply["at"], "strftime") else None,
            "reply_cat": rcat,
            "standing": standing,
        })
    today_dt = datetime.combine(today, datetime.min.time())
    # ── RELATIONSHIP-TIERED CADENCE + LIFETIME CAP (user 6/16 god-level rebuild).
    # Data: more chasing correlates with WORSE outcomes (lost bids avg 7.59 chases).
    # Relationship GCs get a gentle ~weekly cadence + low cap; competitive get
    # every-few-days + a higher cap; a bid that's been chased to its cap with ZERO
    # replies goes DORMANT (no more auto-chase — flagged for an owner/manual call).
    def _loadj(p, default):
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return default
    _tcfg = _loadj(ROOT / "data" / "config" / "gc_tiers.json", {}) or {}
    TIERS = {
        "rel_dom": set(_tcfg.get("relationship_domains") or []),
        "rel_gap": _tcfg.get("relationship_min_gap_days", 7),
        "rel_cap": _tcfg.get("relationship_lifetime_cap", 4),
        "cmp_gap": _tcfg.get("competitive_min_gap_days", 3),
        "cmp_cap": _tcfg.get("competitive_lifetime_cap", 6),
    }
    COUNTS = _loadj(ROOT / "data" / "memory" / "chase_lifetime_counts.json", {}) or {}
    # User-curated DROP list (6/17): bids he explicitly told us to stop chasing
    # (low value / not pursuing). Not lost/won — still live, just no auto-chase.
    NO_CHASE = (_loadj(ROOT / "data" / "config" / "no_chase.json", {}) or {}).get("dropped", {})
    # CHASE-UNTIL-REPLY override (6/17): bids the user said to "fuck / chase until
    # they respond." Bypass the DORMANT cap AND the cadence gap so they stay eligible
    # every business day until the GC replies. Reply-awareness still stops it the
    # moment they answer; the daily approval gate still governs what actually sends.
    FORCE_CHASE = {k.lower() for k in
                   (_loadj(ROOT / "data" / "config" / "chase_until_reply.json", {}) or {}).get("force", {})}
    try:
        from _lib.terminal_states import is_terminal as _terminal_is, mark_terminal as _terminal_mark
    except Exception:
        _terminal_is = lambda _i: None
        _terminal_mark = lambda *a, **k: False

    # ── 6/17 THREAD-COMPREHENSION GATE (the 350-Hein fix) ───────────────────
    # Before asserting "we owe a revised proposal" / "we owe an answer" off the
    # GC's reply TEXT, verify against our own SENT side (body + attachments +
    # aware-timestamp ordering) that we didn't ALREADY deliver/answer after the
    # ask. 350 Hein: a stale "please complete & return the scope sheet" kept a
    # we-owe loop alive after we'd already returned the COMPLETED sheets.
    _ts_cache = {}
    def _thread_state(iid_tag, proj_words, proj_nums):
        try:
            from _lib import thread_reader as _tr
        except Exception:
            return None
        key = iid_tag or " ".join(proj_words[:4])
        if key not in _ts_cache:
            q = " ".join(list(proj_words[:4]) + sorted(proj_nums))
            try:
                _ts_cache[key] = _tr.fetch_thread(q) if q.strip() else []
            except Exception:
                _ts_cache[key] = None   # connection/auth failure → distrust, keep we-owe
        msgs = _ts_cache.get(key)
        if not msgs:
            return None
        try:
            return _tr.thread_state(msgs)
        except Exception:
            return None

    def _owe_resolved(ts):
        """True only if the fetched thread actually CONTAINS the GC's ask AND we
        provably satisfied it (so 'we owe' is closed). Fail-safe: False when the
        thread is missing/unreadable or the ask wasn't captured → keep the we-owe."""
        if not ts:
            return False
        seen = len(ts.get("open_items", [])) + len(ts.get("satisfied_items", []))
        return seen > 0 and not ts.get("open_items")

    for r in crm_rows:
        if r.get("Status") not in ACTIVE_STATUSES: continue
        bid_id = r.get("Bid #", "")
        iid = (r.get("Internal ID") or "").strip()
        if not iid: continue
        # STICKY TERMINAL STATE — if LOST/WON/DO_NOT_CONTACT was ever recorded for
        # this bid, NEVER auto-chase again, regardless of today's reply attribution.
        _term = _terminal_is(iid)
        if _term:
            _skip(r, f"TERMINAL {_term['state']} ({_term.get('date','')}) — sticky, never chase")
            continue
        # USER-DROPPED — explicitly told us to stop chasing (no_chase.json). Still
        # a live bid, just no auto-chase. Reversible by removing it from the list.
        if iid[:8].lower() in NO_CHASE:
            _skip(r, f"dropped from chase — your call ({NO_CHASE[iid[:8].lower()]})")
            continue
        proj = r.get("Project Name", "")
        # Skip user-handled projects (Midtown East — user chases LF Jennings himself)
        if iid[:8].lower() in USER_HANDLED_IIDS or any(s in proj.lower() for s in USER_HANDLED_NAME_SUBSTR):
            _skip(r, "user handles this GC directly")
            _intel(r, None, None, "user-handled")
            continue
        # PRE-DUE-DATE HOLD (user 2026-06-22): we often submit our sub-bid weeks
        # early. If the GC's own Bid Due Date is still in the FUTURE, the GC hasn't
        # bid the owner yet — there is no award/decision to report, so an
        # award-timeline chase is premature pressure that reads as not knowing the
        # schedule. Hold until the day AFTER the due date, then resume normal
        # cadence. Blank/unparseable due date = unknown -> fall through (do NOT
        # suppress), so the only behavior change is suppressing genuinely-early chases.
        _due_str = (r.get("Bid Due Date") or "").strip()
        _due_d = None
        for _fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%m-%d-%Y", "%-m/%-d/%Y"):
            try:
                _due_d = datetime.strptime(_due_str, _fmt).date(); break
            except Exception:
                pass
        if _due_d and _due_d >= today:
            _skip(r, f"pre-bid hold — bid due {_due_d:%m/%d} not yet passed; "
                     f"GC hasn't bid the owner, nothing to report "
                     f"(resume {(_due_d + timedelta(days=1)):%m/%d})")
            continue
        gc = r.get("GC / Client", "")
        amt_str = r.get("Bid Amount ($)", "")
        try: amt = int(re.sub(r"[^\d]", "", amt_str)) if amt_str else 0
        except: amt = 0
        sub_d_str = r.get("Bid Submitted Date", "")

        primary = (r.get("Contact Email") or "").strip().lower()
        if not primary or "@" not in primary:
            _skip(r, "no contact email on file")
            _intel(r, None, None, "no contact email")
            continue
        primary = primary.split()[0] if " " in primary else primary

        last = last_chase_by_recip.get(primary)
        # 5/28 — pick the most recent reply from either the exact primary
        # contact OR anyone at the same domain — BUT only if the reply was
        # about THIS specific bid (matching [ID:xxxxxxxx] tag or project
        # keyword in subject). Prevents "a PM replied about project A → we
        # stop chasing a different PM about project B" cross-project suppression.
        primary_dom = primary.split("@", 1)[1] if "@" in primary else ""
        rep_exact = inbox_replies_seen.get(primary)
        dom_replies = inbox_replies_by_domain.get(primary_dom, [])

        # Use the short Internal ID prefix as the safest project key
        iid_tag = (iid or "").lower()[:8]
        proj_words = [w.lower() for w in re.findall(r"[A-Za-z]{4,}", proj)
                      if w.lower() not in {"project", "phase", "store",
                                           "building", "center", "campus"}]
        # Store numbers are the ONLY way to tell sibling projects apart
        # ("Food Lion 2524 Aylett" vs "Food Lion 2663 Montpelier" share every
        # alpha token — a reply about one store was glued onto the other).
        proj_nums = set(re.findall(r"\d{3,5}", proj))

        def _reply_is_about_this_bid(rep):
            if not rep: return False
            subj = (rep.get("subj") or "").lower()
            if iid_tag and iid_tag in subj.replace("[id:", "").replace("]", " "):
                return True
            # A DIFFERENT [ID:] tag in the subject is an authoritative NO.
            m_other = re.search(r"\[id:([0-9a-f]{6,})\]", subj)
            if m_other and iid_tag and not m_other.group(1).startswith(iid_tag):
                return False
            # Non-overlapping store numbers on both sides = different project.
            subj_nums = set(re.findall(r"\d{3,5}", subj))
            if proj_nums and subj_nums and not (proj_nums & subj_nums):
                return False
            # require ≥2 project keywords to call it "this bid"
            hits = sum(1 for w in proj_words[:6] if w in subj)
            return hits >= 2

        cand = []
        if rep_exact and _reply_is_about_this_bid(rep_exact): cand.append(rep_exact)
        # Scan ALL recent inbound from the GC's domain; keep only those about THIS
        # bid (matched by [ID:] tag / project keywords). Fixes the bug where a
        # domain's newest automated ITB hid the real reply about this project
        # (6/5: a GC's county ITB blasts hid that GC's PM reply on another bid).
        cand += [rp for rp in dom_replies if _reply_is_about_this_bid(rp)]
        last_reply = max(cand, key=lambda r: r["at"]) if cand else None
        # STICKY: a re-price/revise request ANYWHERE in the recent thread means we
        # owe a revised proposal — it overrides a newer soft reply and never expires
        # into a chase. (a PM's 6/1 re-price request must win over a 6/3 "thanks".)
        revise_req = next((c for c in cand if classify_reply(
            c.get("subj", ""), c.get("body", ""))[0] == "REVISE_REQUESTED"), None)

        hours_since_chase = 999999 if not last else (today_dt - last).total_seconds() / 3600
        # 5/28 — user rule: "chase every active bid EVERY DAY until they
        # reply". A chase at 21:00 yesterday and a strict 24h gate means we
        # can't chase until 21:00 today — so a 9 AM morning report finds
        # almost everyone INSIDE the 24h window. Switch to calendar-day
        # cadence: if the last chase was on a previous calendar day, the
        # bid is eligible today. Strict 24h still applies to chases sent
        # earlier TODAY (don't double-chase within a single day).
        already_chased_today = bool(last and last.date() == today)
        # Use DATE-only arithmetic for the buffer count. (today_dt midnight)
        # minus (5/21 20:23) gives .days = 6 even though it's 7 calendar
        # days. The user means calendar days when he says "7-day buffer".
        days_since_reply = (999999 if not last_reply
                            else (today - last_reply["at"].date()).days)

        # Capture per-bid intelligence for every bid that reaches here (all
        # except the 2 early skips, which recorded their own). Standing is
        # filled in after the loop from proposed/skipped membership.
        _intel(r, last, last_reply, None)

        # OWE-REVISION takes priority over all cadence: if the GC asked us to
        # re-price, the ball is in our court — never chase, sticky until we send it.
        if revise_req:
            rd = revise_req["at"].strftime("%m/%d") if hasattr(revise_req["at"], "strftime") else "?"
            # 6/17 gate: did we ALREADY send the revised proposal after their ask?
            if _owe_resolved(_thread_state(iid_tag, proj_words, proj_nums)):
                log(f"[report] {bid_id}: re-price ask {rd} already answered (revised "
                    f"proposal sent) — clearing false we-owe", args.quiet)
                revise_req = None
            else:
                _skip(r, f"GC asked to RE-PRICE {rd} — we owe a revised proposal (not a chase)")
                continue

        # Cadence rules (user 6/5 — "if they replied, don't chase them"):
        #   - Definitive reply (LOST/WON) → exclude permanently
        #   - Re-price request → owe revision, never chase
        #   - REDIRECT → resume chasing the NEW contact (process_followup switched it)
        #   - ANY other reply (still pending / "we'll reach out" / engaged / OOO) →
        #     HOLD. Ball is in their court. NO auto-resume on a timer. The bid stays
        #     visible in the skip list so the user can nudge it manually.
        #   - No reply at all → daily chase cadence (every day until they answer)
        if already_chased_today:
            _skip(r, "already chased today"); continue
        # WEEKLY cadence override (user 6/2): some bids are chased once a week,
        # not daily. Eligible only if last chase was >= 7 days ago.
        # BUT chase-until-reply (FORCE_CHASE, user 6/17) beats EVERY softer cadence
        # — weekly included — so a bid the user said "chase until they respond"
        # stays daily-eligible even if it was once on the weekly list (some bids
        # are on both). FORCE_CHASE > weekly > competitive-gap > dormant-cap.
        if iid[:8].lower() in WEEKLY_CADENCE_IIDS and iid[:8].lower() not in FORCE_CHASE:
            days_since_chase = (today - last.date()).days if last else 99999
            if days_since_chase < 7:
                nxt = (last.date() + timedelta(days=7)).strftime("%m/%d") if last else "?"
                _skip(r, f"weekly cadence — last chased {last.strftime('%m/%d') if last else 'never'}, next ~{nxt}")
                continue
        resume_note = None   # per-row; set only when a replied-hold expires
        if last_reply:
            rcat, _ = classify_reply(last_reply["subj"], last_reply.get("body", ""))
            rdate = last_reply["at"].strftime("%m/%d") if hasattr(last_reply["at"], "strftime") else "?"
            # GC asked us to RE-PRICE / rebid a reduced scope → the ball is in OUR
            # court. NEVER chase — we owe them a revised proposal. Sticky (not a
            # buffer that expires) so a stray later chase can't un-flag it.
            # (6/5: a GC PM asked to re-price the reduced scope on
            # 6/1; chasing "any update?" is backwards.)
            if rcat == "REVISE_REQUESTED":
                if not _owe_resolved(_thread_state(iid_tag, proj_words, proj_nums)):
                    _skip(r, f"GC asked to RE-PRICE {rdate} — we owe a revised proposal (not a chase)")
                    continue
                log(f"[report] {bid_id}: re-price {rdate} already answered — not a we-owe",
                    args.quiet)
                # fall through: treat as submitted/awaiting, not an open we-owe
            if rcat in ("LOST", "WON", "DO_NOT_CONTACT"):
                _terminal_mark(iid, rcat, gc=gc,
                               evidence=f"{rdate}: {last_reply.get('subj','')[:80]}")
                tag = ("DO-NOT-CONTACT — GC asked us to STOP (permanent)"
                       if rcat == "DO_NOT_CONTACT" else f"definitive reply ({rcat})")
                _skip(r, f"{tag} {rdate}"); continue
            # REDIRECT is NOT a permanent stop. It means the GC handed the bid
            # to a new PM; process_followup_replies already switched the CRM
            # contact, so we RESUME chasing the NEW contact on the normal daily
            # cadence — we don't park the bid forever. (6/3: on one bid the GC
            # contact redirected us to a new PM on 5/26; that PM then went silent
            # and MUST be chased, not excluded.) So REDIRECT just falls through.
            # Soft replies — HOLD, do NOT auto-resume on a timer. Once a GC has
            # replied at all (still pending / "we'll reach out" / engaged / OOO) the
            # ball is in THEIR court. The old "7-day buffer then resume" re-chased GCs
            # who had already answered — twice (two real bids where the GC said
            # "they'll reach out"). A reply now parks the bid: it stays VISIBLE in the skip list
            # so the user can nudge it manually, but it never auto-enters the chase
            # batch again on its own. (user 6/5: "if they replied, don't chase them")
            # USER POLICY 6/11 (supersedes 6/5 indefinite hold): "if they
            # reply, fuck them after 5 days, or the day they ask to come back."
            #   1. Reply asked US something → WE owe an answer, never chase
            #   2. Reply names a comeback date → resume that day (+1 if a
            #      meeting/decision event)
            #   3. Otherwise → resume 5 days after the reply
            if rcat in ("STILL_PENDING", "UNCLEAR", "ENGAGED", "PRICING_PUSHBACK",
                        "OUT_OF_OFFICE"):
                gist = reply_gist(last_reply.get("body", ""))
                said = f' said: "{gist}" —' if gist else f" ({rcat}) —"
                body_r = last_reply.get("body", "") or ""
                if ASK_AT_US.search(body_r[:1500]):
                    # 6/17 gate: did we ALREADY answer them after the question?
                    _ts = _thread_state(iid_tag, proj_words, proj_nums)
                    _answered = bool(_ts and _ts.get("who_spoke_last") == "OUT"
                                     and _ts.get("last_in"))
                    if not _answered:
                        _skip(r, f"replied {rdate}{said} THEY ASKED US something — "
                                 f"we owe an answer (human item, not a chase)")
                        continue
                    log(f"[report] {bid_id}: their question {rdate} already answered "
                        f"(we spoke last) — not a we-owe", args.quiet)
                    # fall through: we replied after their question
                # 6/16 FIX (Midtown/LFJ): GC committed to contacting US ("we'll
                # keep you posted / on standby / waiting on the owner"). HARD
                # ball-in-their-court HOLD — never auto-chase on a timer. Stays in
                # the skip list for a manual nudge. Supersedes the 6/11 5-day timer
                # for these "we will initiate" replies (re-chasing them is the exact
                # relationship damage the user flagged — one GC contact replied 3x).
                if WILL_INITIATE.search(_new_text(body_r)):
                    _skip(r, f"replied {rdate}{said} GC will contact US "
                             f"(waiting on owner / on standby) — HOLD, ball in their "
                             f"court, no auto-chase until they reply")
                    continue
                rdt = (last_reply["at"].date()
                       if hasattr(last_reply["at"], "date") else None)
                cb, cb_label = (parse_comeback(body_r, last_reply["at"])
                                if rdt else (None, ""))
                # 6/16 god-level: OUT-OF-OFFICE auto-reply is NOT a real answer.
                # Don't 5-day-timer a vacationing person and then re-chase them
                # while they're still out (audit HIGH). If the OOO names a return
                # date, parse_comeback caught it (cb) → hold until then. If it
                # names NO date, HOLD until a real human reply — never auto-resume.
                if rcat == "OUT_OF_OFFICE" and not cb:
                    _skip(r, f"replied {rdate}{said} OUT-OF-OFFICE auto-reply, no "
                             f"return date — HOLD until a real reply (not a chase)")
                    continue
                resume_d = cb or (rdt + timedelta(days=5) if rdt else None)
                if resume_d and today < resume_d:
                    why = f"they said {cb_label}" if cb else "5-day hold"
                    _skip(r, f"replied {rdate}{said} resume "
                             f"{resume_d.strftime('%m/%d')} ({why})")
                    continue
                # hold expired (or comeback day arrived) → RESUME chasing
                resume_note = (f"resume — they said {cb_label}" if cb
                               else f"replied {rdate}, 5-day hold expired — resuming")

        # SAFETY: if the bid has an ACTIVE back-and-forth thread (the team
        # has replied to the GC recently AND the GC has replied recently —
        # multiple rounds), DO NOT auto-chase. The owner or the user is handling
        # it directly. User rule (5/27 AMC North DeKalb incident): "we were
        # already working WITH the GC on a CO and you sent him a chase asking
        # for an update on the CO he literally said he'd send us."
        # Heuristic: ≥2 GC replies AND ≥1 reply from one of our own addresses in
        # the last 30 days on this thread = active dialogue → skip.
        # Tracked via inbox_replies_seen / sent_to_recip dicts.
        if last_reply and days_since_reply < 30:
            em_addrs = set()
            for a in re.split(r"[,;\s]+", r.get("Contact Email","") or ""):
                if "@" in a: em_addrs.add(a.strip().lower())
            for a in re.split(r"[,;\s]+", r.get("CC Contacts","") or ""):
                if "@" in a: em_addrs.add(a.strip().lower())
            # 6/3 — EXCLUDE auto-replies from the "active dialogue" count.
            # One bid was wrongly flagged "team handling" because two contacts'
            # OOO auto-replies counted as 2 GC replies —
            # but an out-of-office bounce is not a conversation.
            def _is_auto(subj):
                s = (subj or "").lower()
                return any(x in s for x in ("automatic reply", "auto-reply",
                                            "autoreply", "out of office"))
            recent_reply_count = sum(
                1 for a in em_addrs
                if a in inbox_replies_seen
                and (today_dt - inbox_replies_seen[a]["at"]).days < 30
                and not _is_auto(inbox_replies_seen[a].get("subj", ""))
            )
            if recent_reply_count >= 2:
                # Multiple GC engagements — team is handling, skip auto-chase
                _skip(r, "active back-and-forth (team handling directly)"); continue

        # 5-DAY POST-PROPOSAL QUIET WINDOW (user rule 5/29): if WE sent an
        # original or revised proposal to this GC in the last 5 days, do
        # not chase yet. Give them time to read it. Avoids the "we just
        # submitted a $64K revised bid this morning, why are we chasing
        # them at 1 PM" failure mode.
        try:
            from _lib.presend_reply_guard import recent_proposal_sent_to
            prop_hit = recent_proposal_sent_to(
                to_email=primary, iid_full=iid, project_name=proj,
                hours=24*5, verbose=False,
            )
            if prop_hit:
                _skip(r, f"proposal sent {prop_hit['at'][:10]} — 5-day quiet window")
                continue
        except Exception:
            pass  # fail-open

        # ── TIER CADENCE + LIFETIME CAP (god-level 6/16). Applies to bids that
        #    reached here on cadence (silent / resume-expired). resume_note set =>
        #    honoring a comeback date the GC named; never block that.
        _dom = primary.split("@", 1)[1] if "@" in primary else ""
        _tier = "relationship" if _dom in TIERS["rel_dom"] else "competitive"
        _life = int(COUNTS.get(iid, 0) or 0)
        _cap = TIERS["rel_cap"] if _tier == "relationship" else TIERS["cmp_cap"]
        _gap = TIERS["rel_gap"] if _tier == "relationship" else TIERS["cmp_gap"]
        if not resume_note and last:
            # (a) DORMANT — chased to cap with ZERO replies ever: stop auto-chasing
            #     UNLESS the user put it on chase-until-reply (FORCE_CHASE).
            if last_reply is None and _life >= _cap and iid[:8].lower() not in FORCE_CHASE:
                _skip(r, f"DORMANT — {_life} chases, ZERO replies ever ({_tier} cap "
                         f"{_cap}); no manual call, wait until they respond")
                continue
            # (b) tier spacing — don't hammer: relationship ~weekly, competitive ~3d.
            #     FORCE_CHASE bids skip the gap → eligible daily until they reply.
            _since = (today - last.date()).days
            if _since < _gap and iid[:8].lower() not in FORCE_CHASE:
                _nxt = (last.date() + timedelta(days=_gap)).strftime("%m/%d")
                _skip(r, f"{_tier} cadence — last {last.strftime('%m/%d')}, "
                         f"every {_gap}d, next ~{_nxt}")
                continue

        # Eligible — add to plan
        reason = "never chased" if not last else (
            f"last chase {int(hours_since_chase / 24)}d ago" if hours_since_chase > 48
            else f"last chase {int(hours_since_chase)}h ago"
        )
        _rn = resume_note
        proposed.append({
            "bid_id": bid_id, "internal_id": iid, "project": proj, "gc": gc,
            "amount": amt, "to": primary,
            "reason": f"{_rn} | {reason}" if _rn else reason,
            "resume": bool(_rn),
            "submitted": sub_d_str,
        })

    # Sort: never-chased oldest-sub first, then by gap descending
    def sort_key(p):
        # First: never chased (gap=infinity) → put first by oldest sub
        if "never chased" in p["reason"]:
            return (0, p["submitted"])  # earlier sub date = string sorts smaller
        return (1, p["reason"])
    proposed.sort(key=sort_key)

    if proposed:
        lines.append("")
        lines.append(f"📋 *Today's Proposed Chase Plan — {today.strftime('%a %b %d')}*")
        lines.append(f"_{len(proposed)} eligible bid(s). NOTHING sends until you "
                     f"approve — reply 'approve' to authorize today's batch (gate is live)._")
        lines.append("")
        for p in proposed[:30]:
            amt_disp = f"${p['amount']:,}" if p['amount'] else "$?"
            lines.append(f"• {p['bid_id']} {p['project'][:38]} ({amt_disp}) → {p['to'][:30]} _({p['reason']})_")

    # 6/1 — ALWAYS show the real skip list so the rationale is grounded and
    # Carol never invents fake "skipped" bids. These are REAL active bids
    # excluded today, each with the true reason.
    if skipped:
        lines.append("")
        lines.append(f"⏸️ *Skipped today ({len(skipped)})* — active, but not due:")
        for s in sorted(skipped, key=lambda x: x["reason"]):
            lines.append(f"• {s['bid_id']} {s['proj'][:40]} — _{s['reason']}_")

    # Fill each intel record's standing from proposed/skipped membership.
    _prop_ids = {p["bid_id"] for p in proposed}
    _skip_reason = {s["bid_id"]: s["reason"] for s in skipped}
    for it in intel:
        if it["standing"] is None:
            if it["bid_id"] in _prop_ids:
                it["standing"] = "chasing today"
            elif it["bid_id"] in _skip_reason:
                it["standing"] = _skip_reason[it["bid_id"]]
            else:
                it["standing"] = "—"

    # 6/1 — Follow-Up Intelligence: per-bid "who answered / who's silent".
    # Grounded in real chase + reply data; never invented. Shown only with
    # --intel so the default brief stays a clean plan.
    def _short_standing(reason):
        r = (reason or "").lower()
        if reason == "chasing today":     return "→ chasing today"
        if "quiet window" in r:           return "post-proposal hold"
        if "buffer" in r:
            m = re.search(r"(\d+)d left", r)
            return f"in reply buffer ({m.group(1)}d left)" if m else "in reply buffer"
        if "active back-and-forth" in r:  return "active dialogue (team handling)"
        if "user handles" in r:           return "you handle directly"
        if "no contact email" in r:       return "⚠ no contact email"
        if "definitive" in r:             return reason
        return reason

    if args.intel and intel:
        replied = [i for i in intel if i["reply_date"]]
        silent = [i for i in intel if not i["reply_date"]]
        lines.append("")
        lines.append(f"📊 *Follow-Up Intelligence — {len(intel)} active bid(s)*")
        lines.append(f"_{len(replied)} replied · {len(silent)} silent · "
                     f"deeper detail per bid: loss_postmortem.py --bid BID-NNNN_")
        if replied:
            lines.append("")
            lines.append("*Replied (GC got back to us):*")
            for i in sorted(replied, key=lambda x: x["reply_date"], reverse=True):
                lines.append(
                    f"• {i['bid_id']} {i['proj'][:36]} — last reply {i['reply_date']} · "
                    f"{_short_standing(i['standing'])}")
        if silent:
            lines.append("")
            lines.append("*Silent (no GC reply on file):*")
            for i in sorted(silent, key=lambda x: (x["last_chase"] or "~")):
                lc = f"last chased {i['last_chase']}" if i["last_chase"] else "never chased"
                lines.append(f"• {i['bid_id']} {i['proj'][:36]} — {lc} · "
                             f"{_short_standing(i['standing'])}")

    # ── FULL-COVERAGE GUARANTEE (user 6/11: "no single bid forgotten — ──
    # everything in the CRM checked for follow-up possibility, every day").
    # Reconcile: every active row must be proposed or skipped-with-reason;
    # rows that fell through every rule get flagged LOUDLY, never silently.
    _accounted = ({p.get("bid_id") for p in proposed} |
                  {s.get("bid_id") for s in skipped if isinstance(s, dict)})
    _all_active = [r for r in crm_rows if r.get("Status") in ACTIVE_STATUSES]
    _unaccounted = [r for r in _all_active
                    if r.get("Bid #") and r.get("Bid #") not in _accounted]
    for r in _unaccounted:
        why = ("⚠ NO INTERNAL ID — unchaseable until the CRM row is fixed"
               if not (r.get("Internal ID") or "").strip()
               else "⚠ UNEVALUATED — fell through every rule; HUMAN CHECK")
        _skip(r, why)
    _onhold = [{"bid_id": r.get("Bid #"), "project": r.get("Project Name", "")[:44],
                "note": (str(r.get("Notes") or "")[:90])}
               for r in crm_rows if r.get("Status") == "On Hold"]
    coverage = {
        "crm_active": len(_all_active),
        "on_hold": len(_onhold),
        "proposed": len(proposed),
        "held_with_reason": len(skipped),
        "flagged_unaccounted": len(_unaccounted),
    }
    lines.append("")
    if _onhold:
        lines.append(f"📦 *On Hold ({len(_onhold)})* — parked, not forgotten:")
        for h in _onhold:
            lines.append(f"  • {h['bid_id']} {h['project']} — _{h['note'][:70]}_")
    _total = len(_all_active) + len(_onhold)
    _seen = len(proposed) + len(skipped) - len(_unaccounted) + len(_onhold)
    _mark = "✅" if not _unaccounted else "⚠️"
    lines.append(f"{_mark} *Coverage: {len(proposed)} chasing + "
                 f"{len(skipped)} held-with-reason + {len(_onhold)} on-hold "
                 f"= {_total} CRM rows — every bid evaluated*"
                 + ("" if not _unaccounted else
                    f" ({len(_unaccounted)} FLAGGED FOR HUMAN CHECK)"))

    # Save proposed plan as JSON (for execution after approval)
    PLAN_DIR.mkdir(parents=True, exist_ok=True)
    plan_file = PLAN_DIR / f"proposed_chases_{today.isoformat()}.json"
    plan_file.write_text(json.dumps({
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "for_date": today.isoformat(),
        "yesterday_recap": {
            "date": yesterday.isoformat(),
            "total_chased": n,
            "replied": replied,
            "ignored": ignored,
            "by_category": dict(by_cat),
            "outcomes": [{**o, "at": o["at"].isoformat() if o.get("at") else None,
                          "reply_at": o["reply_at"].isoformat() if o.get("reply_at") else None}
                         for o in outcomes],
        },
        "proposed": proposed,
        "skipped": skipped,
        "on_hold": _onhold,
        "coverage": coverage,
    }, indent=2, default=str), encoding="utf-8")
    lines.append("")
    lines.append(f"_Plan saved: {plan_file.relative_to(ROOT)}_")

    body = "\n".join(lines)
    print(body)

    if args.telegram and not args.no_telegram:
        import urllib.request, urllib.parse
        tok = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat = os.environ.get("USER_TELEGRAM_CHAT_ID", "")

        def _send(text, parse_mode):
            payload = {"chat_id": chat, "text": text}
            if parse_mode:
                payload["parse_mode"] = parse_mode
            data = urllib.parse.urlencode(payload).encode()
            urllib.request.urlopen(urllib.request.Request(
                f"https://api.telegram.org/bot{tok}/sendMessage", data=data),
                timeout=10)

        if tok and chat:
            # 6/1 fix — the brief was failing to DELIVER with "HTTP 400 Bad
            # Request" because Markdown parse_mode chokes on characters in the
            # body ([ID:..] brackets, '#', unbalanced '_' in emails/subjects).
            # When that happens the user gets NOTHING and falls back to asking
            # manually (or Carol runs the wrong script). So: try Markdown, and
            # on ANY failure retry as PLAIN TEXT so the brief always arrives.
            try:
                _send(body, "Markdown")
            except Exception as e1:
                log(f"[report] telegram Markdown failed ({e1}); retrying plain text", args.quiet)
                try:
                    _send(body, None)           # plain text — no parse_mode
                    log("[report] telegram delivered as plain text", args.quiet)
                except Exception as e2:
                    log(f"[report] telegram err (plain text also failed): {e2}", args.quiet)

    return 0


if __name__ == "__main__":
    sys.exit(main())
