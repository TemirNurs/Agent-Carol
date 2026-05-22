#!/usr/bin/env python3
"""
run_carol.py — Main entry point for Carol.
OpenClaw calls this with the incoming message as CLI argument.

Usage:
  python scripts/run_carol.py --message "show me today's bids" --from "+19803481827"
  python scripts/run_carol.py --daily-briefing
  python scripts/run_carol.py --check-followups
  python scripts/run_carol.py --scrape
  python scripts/run_carol.py --status
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Add project root to path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from carol_core import CarolCore

SCRIPTS_DIR = ROOT / "scripts"


def send_email_report():
    """Send the HTML email bid report."""
    script = SCRIPTS_DIR / "email_bid_report.py"
    if script.exists():
        result = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True, text=True, timeout=30,
        )
        return result.returncode == 0
    return False


def main():
    parser = argparse.ArgumentParser(description="Carol — Main Entry Point")
    parser.add_argument("--message", "-m", help="Incoming message from owner")
    parser.add_argument("--from", dest="from_number", default="+19803481827",
                        help="Sender phone number")
    parser.add_argument("--daily-briefing", action="store_true",
                        help="Run daily briefing (scrape + email + briefing text)")
    parser.add_argument("--check-followups", action="store_true",
                        help="Check and send due follow-ups")
    parser.add_argument("--scrape", action="store_true",
                        help="Run CC + BC scrapers")
    parser.add_argument("--status", action="store_true",
                        help="Show pipeline status")
    parser.add_argument("--daemon", action="store_true",
                        help="Start Carol daemon (persistent event loop)")
    args = parser.parse_args()

    if args.daemon:
        import importlib.util
        spec = importlib.util.spec_from_file_location("carol_daemon", str(ROOT / "carol_daemon.py"))
        daemon_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(daemon_mod)
        daemon_mod.main()
        return

    carol = CarolCore()

    if args.daily_briefing:
        # Step 1: Scrape both platforms
        print("=== SCRAPING ===")
        scrape_result = carol.scout.run_scrapers()
        print(scrape_result)

        # Step 2: Generate briefing
        print("\n=== DAILY BRIEFING ===")
        briefing = carol.scout.get_briefing(days_ahead=0)
        print(briefing)

        # Step 3: Send email report
        print("\n=== EMAIL REPORT ===")
        if send_email_report():
            print("Email report sent successfully.")
        else:
            print("Email report failed.")

        # Step 4: Check follow-ups
        due_followups = carol.crm.check_followups_due()
        if due_followups:
            print(f"\n=== FOLLOW-UPS DUE: {len(due_followups)} ===")
            for fu in due_followups:
                print(f"  {fu['name']} — GC: {fu['gc']} — Follow-up #{fu['followup_number']}")

        # Output briefing for WhatsApp/Telegram
        print(f"\n__RESULT__:{json.dumps({'briefing': briefing, 'followups': len(due_followups) if due_followups else 0})}")

    elif args.check_followups:
        due = carol.crm.check_followups_due()
        if due:
            print(f"Follow-ups due today: {len(due)}")
            for fu in due:
                print(f"  {fu['name']} — {fu['gc']} — Follow-up #{fu['followup_number']}")
                draft = carol.crm.send_followup(fu['slug'])
                print(f"  {draft}")
        else:
            print("No follow-ups due today.")

    elif args.scrape:
        result = carol.scout.run_scrapers()
        print(result)

    elif args.status:
        status = carol.get_all_statuses()
        print(status)

    elif args.message:
        response = carol.handle_message(args.message, args.from_number)
        print(response)

    else:
        # Interactive mode
        print("Carol — Interactive Mode (type 'quit' to exit)")
        print("=" * 50)
        while True:
            try:
                msg = input("\nYou: ").strip()
                if msg.lower() in ("quit", "exit", "q"):
                    break
                response = carol.handle_message(msg)
                print(f"\nCarol: {response}")
            except (KeyboardInterrupt, EOFError):
                break


if __name__ == "__main__":
    main()
