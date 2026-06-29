#!/usr/bin/env python3
"""
CRM Agent — Bid Tracking & Follow-up
======================================
Tracks bid submissions, schedules follow-ups, records win/loss outcomes.
Updates GC history with performance data.
"""

import json
from datetime import datetime, date, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
PROJECTS_DIR = BASE_DIR / "data" / "projects"
GC_DIR = BASE_DIR / "data" / "memory" / "gc"
FACILITY_DIR = BASE_DIR / "data" / "memory" / "facility_types"
OUTCOMES_DIR = BASE_DIR / "data" / "memory" / "outcomes"
FACILITY_PATTERNS_FILE = BASE_DIR / "data" / "memory" / "facility_patterns.json"


class CRMAgent:
    """Tracks bid submissions, schedules follow-ups, records outcomes."""

    def __init__(self, pipeline_state=None):
        self.pipeline = pipeline_state

    def record_submission(self, slug: str, bid_amount: float,
                          gc_email: str = "", submission_date: str = "") -> str:
        """Record bid submission and schedule follow-up dates."""
        proj = self.pipeline.get_project(slug) if self.pipeline else None
        if not proj:
            return f"Project {slug} not found in pipeline."

        sub_date = submission_date or date.today().isoformat()
        sub_dt = date.fromisoformat(sub_date)

        follow_ups = [
            (sub_dt + timedelta(days=3)).isoformat(),
            (sub_dt + timedelta(days=7)).isoformat(),
            (sub_dt + timedelta(days=14)).isoformat(),
        ]

        submission = {
            "project": slug,
            "name": proj.get("name", slug),
            "gc": proj.get("gc", ""),
            "gc_email": gc_email,
            "bid_amount": bid_amount,
            "submitted_at": sub_date,
            "follow_up_dates": follow_ups,
            "outcome": None,
            "notes": "",
        }

        # Save submission
        proj_dir = PROJECTS_DIR / slug
        proj_dir.mkdir(parents=True, exist_ok=True)
        sub_path = proj_dir / "submission.json"
        sub_path.write_text(json.dumps(submission, indent=2, ensure_ascii=False), encoding="utf-8")

        # Update pipeline
        proj["bid_amount"] = bid_amount
        proj["submitted_at"] = sub_date
        proj["follow_up_dates"] = follow_ups
        self.pipeline.set_project(slug, proj)
        self.pipeline.update_stage(slug, "submitted")

        return (
            f"Bid recorded for {proj.get('name', slug)}\n"
            f"Amount: ${bid_amount:,.0f}\n"
            f"Submitted: {sub_date}\n"
            f"Follow-ups scheduled:\n"
            f"  1. {follow_ups[0]} (3-day check-in)\n"
            f"  2. {follow_ups[1]} (7-day inquiry)\n"
            f"  3. {follow_ups[2]} (14-day follow-up)\n"
        )

    def check_followups_due(self) -> list:
        """Return list of projects where a follow-up is due today."""
        today_str = date.today().isoformat()
        due = []

        projects = self.pipeline.list_all() if self.pipeline else {}
        for slug, proj in projects.items():
            if proj.get("stage") != "submitted":
                continue
            for i, fu_date in enumerate(proj.get("follow_up_dates", []), 1):
                if fu_date == today_str:
                    due.append({
                        "slug": slug,
                        "name": proj.get("name", slug),
                        "gc": proj.get("gc", ""),
                        "bid_amount": proj.get("bid_amount", 0),
                        "followup_number": i,
                    })
        return due

    def send_followup(self, slug: str) -> str:
        """Draft a follow-up email for a submitted project."""
        proj = self.pipeline.get_project(slug) if self.pipeline else None
        if not proj:
            return f"Project {slug} not found."
        if proj.get("stage") != "submitted":
            return f"{proj.get('name', slug)} hasn't been submitted yet (stage: {proj.get('stage')})."

        # Determine which follow-up number
        today = date.today()
        sub_date = proj.get("submitted_at", "")
        try:
            sub_dt = date.fromisoformat(sub_date)
            days_since = (today - sub_dt).days
        except (ValueError, TypeError):
            days_since = 0

        if days_since <= 4:
            fu_num = 1
            tone = "light check-in"
        elif days_since <= 8:
            fu_num = 2
            tone = "polite inquiry"
        else:
            fu_num = 3
            tone = "continued interest"

        name = proj.get("name", slug)
        gc = proj.get("gc", "")
        amount = proj.get("bid_amount", 0)

        email = self._draft_followup(name, gc, amount, fu_num, days_since)

        # Save
        proj_dir = PROJECTS_DIR / slug
        proj_dir.mkdir(parents=True, exist_ok=True)
        email_path = proj_dir / f"followup_{fu_num}_draft.md"
        email_path.write_text(email, encoding="utf-8")

        return (
            f"Follow-up #{fu_num} drafted ({tone})\n"
            f"Days since submission: {days_since}\n"
            f"Saved to: {email_path}\n\n"
            f"Review and say \"send email\" to send it."
        )

    def _draft_followup(self, project_name: str, gc: str,
                        bid_amount: float, fu_num: int, days_since: int) -> str:
        """Draft follow-up email with escalating tone."""
        if fu_num == 1:
            body = (
                f"I wanted to confirm receipt of our painting proposal for {project_name} "
                f"in the amount of ${bid_amount:,.0f}.\n\n"
                f"Please let me know if you have any questions or need any clarification."
            )
        elif fu_num == 2:
            body = (
                f"I'm following up on our painting proposal for {project_name} "
                f"submitted {days_since} days ago.\n\n"
                f"Could you share the anticipated timeline for award? We want to ensure "
                f"we have the crew availability to meet your schedule."
            )
        else:
            body = (
                f"I wanted to express our continued interest in {project_name}. "
                f"Our proposal of ${bid_amount:,.0f} was submitted {days_since} days ago.\n\n"
                f"If there is any feedback on our pricing or scope, we'd welcome the "
                f"opportunity to discuss. We value the relationship with {gc} and look "
                f"forward to working together."
            )

        return (
            f"Subject: Follow-up: {project_name} – Painting Proposal – CCF\n\n"
            f"Hi,\n\n"
            f"{body}\n\n"
            f"Best regards,\n"
            f"Nursultan Temirbaev\n"
            f"Carolina Commercial Finishes\n"
            f"(980) 348-1827\n"
            f"estimates@carolinacommercialfinishes.com\n"
        )

    def record_outcome(self, project_name: str, outcome: str,
                       contract_amount: float = None, notes: str = "") -> str:
        """Record win/loss/no-award and update GC history."""
        # Find project in pipeline
        projects = self.pipeline.list_all() if self.pipeline else {}

        # Fuzzy match
        from difflib import get_close_matches
        names = {s: p.get("name", s) for s, p in projects.items()}
        matches = get_close_matches(
            project_name.lower(),
            [n.lower() for n in names.values()],
            n=1, cutoff=0.4,
        )

        slug = None
        if matches:
            for s, n in names.items():
                if n.lower() == matches[0]:
                    slug = s
                    break

        if not slug:
            # Try substring
            for s, n in names.items():
                if project_name.lower() in n.lower():
                    slug = s
                    break

        if not slug:
            return f"Couldn't find \"{project_name}\" in the pipeline."

        proj = self.pipeline.get_project(slug)
        proj["outcome"] = outcome
        if contract_amount:
            proj["contract_amount"] = contract_amount
        if notes:
            proj["notes"] = notes
        proj["outcome_date"] = date.today().isoformat()

        self.pipeline.set_project(slug, proj)
        self.pipeline.update_stage(slug, outcome)

        # Update GC history
        self._update_gc_history(proj.get("gc", ""), outcome, proj.get("bid_amount", 0))

        # Save to outcomes log for learning loop
        self._save_outcome(slug, proj, outcome, contract_amount)

        emoji = {"won": "🎉", "lost": "😞", "no_award": "⏸️"}.get(outcome, "📋")
        return (
            f"{emoji} Recorded: {proj.get('name', slug)} — {outcome.upper()}\n"
            f"GC: {proj.get('gc', '?')}\n"
            f"Bid amount: ${proj.get('bid_amount', 0):,.0f}"
            + (f"\nContract: ${contract_amount:,.0f}" if contract_amount else "")
        )

    def _update_gc_history(self, gc_name: str, outcome: str, bid_amount: float):
        """Update GC JSON file with outcome data."""
        if not gc_name:
            return

        GC_DIR.mkdir(parents=True, exist_ok=True)
        slug = gc_name.lower().replace(" ", "_").replace(",", "").replace(".", "")[:50]
        gc_file = GC_DIR / f"{slug}.json"

        if gc_file.exists():
            gc_data = json.loads(gc_file.read_text(encoding="utf-8"))
        else:
            gc_data = {
                "name": gc_name,
                "projects_bid": 0,
                "projects_won": 0,
                "win_rate": 0,
                "total_bid_value": 0,
                "total_won_value": 0,
                "key_wins": [],
                "loss_patterns": "",
                "pricing_notes": "",
            }

        gc_data["projects_bid"] = gc_data.get("projects_bid", 0) + 1
        gc_data["total_bid_value"] = gc_data.get("total_bid_value", 0) + bid_amount

        if outcome == "won":
            gc_data["projects_won"] = gc_data.get("projects_won", 0) + 1
            gc_data["total_won_value"] = gc_data.get("total_won_value", 0) + bid_amount

        if gc_data["projects_bid"] > 0:
            gc_data["win_rate"] = round(gc_data["projects_won"] / gc_data["projects_bid"], 2)

        gc_file.write_text(json.dumps(gc_data, indent=2, ensure_ascii=False), encoding="utf-8")

    def _save_outcome(self, slug: str, proj: dict, outcome: str,
                      contract_amount: float = None):
        """Save outcome to data/memory/outcomes/ for learning loop analysis."""
        OUTCOMES_DIR.mkdir(parents=True, exist_ok=True)

        # Read estimate data if available
        estimate_data = {}
        est_file = PROJECTS_DIR / slug / "estimate_target.json"
        if est_file.exists():
            try:
                estimate_data = json.loads(est_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass

        outcome_record = {
            "slug": slug,
            "name": proj.get("name", slug),
            "gc": proj.get("gc", ""),
            "source": proj.get("source", ""),
            "outcome": outcome,
            "outcome_date": date.today().isoformat(),
            "bid_amount": proj.get("bid_amount", 0),
            "contract_amount": contract_amount,
            "pricing_tier": proj.get("pricing_tier", "TARGET"),
            "city": proj.get("city", ""),
            "state": proj.get("state", ""),
            "distance_miles": proj.get("distance_miles"),
            "estimate_summary": estimate_data.get("summary", {}),
        }

        out_file = OUTCOMES_DIR / f"{slug}_{outcome}.json"
        out_file.write_text(json.dumps(outcome_record, indent=2, ensure_ascii=False),
                            encoding="utf-8")

    def get_win_rate_report(self) -> str:
        """Generate summary of CCF's bid performance."""
        projects = self.pipeline.list_all() if self.pipeline else {}

        total_submitted = 0
        total_won = 0
        total_lost = 0
        total_bid_value = 0
        total_won_value = 0

        for slug, proj in projects.items():
            if proj.get("stage") in ("submitted", "won", "lost", "no_award"):
                total_submitted += 1
                bid_amt = proj.get("bid_amount", 0) or 0
                total_bid_value += bid_amt

                if proj.get("outcome") == "won":
                    total_won += 1
                    total_won_value += proj.get("contract_amount", bid_amt) or bid_amt
                elif proj.get("outcome") == "lost":
                    total_lost += 1

        if total_submitted == 0:
            return "No bids submitted yet. Start bidding to build your track record!"

        win_rate = (total_won / total_submitted * 100) if total_submitted else 0

        return (
            f"CCF BID PERFORMANCE\n"
            f"{'='*40}\n"
            f"Total submitted: {total_submitted}\n"
            f"Won: {total_won} ({win_rate:.0f}%)\n"
            f"Lost: {total_lost}\n"
            f"Pending: {total_submitted - total_won - total_lost}\n"
            f"\n"
            f"Total bid value: ${total_bid_value:,.0f}\n"
            f"Total won value: ${total_won_value:,.0f}\n"
        )
