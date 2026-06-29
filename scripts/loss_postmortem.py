#!/usr/bin/env python3
r"""
loss_postmortem.py — Trace email history for lost bids and build per-project
post-mortem reports.

For each Lost bid in the CRM:
  1. Read CRM row (project name, GC, dates, Loss Reason — authoritative)
  2. Search Gmail INBOX + Sent for matching threads
  3. Build a timeline: original ITB → proposal sent → follow-ups → GC replies
  4. Extract any commentary from GC replies that hints at why we lost
  5. Write per-bid markdown report to data/memory/loss_postmortems/{bid_id}.md
  6. (Optional) Append a one-line summary to the CRM "Notes" column

CRM "Loss Reason" is treated as the source of truth and is NEVER overwritten.
Email-derived insights are *supplementary* — they go to Notes (or the postmortem
md file) so the user's authoritative reason stays clean.

Run:
  python scripts/loss_postmortem.py                    # all lost bids
  python scripts/loss_postmortem.py --bid BID-0024     # one bid
  python scripts/loss_postmortem.py --status "Awaiting Decision"  # other statuses
  python scripts/loss_postmortem.py --write-notes      # append summary to CRM Notes
  python scripts/loss_postmortem.py --quiet
"""

import argparse
import difflib
import email as email_lib
import imaplib
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime
from email.header import decode_header
from email.utils import parsedate_to_datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Load .env BEFORE reading any credential from the environment. When Carol
# (OpenClaw/Telegram) shells out to this script, GMAIL_APP_PASSWORD is NOT
# in the process env — without this, login fails and Carol tells the user
# "the script doesn't have the correct password configured" (5/30 incident).
# Always load from the project .env by ABSOLUTE path so cwd doesn't matter.
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    pass

POSTMORTEM_DIR = ROOT / "data" / "memory" / "loss_postmortems"
LOG_FILE       = ROOT / "data" / "logs" / "loss_postmortem.log"

GMAIL_USER = os.environ.get("GMAIL_USER", "estimates@carolinacommercialfinishes.com")
GMAIL_PASS = os.environ.get("GMAIL_APP_PASSWORD", "")

# Team alias addresses (owner/teammate personal inboxes used to forward ITBs).
# Sourced from .env so no personal addresses ship in source.
TEAM_ALIAS_EMAILS = tuple(
    a.strip().lower()
    for a in os.environ.get("OWNER_ALIAS_EMAILS", "").split(",")
    if a.strip()
)

# Body snippet length per email
SNIPPET_CHARS = 700
# Max threads to inspect per bid (avoid runaway scans)
MAX_THREADS = 30
# Words shorter than this are skipped when building search keywords
MIN_KEYWORD_LEN = 4


def normalize(s: str) -> str:
    if not s: return ""
    s = s.lower()
    s = re.sub(r"#\s*\d+", "", s)
    s = re.sub(r"[(),\-/_|]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9\s-]", "", (name or "").lower())
    s = re.sub(r"\s+", "-", s).strip("-")
    return re.sub(r"-+", "-", s)[:80]


STOP_WORDS = {"the", "a", "an", "and", "or", "of", "for", "store", "project",
              "building", "renovation", "upfit", "construction", "company",
              "phase", "facility", "office", "north", "south", "east", "west",
              "new", "ltd", "llc", "inc", "co", "nc", "sc", "ga", "fl",
              # Generic food/retail words that cross-match unrelated projects
              "foods", "food", "market", "remodel", "shop", "center",
              "concept", "service", "services", "group", "lobby"}


def project_keywords(project_name: str) -> list[str]:
    """Project-specific anchors. Returns DISTINCTIVE single words + bigrams + numbers."""
    if not project_name: return []
    words = re.findall(r"[A-Za-z0-9#]+", project_name)
    out = []
    # Numbers (store/project numbers are great anchors)
    for w in words:
        wl = w.lower().strip("#")
        if wl.isdigit() and len(wl) >= 3:
            out.append(w.strip("#"))
    # Distinctive single words
    for w in words:
        wl = w.lower().strip("#")
        if wl in STOP_WORDS or wl.isdigit(): continue
        if len(wl) < MIN_KEYWORD_LEN: continue
        out.append(w)
    # Bigrams from consecutive non-stop words (better anchoring)
    for i in range(len(words) - 1):
        a, b = words[i], words[i+1]
        if a.lower() in STOP_WORDS or b.lower() in STOP_WORDS: continue
        if len(a) < 3 or len(b) < 3: continue
        bigram = f"{a} {b}"
        if bigram not in out:
            out.append(bigram)
    # De-dupe preserving order
    seen, dedup = set(), []
    for w in out:
        if w.lower() not in seen:
            seen.add(w.lower())
            dedup.append(w)
    return dedup[:6]


def gc_keywords(gc_name: str) -> list[str]:
    """GC-specific anchors (distinguishing token only)."""
    if not gc_name: return []
    words = re.findall(r"[A-Za-z0-9]+", gc_name)
    out = []
    for w in words:
        if w.lower() in STOP_WORDS: continue
        if len(w) < 4: continue
        out.append(w)
    return out[:2]


def decode_field(raw):
    """Decode an email header value safely."""
    if not raw: return ""
    out = ""
    for p, e in decode_header(str(raw)):
        if isinstance(p, bytes):
            try:
                out += p.decode(e or "utf-8", errors="replace")
            except LookupError:
                out += p.decode("utf-8", errors="replace")
        else:
            out += p
    return re.sub(r"\s+", " ", out).strip()


def get_body_snippet(msg, max_chars=SNIPPET_CHARS) -> str:
    """Pull plain-text body from an email message, truncated."""
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    try:
                        body = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                    except (LookupError, AttributeError):
                        body = payload.decode("utf-8", errors="replace")
                    break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            try:
                body = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
            except (LookupError, AttributeError):
                body = payload.decode("utf-8", errors="replace")
    body = re.sub(r"\r\n?", "\n", body)
    # Strip quoted reply blocks (lines starting with >)
    body = "\n".join(l for l in body.split("\n") if not l.lstrip().startswith(">"))
    # Cut Outlook/Gmail-style quoted history + signatures so we keep only the
    # NEW reply text (otherwise the snippet trails into our own quoted email
    # and the GC's signature block — noise that misleads a summarizer).
    cut_markers = [
        r"\n_{5,}\n",                         # Outlook "________" divider
        r"\nFrom:\s.+?\nSent:",               # Outlook quoted header
        r"\n-{2,}\s*Original Message",        # "-----Original Message-----"
        r"\nOn .{0,80}\bwrote:",              # Gmail "On <date> X wrote:"
        r"\nSent from my ",                   # mobile sig
    ]
    for mk in cut_markers:
        m = re.search(mk, body, re.IGNORECASE | re.DOTALL)
        if m:
            body = body[:m.start()]
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return body[:max_chars] + ("…" if len(body) > max_chars else "")


def gmail_search(M, folder: str, gmail_query: str) -> list:
    """Run a Gmail X-GM-RAW search in the given folder. Returns list of message dicts."""
    try:
        M.select(folder)
    except Exception:
        return []
    # Sanitize query: strip quotes, control chars; keep keywords only
    safe = re.sub(r'["\\\x00-\x1f]', " ", gmail_query)
    safe = re.sub(r"\s+", " ", safe).strip()
    if not safe:
        return []
    try:
        typ, data = M.search(None, "X-GM-RAW", f'"{safe}"')
    except imaplib.IMAP4.error:
        return []
    if typ != "OK" or not data or not data[0]:
        return []
    ids = data[0].split()
    if len(ids) > MAX_THREADS:
        ids = ids[-MAX_THREADS:]   # most recent
    out = []
    for mid in ids:
        typ, raw = M.fetch(mid, '(BODY.PEEK[])')
        if typ != "OK" or not raw or not raw[0]:
            continue
        msg = email_lib.message_from_bytes(raw[0][1])
        try:
            dt = parsedate_to_datetime(msg.get("Date", "")) if msg.get("Date") else None
        except Exception:
            dt = None
        out.append({
            "folder": folder,
            "subject": decode_field(msg.get("Subject")),
            "from": decode_field(msg.get("From")),
            "to": decode_field(msg.get("To")),
            "date": dt,
            "date_str": msg.get("Date", ""),
            "snippet": get_body_snippet(msg),
            "thread_id": msg.get("In-Reply-To") or msg.get("Message-ID") or "",
        })
    return out


def find_threads_for_bid(M, project_name: str, gc_name: str, log_fn) -> list:
    """Search Inbox + Sent for emails relevant to this bid.

    Strategy: search by PROJECT keywords (not GC keywords) so we don't pull
    every email from a high-volume GC. Then require the project keyword to
    actually appear in the subject for inclusion.
    """
    proj_kw = project_keywords(project_name)
    gc_kw = gc_keywords(gc_name)
    if not proj_kw:
        return []
    # Gmail search: project keywords only — GC name alone is too broad
    terms = [f'"{k}"' if " " in k else k for k in proj_kw]
    query = " OR ".join(terms)
    log_fn(f"    project kw: {proj_kw}")
    log_fn(f"    GC kw:      {gc_kw}")
    log_fn(f"    query:      {query}")

    threads = []
    # 5/30 fix — search All Mail, NOT INBOX+Sent. The original proposal lives
    # in Sent, GC replies often get auto-labeled OUT of INBOX (e.g. to
    # "Known GC"), and team ITB forwards sit wherever filters put them. Only
    # All Mail contains EVERYTHING. Direction (sent vs received) is decided by
    # the From address in build_timeline(), not by folder. (Carvana bug: the
    # 4/10 ITB + 4/21 proposal were invisible because they weren't in INBOX
    # and the proposal subject "Carvana / Adesa … Proposal" wasn't matched.)
    threads.extend(gmail_search(M, '"[Gmail]/All Mail"', query))
    # de-dup (All Mail can list a message once per label)
    _seen, _dedup = set(), []
    for _t in threads:
        _k = (_t.get("thread_id", ""), _t.get("subject", ""), str(_t.get("date", "")))
        if _k in _seen:
            continue
        _seen.add(_k); _dedup.append(_t)
    threads = _dedup

    # Filter: subject MUST contain a STRONG project anchor — either a
    # multi-word bigram (e.g. "Durham 85", "Sunbelt Kennesaw") or a 3+ digit
    # number (e.g. "0473"). Single common words alone are too weak — "Concept"
    # would match Whole Foods Concept emails when our project is Durham 85
    # Concept Foods.
    bigrams = [k for k in proj_kw if " " in k]
    numbers = [k for k in proj_kw if k.strip("#").isdigit() and len(k.strip("#")) >= 3]
    strong_anchors = bigrams + numbers
    # If we have no bigrams or numbers, fall back to single keywords (small projects)
    anchors_for_filter = strong_anchors if strong_anchors else proj_kw

    # 5/30 fix — match anchors AFTER collapsing punctuation, so a bigram like
    # "carvana adesa" matches the real subject "Carvana / Adesa Jacksonville
    # FL — Painting Proposal". Also accept a DISTINCTIVE single token (a brand
    # / proper noun, ≥5 chars, not a generic construction word) so the ITB
    # forward "Fwd: Carvana - Jacksonville, FL" (no bigram) is still caught.
    def _norm_sep(s: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (s or "").lower())).strip()

    _COMMON = {"center", "centre", "concept", "building", "construction",
               "facility", "project", "store", "phase", "painting", "proposal",
               "remodel", "renovation", "hospital", "school", "dental", "retail",
               "campus", "fitness", "suites", "hotel", "food", "grandstands",
               "community", "fellowship", "health", "reconditioning", "independent",
               "living", "generator", "partitions"}
    distinct_singles = [k.lower() for k in proj_kw
                        if " " not in k and len(k) >= 5 and k.lower() not in _COMMON]

    def has_project_match(t):
        subj_n = _norm_sep(t["subject"])
        for k in anchors_for_filter:
            kn = _norm_sep(k)
            if kn and kn in subj_n:
                return True
        for k in distinct_singles:
            if k in subj_n:
                return True
        return False

    threads = [t for t in threads if has_project_match(t)]
    # Score for ordering: project keyword count + GC bonus
    def score(t):
        text = f"{t['subject']} {t['snippet']}".lower()
        s = 0
        for k in proj_kw:
            if k.lower() in t["subject"].lower(): s += 3
            elif k.lower() in text: s += 1
        for g in gc_kw:
            if g.lower() in text: s += 1
        return s
    threads.sort(key=lambda t: (-score(t), t["date"] or datetime.min))
    # Re-sort by date ascending for timeline
    threads.sort(key=lambda t: t["date"] or datetime.min)
    return threads


def build_timeline(threads: list, submitted_date: str) -> dict:
    """Boil threads down to a structured timeline."""
    # 5/30 fix — classify direction by the FROM address, not the Gmail folder.
    # (We now search All Mail, so folder is always "All Mail" and the old
    # folder check mislabeled everything as inbound.) An email is OUTBOUND iff
    # it is from our domain.
    OUR_DOMAIN = "carolinacommercialfinishes.com"
    TEAM_FWD = TEAM_ALIAS_EMAILS

    def _from(t): return (t.get("from", "") or "").lower()
    def is_outbound(t): return OUR_DOMAIN in _from(t)
    def is_internal(t):  # our own sends OR a teammate forwarding us the ITB
        f = _from(t)
        return OUR_DOMAIN in f or any(a in f for a in TEAM_FWD)

    inbound = [t for t in threads if not is_outbound(t)]
    outbound = [t for t in threads if is_outbound(t)]

    # ITB invitation — a GENUINE invitation only: either a teammate forward
    # of the GC's ITB, or a non-reply message with invitation language. NEVER
    # fall back to "earliest inbound" — that is usually a GC *reply* (Re:…),
    # and labeling it the ITB both shows a wrong ITB date AND steals it from
    # the GC-reply count. If no real ITB email is in the mailbox, leave it
    # None and the report says so (the CRM still holds the ITB Received Date).
    def _is_reply(subj): return subj.lstrip().startswith(("re:", "re ", "fwd:"))
    itb = None
    for t in inbound:
        subj = t["subject"].lower()
        has_invite_lang = any(k in subj for k in ("invit", "itb", "rfq", "rfp")) \
                          or ("bid" in subj and "follow" not in subj and "re:" not in subj)
        if is_internal(t) and ("fwd" in subj or has_invite_lang):
            itb = t; break          # teammate forwarded us the GC's ITB
        if has_invite_lang and not _is_reply(subj):
            itb = t; break          # GC sent the ITB directly

    proposal = None
    for t in outbound:
        subj = t["subject"].lower()
        if any(k in subj for k in ("proposal", "quote")) and "follow" not in subj:
            proposal = t; break

    followups = [t for t in outbound if any(k in t["subject"].lower()
                  for k in ("follow", "fu", "checking in", "status", "quick check"))]
    # GC replies = inbound that are NOT our own team (exclude teammate ITB
    # forwards) and NOT the ITB itself. The real "did the GC respond" count.
    # (A real bug we hit: old code did inbound[1:], counting a teammate's
    # forward as a GC reply AND, after the first fix, the ITB fallback stole a
    # genuine GC reply — undercounting 2 real replies to 1.)
    gc_replies = [t for t in inbound if not is_internal(t) and t is not itb]

    return {
        "itb": itb,
        "proposal": proposal,
        "followups": followups,
        "gc_replies": gc_replies,
        "all_threads": threads,
    }


def extract_loss_hints(threads: list) -> list[str]:
    """Look for sentences in inbound emails that hint at why we lost."""
    HINT_PATTERNS = [
        r"\bwent\s+with\s+\w+",
        r"\bawarded?\s+to\s+\w+",
        r"\bselected\s+\w+",
        r"\bnot\s+(?:selected|chosen)",
        r"\bhigher\s+(?:than|by)",
        r"\bover\s+budget\b",
        r"\bpricing\b",
        r"\bcost\s+(?:was|came)\b",
        r"\bbudget\s+(?:constraint|issue)",
        r"\bproject\s+(?:cancel|delay|on\s+hold|killed|fell\s+through)",
        r"\bowner\s+decided\b",
        r"\bother\s+(?:vendor|contractor|painter|bidder)",
        r"\b\$\s*[\d,]+\s+(?:lower|higher)",
        # award / redirect signals (a real GC reply that redirected us to a PM)
        r"\bbeen\s+awarded\b",
        r"\bwe\s+(?:were|have\s+been)\s+awarded\b",
        r"\bturned?\s+over\s+to\b",
        r"\bproject\s+manager\b",
        r"\breach\s+out\s+to\b",
        r"\bnot\s+awarded\b",
        r"\bgo(?:ing)?\s+(?:with|a\s+different)\b",
    ]
    rx = re.compile("|".join(HINT_PATTERNS), re.IGNORECASE)
    hints = []
    for t in threads:
        # 5/30 fix — skip OUR OWN emails by SENDER, not folder. We now search
        # All Mail, so the old `"[Gmail]/Sent Mail" in folder` check never
        # fired and the extractor pulled "hints" from our own follow-ups,
        # then attributed them to the GC. (One real case: it quoted our own
        # line "happy to clarify scope or revisit pricing" as if the GC said
        # it, and missed the GC's real reply — a redirect to a named PM.)
        # Only mine GENUINE inbound GC mail for hints.
        frm = (t.get("from", "") or "").lower()
        if "carolinacommercialfinishes.com" in frm:
            continue  # our own outbound — never a "GC said" hint
        if any(a in frm for a in TEAM_ALIAS_EMAILS):
            continue  # teammate forward — not the GC
        body = t["snippet"]
        for sentence in re.split(r"(?<=[.!?])\s+", body):
            if rx.search(sentence) and len(sentence) > 20 and len(sentence) < 300:
                hints.append((t["date"], sentence.strip()))
    # De-dupe
    seen = set()
    unique = []
    for d, s in hints:
        key = s.lower()[:80]
        if key not in seen:
            seen.add(key)
            unique.append((d, s))
    return unique[:5]


def fmt_email(t: dict) -> str:
    """One-liner for a thread. Direction by SENDER (we search All Mail now,
    so the folder is always 'All Mail' — the old folder check made every
    line render as inbound '←')."""
    d = t["date"].strftime("%Y-%m-%d") if t["date"] else "?"
    is_out = "carolinacommercialfinishes.com" in (t.get("from", "") or "").lower()
    direction = "→" if is_out else "←"
    other = t["to"] if is_out else t["from"]
    return f"  {d} {direction} {other[:40]:40}  {t['subject'][:70]}"


def build_postmortem_md(bid: dict, threads: list, hints: list) -> str:
    """Render the per-bid postmortem markdown."""
    timeline = build_timeline(threads, bid.get("submitted", ""))

    lines = [
        f"# Loss postmortem — {bid.get('name','?')}",
        f"_generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}_  ",
        f"_bid: {bid.get('bid_id','?')}_  ",
        f"_GC: {bid.get('gc','?')}_  ",
        f"_amount: ${(bid.get('amount') or 0):,.0f}_  ",
        f"_submitted: {bid.get('submitted','?')}_",
        "",
        "## CRM Loss Reason (authoritative)",
        f"> {bid.get('loss_reason') or '_(blank — no reason recorded)_'}",
        "",
    ]

    if hints:
        lines.append("## Loss hints from email replies")
        lines.append("_Sentences from GC inbound emails that may add context. NOT replacing the CRM Loss Reason._")
        lines.append("")
        for d, s in hints:
            ds = d.strftime("%Y-%m-%d") if d else "?"
            lines.append(f"- _{ds}_: {s}")
        lines.append("")

    lines.append("## Email timeline")
    lines.append("")
    if timeline["itb"]:
        lines.append("**Initial invitation (ITB):**")
        lines.append(fmt_email(timeline["itb"]))
        lines.append("")
    else:
        # No ITB email matched (often a teammate Gmail forward the keyword
        # search doesn't reach). Show the CRM's recorded date instead of
        # mislabeling a GC reply as the invitation.
        itb_date = (bid.get("itb_date") or bid.get("itb_received")
                    or bid.get("itb") or "").strip()
        lines.append("**Initial invitation (ITB):**")
        lines.append(f"  (ITB email not located in mailbox; CRM ITB Received Date: "
                     f"{itb_date or 'not recorded'})")
        lines.append("")
    if timeline["proposal"]:
        lines.append("**Proposal sent:**")
        lines.append(fmt_email(timeline["proposal"]))
        lines.append("")
    if timeline["followups"]:
        lines.append(f"**Follow-ups ({len(timeline['followups'])}):**")
        for t in timeline["followups"]:
            lines.append(fmt_email(t))
        lines.append("")
    if timeline["gc_replies"]:
        lines.append(f"**GC replies / inbound ({len(timeline['gc_replies'])}):**")
        for t in timeline["gc_replies"]:
            lines.append(fmt_email(t))
            # Show the ACTUAL reply text (quoted-history already stripped) so
            # Carol can answer "how did they respond?" by quoting verbatim —
            # not just list subjects. (5/30: Carol claimed she had "no tool to
            # fetch the body" when the body was right here, just not printed.)
            body = (t.get("snippet") or "").strip()
            if body:
                for bl in body.splitlines():
                    if bl.strip():
                        lines.append(f"      | {bl.strip()}")
            lines.append("")
        lines.append("")

    if not threads:
        lines.append("_No matching email threads found. Search keywords may need refinement._")
    else:
        lines.append(f"_Total relevant threads: {len(threads)}_")

    return "\n".join(lines)


def short_summary_for_notes(bid: dict, threads: list, hints: list) -> str:
    """One-line summary suitable for the CRM 'Notes' column."""
    parts = []
    if threads:
        timeline = build_timeline(threads, bid.get("submitted", ""))
        if timeline["proposal"]:
            d = timeline["proposal"]["date"]
            if d:
                parts.append(f"sent {d.strftime('%m/%d')}")
        if timeline["followups"]:
            parts.append(f"{len(timeline['followups'])} FU")
        if timeline["gc_replies"]:
            parts.append(f"{len(timeline['gc_replies'])} GC reply")
    if hints:
        parts.append(f"hint: {hints[0][1][:80]}")
    if not parts:
        return ""
    return "Email trace: " + "; ".join(parts)


def write_notes_to_crm(updates: list[tuple[str, str]]):
    """Append email summary to CRM 'Notes' column for given (bid_id, summary) pairs."""
    from scripts.crm_lib import get_sheet
    from gspread.utils import rowcol_to_a1
    ws = get_sheet("Bid Log")
    headers = ws.row_values(1)
    if "Bid #" not in headers or "Notes" not in headers:
        return 0
    bid_col = headers.index("Bid #") + 1
    notes_col = headers.index("Notes") + 1
    bid_ids = ws.col_values(bid_col)  # whole column as list
    cell_updates = []
    for bid_id, summary in updates:
        try:
            row_idx = bid_ids.index(bid_id) + 1  # 1-based
        except ValueError:
            continue
        # Read existing notes; only add our summary if not already there
        existing = ws.cell(row_idx, notes_col).value or ""
        if summary in existing:
            continue
        new_val = (existing + "\n" + summary).strip() if existing else summary
        cell_updates.append({"range": rowcol_to_a1(row_idx, notes_col),
                             "values": [[new_val]]})
    if cell_updates:
        ws.batch_update(cell_updates, value_input_option="USER_ENTERED")
    return len(cell_updates)


def log(msg: str, quiet: bool = False):
    if not quiet:
        print(msg)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now().isoformat(timespec='seconds')}  {msg}\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bid", help="Run for a single bid id (e.g. BID-0024)")
    ap.add_argument("--status", default="Lost", help="Status to scan (default 'Lost')")
    ap.add_argument("--year", type=int, default=None)
    ap.add_argument("--write-notes", action="store_true",
                    help="Append email-trace summary to CRM Notes column")
    ap.add_argument("--new-only", action="store_true",
                    help="Skip bids that already have a postmortem .md file (daemon mode)")
    ap.add_argument("--telegram", action="store_true",
                    help="Send Telegram ping when new postmortems are produced")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    POSTMORTEM_DIR.mkdir(parents=True, exist_ok=True)

    # Pull lost bids from CRM (authoritative source)
    from scripts.crm_stats import submitted_bid_stats
    s = submitted_bid_stats(year=args.year, status_filter=args.status)
    bids = s.get("submitted_in_year", [])
    if args.bid:
        bids = [b for b in bids if b.get("bid_id") == args.bid]

    # --new-only: skip bids that already have a postmortem.
    # Dedupe by Internal ID UUID (stable) + project name+GC signature (fallback)
    # — NOT by Bid# filename, because Bid# shifts every time the CRM is sorted
    # and the postmortem .md from 2 days ago has a stale Bid# prefix. Without
    # this fix the daemon re-investigated all 46 Lost bids every 4 hours and
    # spammed Telegram with "45 newly lost bid(s) investigated".
    if args.new_only:
        # Build the "already investigated" index from existing .json sidecars
        already_iid = set()
        already_sig = set()  # (project_name_norm, gc_norm)
        def _norm(s):
            return re.sub(r"[^a-z0-9]+", "", (s or "").lower())
        for jf in POSTMORTEM_DIR.glob("*.json"):
            try:
                d = json.loads(jf.read_text(encoding="utf-8"))
                iid = (d.get("internal_id") or "").strip()
                if iid:
                    already_iid.add(iid)
                sig = (_norm(d.get("name","")), _norm(d.get("gc","")))
                if sig[0]:
                    already_sig.add(sig)
            except Exception:
                continue
        skipped = 0
        kept = []
        for b in bids:
            iid = (b.get("internal_id") or "").strip()
            sig = (_norm(b.get("name","")), _norm(b.get("gc","")))
            if iid and iid in already_iid:
                skipped += 1
                continue
            if sig[0] and sig in already_sig:
                skipped += 1
                continue
            # Legacy fallback: old filename check
            bid_id = b.get("bid_id", "")
            slug = slugify(b.get("name", ""))
            if (POSTMORTEM_DIR / f"{bid_id}_{slug}.md").exists():
                # Old-style hit — also retire it: read the .md (if json missing)
                # and call this one done by signature.
                already_sig.add(sig)
                skipped += 1
                continue
            kept.append(b)
        if skipped:
            log(f"[postmortem] skipping {skipped} bids already investigated "
                f"(by Internal ID / project signature)", args.quiet)
        bids = kept

    if not bids:
        log(f"No new bids to process (status={args.status}, year={args.year}, bid={args.bid})", args.quiet)
        return 0
    log(f"[postmortem] {len(bids)} bid(s) to scan", args.quiet)
    new_postmortems = []  # for Telegram ping

    # Connect to Gmail once for all
    log(f"[postmortem] connecting to Gmail as {GMAIL_USER}...", args.quiet)
    M = imaplib.IMAP4_SSL("imap.gmail.com")
    M.login(GMAIL_USER, GMAIL_PASS)

    notes_updates = []
    try:
        for b in bids:
            bid_id = b.get("bid_id", "?")
            name = b.get("name", "?")
            gc = b.get("gc", "")
            log(f"\n[postmortem] {bid_id}  {name[:50]}  ({gc[:30]})", args.quiet)
            threads = find_threads_for_bid(M, name, gc, lambda m: log(m, args.quiet))
            hints = extract_loss_hints(threads)
            log(f"    threads={len(threads)}  hints={len(hints)}", args.quiet)

            md = build_postmortem_md(b, threads, hints)
            slug = slugify(name)
            out_path = POSTMORTEM_DIR / f"{bid_id}_{slug}.md"
            out_path.write_text(md, encoding="utf-8")
            # JSON sidecar — followup_scheduler.py and other consumers read THIS
            # instead of regex-parsing the .md (faster + more reliable).
            timeline = build_timeline(threads, b.get("submitted", ""))
            sidecar = {
                "bid_id": bid_id,
                "internal_id": b.get("internal_id", ""),
                "name": name,
                "gc": gc,
                "amount": b.get("amount", 0),
                "loss_reason": b.get("loss_reason", ""),
                "submitted": b.get("submitted", ""),
                "status": b.get("status", ""),
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "thread_count": len(threads),
                "followup_count": len(timeline.get("followups") or []),
                "gc_reply_count": len(timeline.get("gc_replies") or []),
                "last_followup_date": (
                    max((t["date"].strftime("%Y-%m-%d")
                         for t in (timeline.get("followups") or []) if t.get("date")),
                        default="")
                ),
                "last_gc_reply_date": (
                    max((t["date"].strftime("%Y-%m-%d")
                         for t in (timeline.get("gc_replies") or []) if t.get("date")),
                        default="")
                ),
                "hints": [
                    {"date": d.strftime("%Y-%m-%d") if d else "", "text": s}
                    for d, s in hints
                ],
            }
            (POSTMORTEM_DIR / f"{bid_id}_{slug}.json").write_text(
                json.dumps(sidecar, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            log(f"    saved → {out_path.name} (+ .json sidecar)", args.quiet)
            new_postmortems.append({
                "bid_id": bid_id, "name": name, "gc": gc,
                "amount": b.get("amount", 0),
                "loss_reason": b.get("loss_reason", ""),
                "thread_count": len(threads),
                "hint_count": len(hints),
                "first_hint": hints[0][1] if hints else "",
            })

            if args.write_notes:
                summary = short_summary_for_notes(b, threads, hints)
                if summary:
                    notes_updates.append((bid_id, summary))
            time.sleep(0.5)  # polite pacing
    finally:
        try: M.logout()
        except Exception: pass

    if args.write_notes and notes_updates:
        n = write_notes_to_crm(notes_updates)
        log(f"[postmortem] wrote email-trace summary to {n} CRM Notes cells", args.quiet)

    # Index file linking all postmortems
    index_lines = [
        f"# Loss postmortems index",
        f"_generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}_",
        "",
    ]
    for b in bids:
        bid_id = b.get("bid_id", "?")
        slug = slugify(b.get("name", ""))
        fname = f"{bid_id}_{slug}.md"
        index_lines.append(f"- [{bid_id} — {b.get('name','?')[:50]}](./{fname})  "
                           f"${(b.get('amount') or 0):,.0f}  ({b.get('gc','?')[:30]})  "
                           f"_reason: {b.get('loss_reason','?')[:60]}_")
    (POSTMORTEM_DIR / "_index.md").write_text("\n".join(index_lines), encoding="utf-8")
    log(f"\n[postmortem] index → {POSTMORTEM_DIR / '_index.md'}", args.quiet)

    # Telegram ping for new losses (only when --telegram flag set, e.g. daemon mode)
    if args.telegram and new_postmortems:
        try:
            import requests
            bot = os.environ.get("TELEGRAM_BOT_TOKEN", "")
            chat = os.environ.get("USER_TELEGRAM_CHAT_ID", "")
            lines = [f"📝 *Loss postmortems ready*", f"{len(new_postmortems)} newly lost bid(s) investigated:"]
            for pm in new_postmortems[:5]:
                lines.append(f"\n*{pm['bid_id']} — {pm['name'][:40]}*")
                lines.append(f"  GC: {pm['gc'][:30]}  ·  ${pm['amount']:,.0f}")
                if pm['loss_reason']:
                    lines.append(f"  Reason: _{pm['loss_reason'][:80]}_")
                lines.append(f"  📧 {pm['thread_count']} email threads, {pm['hint_count']} hint(s)")
                if pm['first_hint']:
                    lines.append(f"  💡 {pm['first_hint'][:120]}")
            requests.post(
                f"https://api.telegram.org/bot{bot}/sendMessage",
                json={"chat_id": chat, "text": "\n".join(lines), "parse_mode": "Markdown"},
                timeout=10,
            )
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
