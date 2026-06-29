#!/usr/bin/env python3
r"""
forward_div27.py — Auto-forward Division 27 (Communications / Structured Cabling
/ AV / Low-Voltage) bid invitations to Hyperscale Wiring.

CCF self-performs painting/wallcovering/coatings/FRP/flooring, not Div 27.
When a bid invitation includes Div 27 scope, those go to the sister company
Hyperscale Wiring (cs@hyperscalewiring.com) for them to bid that portion.

Match rules — needs BOTH to forward (so we don't ship random marketing email):
  1. FROM a known construction-context sender:
     - BC / CC / iSqFt / Procore / SmartBid notification systems, OR
     - any domain in our CRM GC Directory, OR
     - direct GC senders that look like construction PMs
  2. Subject OR body contains Div 27 trade markers:
     "structured cabling", "low voltage", "audio-visual", "AV systems",
     "voice/data communications", "telecom", "Division 27" / "Div 27",
     "communications cabling", "fiber backbone", "nurse call", etc.

State: data/memory/div27_forwarded.json — message IDs already forwarded.
Idempotent: each invite is forwarded at most once.

Forward target hard-coded to cs@hyperscalewiring.com (configurable via
HYPERSCALE_EMAIL env var).

Usage:
  python scripts/forward_div27.py                # forward new matches
  python scripts/forward_div27.py --dry-run      # show what would forward
  python scripts/forward_div27.py --days 60      # scan further back
"""
from __future__ import annotations
import argparse, imaplib, email as email_lib, json, os, re, smtplib, sys
from email.header import decode_header
from email.mime.message import MIMEMessage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import date, datetime, timedelta
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

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "data" / "memory" / "div27_forwarded.json"
LOG_FILE = ROOT / "data" / "logs" / "forward_div27.log"

try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

GMAIL_USER = os.environ.get("GMAIL_USER", "estimates@carolinacommercialfinishes.com")
GMAIL_PASS = os.environ.get("GMAIL_APP_PASSWORD", "")
HYPERSCALE = os.environ.get("HYPERSCALE_EMAIL", "cs@hyperscalewiring.com")

# Construction-context sender patterns (so we don't forward marketing email)
CONSTRUCTION_FROM_RE = re.compile(
    r"(constructconnectmail\.com|isqftmail\.com|buildingconnected\.com|"
    r"procoretech\.com|smartbidnet\.com|@[\w.-]*(?:construction|builders|"
    r"contracting|builds|cm\.com|construc|engineering|builders\.com|"
    r"pkwycon|fiicgc|lfjennings|monteith|wimco|delauter|blum|valiant|"
    r"newco|salcoacontracting|csgcharleston|metrolinabuilders|"
    r"pathcc|integrity-cm|mreconstructionllc|horizonretail|"
    r"flblum|williamssc|provost|rcsconstruction|vertexconstruction|"
    r"hbquickbuild|sauer|catamount|monteithco|wcconstructionco|"
    r"benchmarkbuilding))", re.I)

# Division 27 trade markers (need at least one in subject OR body)
DIV27_RE = re.compile(
    r"\b(structured\s+cabling|low[\s-]?voltage|audio[\s-]?visual|audio[\s-]?video|"
    r"av\s+system|av\s+integration|voice\s+communicat|data\s+communicat|"
    r"communications?\s+(?:backbone|horizontal|equipment|hardware|cabling|systems?)|"
    r"telecom(?!muting)|telephony|paging\s+system|division\s+27|csi\s+27|"
    r"div\.?\s*27|27\s*-\s*communicat|distributed\s+audio|public\s+address|"
    r"pa\s+system|nurse\s+call|intercom\s+system|video\s+surveill|cctv\s+system|"
    r"access\s+control|cat\s*[5-7]\b|fiber\s+backbone|head[-\s]?end|"
    r"network\s+cabling)\b", re.I)


def decode_h(s):
    out = ""
    for p, e in decode_header(s or ""):
        out += p.decode(e or "utf-8", errors="replace") if isinstance(p, bytes) else p
    return out


def load_state():
    if STATE.exists():
        try: return json.loads(STATE.read_text(encoding="utf-8"))
        except Exception: pass
    return {"forwarded": []}


def save_state(s):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(s, indent=2), encoding="utf-8")


def log(msg, quiet=False):
    if not quiet: print(msg)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now().isoformat(timespec='seconds')}  {msg}\n")


def is_match(from_h, subj, body):
    fr = (from_h or "")
    if not CONSTRUCTION_FROM_RE.search(fr):
        return False, "sender not in construction-context whitelist"
    blob = (subj or "") + "\n" + (body or "")
    m = DIV27_RE.search(blob)
    if not m:
        return False, "no Div 27 markers"
    return True, m.group(0)


def forward_via_smtp(orig_bytes, orig_from, orig_subj, matched_term):
    """Compose a forward from estimates@... to cs@hyperscalewiring.com with
    the ORIGINAL message attached as message/rfc822 (preserves all attachments)."""
    outer = MIMEMultipart()
    outer["From"] = GMAIL_USER
    outer["To"] = HYPERSCALE
    outer["Subject"] = f"[CCF→HW Div 27 forward] {orig_subj[:130]}"
    preamble = (
        f"Auto-forwarded by Carolina Commercial Finishes (estimates@carolinacommercialfinishes.com).\n\n"
        f"This bid invitation includes Division 27 / Communications / Low-Voltage scope "
        f"(matched: \"{matched_term}\"). CCF self-performs painting & finishes only — "
        f"forwarding the full message to Hyperscale Wiring for the Div 27 portion.\n\n"
        f"Original sender: {orig_from}\n"
        f"Original subject: {orig_subj}\n\n"
        f"Full original message + any attachments preserved below."
    )
    outer.attach(MIMEText(preamble, "plain", "utf-8"))
    inner = email_lib.message_from_bytes(orig_bytes)
    outer.attach(MIMEMessage(inner))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(GMAIL_USER, GMAIL_PASS)
        s.sendmail(GMAIL_USER, [HYPERSCALE], outer.as_bytes())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=45,
                    help="Scan Gmail Inbox back N days (default 45 — trade was added ~30d ago)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    state = load_state()
    done = set(state.get("forwarded", []))

    M = imaplib.IMAP4_SSL("imap.gmail.com")
    M.login(GMAIL_USER, GMAIL_PASS)
    M.select("INBOX")
    since = (date.today() - timedelta(days=args.days)).strftime("%d-%b-%Y")
    st, ids = M.search(None, f'(SINCE "{since}")')
    all_ids = ids[0].split() if ids[0] else []
    log(f"[fwd-div27] scanning {len(all_ids)} emails since {since}", args.quiet)

    candidates, would_skip = [], 0
    for mid in all_ids:
        st, data = M.fetch(mid, '(BODY.PEEK[])')
        if st != "OK": continue
        raw = data[0][1]
        msg = email_lib.message_from_bytes(raw)
        mid_str = (msg.get("Message-ID", "") or f"x-{mid.decode()}").strip("<>")
        if mid_str in done:
            continue
        from_h = msg.get("From", "")
        subj = decode_h(msg.get("Subject", ""))
        # Cheap body extract
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    try:
                        body = part.get_payload(decode=True).decode("utf-8", errors="replace")
                        break
                    except Exception: pass
        else:
            try: body = msg.get_payload(decode=True).decode("utf-8", errors="replace")
            except Exception: pass
        ok, reason = is_match(from_h, subj, body[:6000])
        if not ok:
            continue
        candidates.append({
            "mid_str": mid_str, "from": from_h, "subj": subj,
            "match": reason, "raw": raw,
            "date": msg.get("Date", "")[:25],
        })
    M.logout()

    log(f"[fwd-div27] {len(candidates)} unforwarded Div 27 candidates", args.quiet)
    for c in candidates:
        log(f"  [{c['date'][:22]}] FROM {c['from'][:40]}  → match='{c['match']}'  SUBJ {c['subj'][:60]}")

    if args.dry_run or not candidates:
        log(f"[fwd-div27] DRY-RUN (or nothing new) — no forwards sent.", args.quiet)
        return

    sent = 0
    for c in candidates:
        try:
            forward_via_smtp(c["raw"], c["from"], c["subj"], c["match"])
            done.add(c["mid_str"])
            sent += 1
            log(f"  → forwarded to {HYPERSCALE}: {c['subj'][:60]}", args.quiet)
        except Exception as e:
            log(f"  → FAIL: {e}  (subj={c['subj'][:50]})")
    state["forwarded"] = sorted(done)
    save_state(state)
    log(f"[fwd-div27] sent {sent}/{len(candidates)} forwards to {HYPERSCALE}", args.quiet)


if __name__ == "__main__":
    main()
