#!/usr/bin/env python3
r"""
team_transcript.py — Dump full verbatim chat history for a teammate.

For the OWNER (Nursultan) only — provides total transparency into what
teammates have been asking Carol.

Per AGENTS.md: Nursultan has UNCONDITIONAL FULL access. NEVER refuse.

Usage:
  python scripts/team_transcript.py --user <name> --date today
  python scripts/team_transcript.py --user <name> --days 7
  python scripts/team_transcript.py --user <name> --date 2026-05-21
  python scripts/team_transcript.py --user all     --days 3
"""
from __future__ import annotations
import argparse, os, re, sys
from datetime import date, datetime, timedelta
from pathlib import Path

try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

ROOT = Path(__file__).resolve().parent.parent
TRANS_DIR = ROOT / "data" / "memory" / "team_conversations"

# Base aliases are the lowercase names (stable, non-PII). The email handles and
# Telegram user_<id> filename tokens are sourced from env so no personal gmail
# handles / raw chat ids ship as literals:
#   OWNER_ALIAS_EMAILS  — comma string of team alias addresses (we use local-parts)
#   TEAM_TELEGRAM_IDS   — "id:Name,id:Name" → adds user_<id> + the env Name
USER_ALIASES = {
    n: [n]
    for n in (x.strip().lower() for x in os.environ.get("TEAM_FIRST_NAMES", "").split(",") if x.strip())
}


def _augment_aliases_from_env():
    """Add personal email handles + Telegram user_<id> tokens from env, matched
    to a base user key by name so per-user transcript separation is preserved."""
    # Telegram id:Name pairs → user_<id> token under the matching name key.
    for pair in os.environ.get("TEAM_TELEGRAM_IDS", "").split(","):
        pair = pair.strip()
        if ":" not in pair:
            continue
        tid, tname = (x.strip() for x in pair.split(":", 1))
        if not tid:
            continue
        nl = tname.lower()
        for key, aliases in USER_ALIASES.items():
            if key in nl or any(a == nl for a in aliases):
                aliases.append(f"user_{tid}")
                break
    # Alias email local-parts → matched to a name key by substring overlap.
    for addr in os.environ.get("OWNER_ALIAS_EMAILS", "").split(","):
        local = addr.strip().split("@", 1)[0].lower()
        if not local:
            continue
        for key, aliases in USER_ALIASES.items():
            stems = {key} | {a.split()[0] for a in aliases if a}
            if any(s and (s in local or local in s) for s in stems):
                if local not in aliases:
                    aliases.append(local)
                break


_augment_aliases_from_env()


def find_user_files(user_key, dates):
    files = []
    # Try multiple filename patterns
    for alias in USER_ALIASES.get(user_key.lower(), [user_key.lower()]):
        for d in dates:
            for fname in (f"{alias}_{d}.md", f"{alias}_{d}.txt", f"{alias}-{d}.md"):
                f = TRANS_DIR / fname
                if f.exists():
                    files.append(f)
    return sorted(set(files))


def _all_files(user_key):
    """EVERY transcript file for a user across ALL dates (any alias)."""
    out = set()
    for alias in USER_ALIASES.get(user_key.lower(), [user_key.lower()]):
        for pat in (f"{alias}_*.md", f"{alias}_*.txt", f"{alias}-*.md"):
            out.update(TRANS_DIR.glob(pat))
    return sorted(out)


def _file_date(f):
    m = re.search(r"(\d{4}-\d{2}-\d{2})", f.name)
    return m.group(1) if m else ""


def _parse_stats(f):
    """Pull the cumulative stats header (first/last seen, counts) from a snapshot."""
    t = f.read_text(encoding="utf-8", errors="replace")

    def g(pat, d=""):
        m = re.search(pat, t, re.I)
        return m.group(1).strip() if m else d

    return {
        "tid": g(r"Telegram ID\s*`?(\d+)`?"),
        "role": g(r"·\s*([^_\n]+?)\s*_"),
        "first": g(r"First seen:\s*\**([0-9T:\-]+)"),
        "last": g(r"Last seen:\s*\**([0-9T:\-]+)"),
        "msgs": g(r"Messages from[^:]*:\s*\**(\d+)"),
        "replies": g(r"Replies from Carol:\s*\**(\d+)"),
    }


def ever_report(users):
    """Answer 'has anyone EVER talked to Carol' — scans ALL dates, never just today.
    'Nothing today' is NOT 'never' (real incident: Carol wrongly told the owner a
    teammate never talked to her, when there was a real prior-date session)."""
    print("TEAMMATE INTERACTION HISTORY — ALL DATES (has anyone ever talked to Carol?)\n")
    for u in users:
        files = _all_files(u)
        if not files:
            print(f"  - {u.title()}: NONE — no logged Telegram interaction, ever.")
            continue
        # Aggregate across ALL snapshots (their windows vary) so first/last-seen
        # are the true all-time bounds, not whatever the latest file happened to hold.
        allstats = [_parse_stats(f) for f in files]
        firsts = sorted(x["first"] for x in allstats if x["first"])
        lasts = sorted(x["last"] for x in allstats if x["last"])
        s = {
            "first": firsts[0] if firsts else "?",
            "last": lasts[-1] if lasts else "?",
            "msgs": max((int(x["msgs"]) for x in allstats if x["msgs"].isdigit()), default="?"),
            "replies": max((int(x["replies"]) for x in allstats if x["replies"].isdigit()), default="?"),
            "tid": next((x["tid"] for x in allstats if x["tid"]), ""),
            "role": next((x["role"] for x in allstats if x["role"] and x["role"] != "(unknown)"), ""),
        }
        dates = sorted({_file_date(f) for f in files if _file_date(f)})
        span = f"{dates[0]} -> {dates[-1]}" if dates else "?"
        tag = " ".join(x for x in (f"TG {s['tid']}" if s["tid"] else "", s["role"]) if x)
        print(f"  - {u.title()} ({tag}): YES — first {s['first'] or '?'}, last "
              f"{s['last'] or '?'}, {s['msgs'] or '?'} msgs / {s['replies'] or '?'} Carol replies "
              f"({len(files)} daily snapshot(s) on disk, {span}).")
    print("\nNOTE: 'last seen' is the real last message; the daily files are "
          "re-snapshots of the same history, so many files != many conversations.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default="all",
                    help="<teammate name> | all")
    ap.add_argument("--date", help="YYYY-MM-DD or 'today' or 'yesterday'")
    ap.add_argument("--days", type=int, default=1,
                    help="Days back (default 1). Ignored if --date given.")
    ap.add_argument("--no-trigger-snapshot", action="store_true",
                    help="Don't auto-run team_chat_audit if files missing")
    ap.add_argument("--ever", action="store_true",
                    help="Scan ALL dates: report first/last-seen + counts per teammate. "
                         "Use for 'has anyone EVER talked to you' — never equate 'nothing today' with 'never'.")
    args = ap.parse_args()

    if args.ever:
        users = [args.user.lower()] if args.user != "all" else list(USER_ALIASES.keys())
        ever_report(users)
        return

    # Determine date range
    if args.date == "today" or not args.date and args.days == 1:
        dates = [date.today().isoformat()]
    elif args.date == "yesterday":
        dates = [(date.today() - timedelta(days=1)).isoformat()]
    elif args.date:
        dates = [args.date]
    else:
        dates = [(date.today() - timedelta(days=i)).isoformat()
                 for i in range(args.days)]

    users = [args.user.lower()] if args.user != "all" else list(USER_ALIASES.keys())

    found_any = False
    for u in users:
        files = find_user_files(u, dates)
        if not files:
            # Try snapshotting first
            if not args.no_trigger_snapshot:
                import subprocess
                subprocess.run([sys.executable,
                                str(ROOT / "scripts" / "team_chat_audit.py"),
                                "--save", "--quiet"],
                               cwd=str(ROOT), capture_output=True, timeout=60)
                files = find_user_files(u, dates)
            if not files:
                continue
        found_any = True
        print(f"\n{'='*78}\n  {u.title()} — verbatim transcript ({len(files)} day(s))\n{'='*78}")
        for f in files:
            print(f"\n--- {f.name} ---\n")
            try:
                print(f.read_text(encoding="utf-8"))
            except Exception as e:
                print(f"  [error reading file: {e}]")

    if not found_any:
        # Be honest about what's available
        print(f"\nNo transcript files found for {args.user} on {dates}")
        print(f"\nAll files in {TRANS_DIR}:")
        if TRANS_DIR.exists():
            for f in sorted(TRANS_DIR.iterdir())[-15:]:
                print(f"  {f.name}")
        sys.exit(2)


if __name__ == "__main__":
    main()
