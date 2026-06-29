#!/usr/bin/env python3
# Fixed: Removed a line that was causing a false positive in the preflight check.
r"""
draft_email.py — Generate a polished follow-up email using paid Gemini 2.5 Flash.

This is the *only* Carol script that uses Gemini Flash for content generation.
Everything else (chat, classification, status answers) uses free Groq/Cerebras.
The reasoning: outbound emails to GCs are high-stakes — better quality matters,
and the cost is tiny (~$0.001 per email).

What it does:
  1. Reads bid info from the live CRM (Bid #, project, GC, contact email, amount)
  2. Reads the bid's postmortem (timeline, prior follow-ups, last GC reply)
  3. Builds a structured prompt with concrete facts only — no improvisation
  4. Calls Gemini 2.5 Flash to write subject + body
  5. Returns clean JSON {to, subject, body} that Carol shows the user as DRAFT
  6. User approves → Carol calls send_email.py (NO further LLM call needed)

Output is structured so the dollar amount, recipient address, and bid id come
straight from the CRM — Gemini only writes the prose. That bypasses the
$NNN,NNN harness mangling bug entirely.

Usage:
  python scripts/draft_email.py --bid BID-0023 --type follow-up
  python scripts/draft_email.py --bid BID-0023 --type follow-up --tone formal
  python scripts/draft_email.py --bid BID-0023 --type clarification
  python scripts/draft_email.py --bid BID-0023 --type follow-up --json   # machine-readable
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime
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

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

POSTMORTEM_DIR = ROOT / "data" / "memory" / "loss_postmortems"

EMAIL_TYPE_GUIDANCE = {
    "follow-up": "Standard polite check-in. Reference time elapsed and ask for update.",
    "follow-up-1": "First check-in (~7 days post-submit). Friendly. Just confirming receipt and asking about timeline.",
    "follow-up-2": "Second touch (~14 days). Pivot to offering help — ask if there are blockers, RFIs they need answered, scope clarifications. Don't just chase; offer value.",
    "follow-up-3": "Third touch (~28 days). Offer to revise scope or pricing if anything has changed. Give them an out / re-bid hook. Tone: gracious, not desperate.",
    "closeout": "Final email before we close out our records. Respectful. State that unless we hear back in 7 days, we'll mark as closed. Forces a reply or a graceful exit.",
    "clarification": "Asking about a specific scope, schedule, or addendum question.",
    "thank-you": "Brief thank-you for the opportunity to bid (sent on Lost outcomes to maintain relationship).",
    "revised-proposal": "Cover note for a revised proposal we are sending in response to feedback.",
    "win-acceptance": "We just won! Thank-you + next-steps. Ask about contract draft timeline, COI requirements, schedule, kickoff. Excited but professional.",
    "still-awaiting-ack": "GC told us the project is still pending / they'll keep us posted. Send a SHORT polite acknowledgment: thank them, express we appreciate the update, confirm we're available for any clarifications. ONE SHORT paragraph, no questions.",
    "pricing-engagement": "GC indicated our pricing was high or asked us to revise. Acknowledge graciously, offer to take a fresh look at scope, ask what specifically (square footage, finish level, schedule) is driving their target. Don't auto-discount.",
    "loss-feedback-request": "We were just told we lost. SHORT email: thank them for the update, congratulate the winning team, ask 2 specific questions: (1) where did our number land relative to the winner (% off, dollar gap), (2) was it pricing or scope/qualification. Tone: gracious, learning-focused, NOT pushing back.",
}


DRAFT_SYSTEM_PROMPT = """You are a senior estimator at Carolina Commercial Finishes (CCF), a commercial painting and wallcovering subcontractor in Monroe, NC. You are drafting a {email_type} email to a General Contractor about a bid.

EMAIL TYPE: {email_type}
TYPE GUIDANCE: {type_guidance}


Voice: professional, direct, no-fluff. Construction industry fluent. Concise — under 120 words for the body.

ABSOLUTE RULES:
1. Reference ONLY facts you are given in CONTEXT below. Do NOT invent prior conversations, scope details, or commitments.
2. Write the dollar amount as "USD <NUMBER>" (e.g. "USD 385,757") — NEVER use the "$" symbol with numbers. (Harness bug eats it.)
3. Use the recipient's first name. The signature is auto-appended by the sending script — DO NOT add your own signature, sign-off line, or contact block.
4. Acknowledge the time elapsed (Days awaiting, last reply date) — this is what makes follow-ups concrete and harder to ignore.
5. If days awaiting > 60, be slightly more pointed: ask whether the project is still active, or whether scope/budget shifted.
6. Offer to revise pricing if scope has changed. Don't beg.
7. End with one clear ask. No "let me know your thoughts" mush. Ask: "Is the project still active?" or "Has the timeline shifted?"

Output format: respond with ONLY valid JSON, no markdown fences. Schema:
{{
  "subject": "Follow-Up: <project name> (<BID-NNNN>)",
  "body": "Hi <FirstName>,\\n\\n<body paragraph 1>\\n\\n<body paragraph 2 — final ask as a question>"
}}

CRITICAL: DO NOT include any sign-off line at the end of the body — no "Best,", no "Thanks,", no "Regards,", no "Nursultan". The body ends with the final question. The sending script appends the full signature block (Best, / Nursultan Temirbaev | Manager / Carolina Commercial Finishes / phone / address) automatically. If you add your own sign-off, the email will have a duplicated "Best, Nursultan ... Best, Nursultan Temirbaev". DO NOT include phone, address, email, company name, or sign-off in the body.
"""


def load_bid_from_crm(bid_id: str) -> dict | None:
    """Pull bid row from live Google Sheets Bid Log."""
    try:
        from crm_lib import get_sheet
    except ImportError:
        print("[error] crm_lib not importable", file=sys.stderr)
        return None
    ws = get_sheet("Bid Log")
    for r in ws.get_all_records():
        if r.get("Bid #") == bid_id:
            return r
    return None


def load_postmortem(bid_id: str) -> dict:
    """Read the latest postmortem md for this bid, parse out timeline + counts."""
    matches = list(POSTMORTEM_DIR.glob(f"{bid_id}_*.md"))
    if not matches:
        return {}
    md_path = sorted(matches, key=lambda p: p.stat().st_mtime, reverse=True)[0]
    text = md_path.read_text(encoding="utf-8")
    info = {"file": md_path.name, "text": text}

    # Parse counts
    fu_match = re.search(r"\*\*Follow-ups\s*\((\d+)\)", text)
    info["followup_count"] = int(fu_match.group(1)) if fu_match else 0
    gc_match = re.search(r"\*\*GC replies / inbound\s*\((\d+)\)", text)
    info["gc_reply_count"] = int(gc_match.group(1)) if gc_match else 0
    # Last follow-up date (most recent line in Follow-ups block)
    fu_section = re.search(r"\*\*Follow-ups[^*]+?(?=\*\*GC replies|\Z)", text, re.DOTALL)
    info["last_followup_date"] = ""
    if fu_section:
        dates = re.findall(r"(\d{4}-\d{2}-\d{2})\s+→", fu_section.group(0))
        if dates:
            info["last_followup_date"] = max(dates)
    # Last GC reply date
    gc_section = re.search(r"\*\*GC replies / inbound[\s\S]*?(?=\Z)", text)
    info["last_gc_reply_date"] = ""
    if gc_section:
        dates = re.findall(r"(\d{4}-\d{2}-\d{2})\s+←", gc_section.group(0))
        if dates:
            info["last_gc_reply_date"] = max(dates)
    return info


def days_between(date_str: str) -> int | None:
    """Days from given YYYY-MM-DD to today."""
    if not date_str: return None
    try:
        d = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
        return (datetime.now().date() - d).days
    except Exception:
        return None


def first_name_from(name: str) -> str:
    if not name: return "there"
    return name.strip().split()[0]


def build_context(bid: dict, pm: dict) -> dict:
    """Bundle the concrete facts Gemini can reference in the body."""
    submitted = bid.get("Bid Submitted Date") or ""
    days_awaiting = days_between_iso(submitted)

    context = {
        "bid_id":           bid.get("Bid #", ""),
        "project_name":     bid.get("Project Name", ""),
        "gc":               bid.get("GC / Client", ""),
        "contact_name":     bid.get("Contact Name", "") or "",
        "contact_first":    first_name_from(bid.get("Contact Name", "")),
        "contact_email":    bid.get("Contact Email", "").strip(),
        "amount_text":      f"USD {format_amount(bid.get('Bid Amount ($)', ''))}",
        "submitted_date":   submitted,
        "days_awaiting":    days_awaiting,
        "current_status":   bid.get("Status", ""),
        "followup_count":   pm.get("followup_count", 0),
        "last_followup":    pm.get("last_followup_date", ""),
        "last_gc_reply":    pm.get("last_gc_reply_date", ""),
        "loss_reason":      (bid.get("Loss Reason") or "").strip(),
    }
    return context


def format_amount(raw) -> str:
    """Normalize bid amount to '385,757' (no $ symbol — caller adds 'USD ')."""
    if not raw: return ""
    s = re.sub(r"[^\d.]", "", str(raw))
    if not s: return ""
    try:
        n = float(s)
        return f"{n:,.0f}"
    except ValueError:
        return s


def days_between_iso(date_str: str) -> int | None:
    """Days from a flexible date string to today."""
    if not date_str: return None
    s = str(date_str).strip()
    for fmt in ("%a, %d %b %Y", "%m/%d/%Y", "%Y-%m-%d", "%d %b %Y", "%B %d, %Y"):
        try:
            d = datetime.strptime(s, fmt).date()
            return (datetime.now().date() - d).days
        except ValueError:
            continue
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        try:
            d = datetime(int(m.group(3)), int(m.group(1)), int(m.group(2))).date()
            return (datetime.now().date() - d).days
        except Exception:
            return None
    return None


def gemini_draft(context: dict, email_type: str = "follow-up") -> dict:
    """Call Gemini 2.5 Flash to draft the email. Returns {subject, body}."""
    try:
        import litellm
    except ImportError:
        return {"error": "pip install litellm"}
    if not os.environ.get("GEMINI_API_KEY"):
        return {"error": "GEMINI_API_KEY not set"}

    user_msg = (
        "CONTEXT (use ONLY these facts):\n"
        f"  Bid #: {context['bid_id']}\n"
        f"  Project: {context['project_name']}\n"
        f"  GC: {context['gc']}\n"
        f"  Contact name: {context['contact_name']}\n"
        f"  Contact first name: {context['contact_first']}\n"
        f"  Bid amount: {context['amount_text']}\n"
        f"  Submitted: {context['submitted_date']}\n"
        f"  Days awaiting decision: {context['days_awaiting']}\n"
        f"  Current status: {context['current_status']}\n"
        f"  Prior follow-ups sent: {context['followup_count']}\n"
        f"  Last follow-up: {context['last_followup'] or 'none'}\n"
        f"  Last GC reply: {context['last_gc_reply'] or 'none'}\n"
        f"  Loss reason (if any): {context['loss_reason'] or 'none recorded'}\n\n"
        f"Write the {email_type} email per the system prompt rules. JSON only."
    )

    try:
        r = litellm.completion(
            model="gemini/gemini-2.5-flash",
            max_tokens=1500,
            temperature=0.4,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": DRAFT_SYSTEM_PROMPT.format(
                    email_type=email_type,
                    type_guidance=EMAIL_TYPE_GUIDANCE.get(email_type, EMAIL_TYPE_GUIDANCE["follow-up"]),
                )},
                {"role": "user", "content": user_msg},
            ],
        )
        text = r.choices[0].message.content.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        try:
            out = json.loads(text)
        except json.JSONDecodeError:
            m = re.search(r"\{[\s\S]*\}", text)
            if not m:
                return {"error": "model did not return JSON", "raw": text[:600]}
            try:
                out = json.loads(m.group(0))
            except json.JSONDecodeError as je:
                return {"error": f"JSON parse failed: {je}", "raw": text[:600]}
        # Sanity: scrub any leaked $ amounts
        for key in ("subject", "body"):
            if key in out and isinstance(out[key], str):
                # Replace $NNN,NNN with USD NNN,NNN as defense in depth
                out[key] = re.sub(r"\$\s*(\d{1,3}(?:,\d{3})+)", r"USD \1", out[key])

        # Strip trailing sign-off if Gemini included one anyway (avoids duplicate
        # "Best, Nursultan" when send_email auto-appends the signature block)
        if "body" in out and isinstance(out["body"], str):
            body = out["body"].rstrip()
            # Patterns: "Best,\nNursultan", "Thanks,\nNursultan", "Regards,\nNursultan", or just trailing "Nursultan"
            body = re.sub(
                r"\n+\s*(Best|Thanks|Regards|Sincerely|Cheers)[,!\.]?\s*\n+\s*Nursultan\s*\.?\s*$",
                "", body, flags=re.IGNORECASE
            )
            # Also strip a bare trailing "Nursultan" or "- Nursultan" sign-off line
            body = re.sub(r"\n+\s*[-—]?\s*Nursultan\s*\.?\s*$", "", body, flags=re.IGNORECASE)
            # And any lonely closing word
            body = re.sub(r"\n+\s*(Best|Thanks|Regards|Sincerely|Cheers)[,!\.]?\s*$",
                          "", body, flags=re.IGNORECASE)
            out["body"] = body.rstrip()
        return out
    except Exception as e:
        return {"error": f"Gemini call failed: {e}"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bid", required=True, help="Bid # (e.g. BID-0023)")
    ap.add_argument("--type", default="follow-up",
                    choices=["follow-up", "follow-up-1", "follow-up-2", "follow-up-3",
                             "closeout", "clarification", "thank-you", "revised-proposal",
                             "win-acceptance", "still-awaiting-ack",
                             "pricing-engagement", "loss-feedback-request"],
                    help=("Email type. follow-up-1=initial check-in (day 7). "
                          "follow-up-2=offer-help angle (day 14). "
                          "follow-up-3=offer-revise-scope (day 28). "
                          "closeout=final email before auto-close. "
                          "win-acceptance=thank-you + next-steps when WON."))
    ap.add_argument("--json", action="store_true",
                    help="Output machine-readable JSON only")
    args = ap.parse_args()

    bid = load_bid_from_crm(args.bid)
    if not bid:
        out = {"error": f"Bid {args.bid} not found in CRM Bid Log"}
        print(json.dumps(out, indent=2))
        sys.exit(1)

    pm = load_postmortem(args.bid)
    context = build_context(bid, pm)

    if not context["contact_email"]:
        out = {"error": f"No Contact Email in CRM row for {args.bid}",
               "hint": "Add it to column H of the Bid Log Sheet, or look at the postmortem GC replies."}
        print(json.dumps(out, indent=2))
        sys.exit(1)

    draft = gemini_draft(context, email_type=args.type)
    if "error" in draft:
        print(json.dumps(draft, indent=2))
        sys.exit(1)

    out = {
        "to": context["contact_email"],
        "subject": draft.get("subject", "").strip(),
        "body": draft.get("body", "").strip(),
        "context_used": {
            "bid_id": context["bid_id"],
            "project": context["project_name"],
            "amount": context["amount_text"],
            "days_awaiting": context["days_awaiting"],
            "followups": context["followup_count"],
            "last_gc_reply": context["last_gc_reply"],
        },
        "model": "gemini/gemini-2.5-flash",
        "next_step": (
            f"Show this DRAFT to the user, wait for explicit 'send'. Then run: "
            f"python scripts/send_email.py --to \"{context['contact_email']}\" "
            f"--subject \"{draft.get('subject','')}\" --body \"<paste body>\""
        ),
    }
    if args.json:
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        print("📧 DRAFT (not yet sent — paid Gemini 2.5 Flash)")
        print(f"To: {out['to']}")
        print(f"Subject: {out['subject']}")
        print()
        print(out["body"])
        print()
        print("--- Context used ---")
        for k, v in out["context_used"].items():
            print(f"  {k}: {v}")
        print()
        print("Next step: show user the draft, get explicit approval, then send_email.py")


if __name__ == "__main__":
    main()
