#!/usr/bin/env python3
"""
Carol Core — The Coordinator Agent
===================================
All owner communication flows through here.
Reads project state, delegates to subagents, returns formatted responses.

Carol Core is the ONLY agent that talks to the owner.
Subagents (Scout, Estimator, Proposal, CRM) are Python classes she orchestrates.
"""

import json
import os
import re
import sys
from datetime import datetime, date, timedelta
from difflib import get_close_matches
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from agents.scout_agent import ScoutAgent
from agents.estimator_agent import EstimatorAgent
from agents.proposal_agent import ProposalAgent
from agents.crm_agent import CRMAgent

# Paths
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
MEMORY_DIR = DATA_DIR / "memory"
PROJECTS_DIR = DATA_DIR / "projects"
PIPELINE_FILE = MEMORY_DIR / "pipeline.json"
BIDS_FILE = MEMORY_DIR / "active_bids.json"

# Owner info
OWNER_NUMBER = "+19803481827"
OWNER_EMAIL = "Nurs.mllrder@gmail.com"
CCF_EMAIL = "estimates@carolinacommercialfinishes.com"


class PipelineState:
    """Reads/writes pipeline.json — the central state tracker for all projects."""

    def __init__(self, path=PIPELINE_FILE):
        self.path = Path(path)
        self._ensure_exists()

    def _ensure_exists(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.save({"projects": {}})

    def load(self) -> dict:
        """Always read fresh from disk."""
        return json.loads(self.path.read_text(encoding="utf-8"))

    def save(self, pipeline: dict):
        self.path.write_text(json.dumps(pipeline, indent=2, ensure_ascii=False), encoding="utf-8")

    def get_project(self, slug: str) -> dict | None:
        pipeline = self.load()
        return pipeline["projects"].get(slug)

    def set_project(self, slug: str, data: dict):
        pipeline = self.load()
        pipeline["projects"][slug] = data
        self.save(pipeline)

    def update_stage(self, slug: str, stage: str):
        pipeline = self.load()
        proj = pipeline["projects"].get(slug)
        if not proj:
            return
        prev_stage = proj.get("stage", "")
        if prev_stage and prev_stage not in proj.get("stages_completed", []):
            proj.setdefault("stages_completed", []).append(prev_stage)
        proj["stage"] = stage
        proj["updated_at"] = datetime.now().isoformat()
        pipeline["projects"][slug] = proj
        self.save(pipeline)

    def create_project(self, slug: str, name: str, bid: dict) -> dict:
        """Create a new project entry from a bid dict."""
        project = {
            "name": name,
            "gc": bid.get("gc", "Unknown"),
            "source": "CC" if bid.get("source") == "constructconnect" else "BC",
            "due_date": bid.get("due_date", ""),
            "distance_miles": bid.get("distance_miles"),
            "city": bid.get("city", ""),
            "state": bid.get("state", ""),
            "stage": "scouted",
            "stages_completed": [],
            "togal_set_id": None,
            "pricing_tier": "TARGET",
            "bid_amount": None,
            "submitted_at": None,
            "outcome": None,
            "follow_up_dates": [],
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "notes": "",
        }
        self.set_project(slug, project)
        return project

    def list_active(self) -> dict:
        """Return all projects that aren't won/lost/no_award."""
        pipeline = self.load()
        return {
            slug: proj
            for slug, proj in pipeline["projects"].items()
            if proj.get("stage") not in ("won", "lost", "no_award")
        }

    def list_all(self) -> dict:
        return self.load()["projects"]


class CarolCore:
    """
    The coordinator agent. All owner communication flows through here.
    Never calls CC, BC, or Togal directly — delegates to subagents.
    """

    # Valid stages in pipeline order
    STAGES = [
        "scouted", "docs_downloading", "docs_ready",
        "sow_building", "sow_ready",
        "takeoff_planning", "takeoff_uploading", "takeoff_done",
        "estimating", "estimate_ready",
        "proposal_drafting", "proposal_ready",
        "submitted", "won", "lost", "no_award",
    ]

    def __init__(self):
        self.pipeline = PipelineState()
        self.scout = ScoutAgent(self.pipeline)
        self.estimator = EstimatorAgent(self.pipeline)
        self.proposal = ProposalAgent(self.pipeline)
        self.crm = CRMAgent(self.pipeline)

    # ------------------------------------------------------------------
    # Message routing
    # ------------------------------------------------------------------

    def handle_message(self, message: str, from_number: str = "") -> str:
        """
        Main entry point. Parse owner intent, delegate, return response.
        """
        msg = message.strip()
        msg_lower = msg.lower()

        # --- Bid briefing ---
        if any(k in msg_lower for k in [
            "what bids", "show bids", "today's bids", "what do we have",
            "daily brief", "briefing", "what's due", "whats due",
            "bids for today", "bids for tomorrow",
            "bids this week", "this week",
        ]):
            if "tomorrow" in msg_lower:
                return self.scout.get_briefing(days_ahead=1)
            if "this week" in msg_lower or "week" in msg_lower:
                return self.scout.get_briefing(days_ahead=7)
            return self.scout.get_briefing(days_ahead=0)

        # --- Start bidding a project ---
        bid_match = re.match(
            r"(?:bid|let'?s bid|start|estimate|bid on)\s+(.+)",
            msg_lower,
        )
        if bid_match:
            project_name = bid_match.group(1).strip().strip('"\'')
            return self._start_bid_pipeline(project_name)

        # --- Continue pipeline for a project ---
        continue_match = re.match(
            r"(?:continue|next step|proceed|keep going|go ahead)\s+(?:with\s+|on\s+)?(.+)",
            msg_lower,
        )
        if continue_match:
            project_name = continue_match.group(1).strip().strip('"\'')
            return self._continue_pipeline(project_name)

        # --- Skip / pass on a project ---
        skip_match = re.match(
            r"(?:skip|pass|pass on|decline|no bid)\s+(.+)",
            msg_lower,
        )
        if skip_match:
            project_name = skip_match.group(1).strip().strip('"\'')
            return self.scout.mark_bid_decision(project_name, "SKIP")

        # --- Project status ---
        if msg_lower in ("status", "what's cooking", "whats cooking", "pipeline", "show pipeline"):
            return self.get_all_statuses()

        status_match = re.match(
            r"(?:status|status on|update on|what's happening with|whats happening with)\s+(.+)",
            msg_lower,
        )
        if status_match:
            project_name = status_match.group(1).strip().strip('"\'')
            return self.get_project_status(project_name)

        # --- Get estimate ---
        est_match = re.match(
            r"(?:what'?s the estimate|estimate for|show estimate|get estimate)\s+(?:for\s+)?(.+)",
            msg_lower,
        )
        if est_match:
            project_name = est_match.group(1).strip().strip('"\'')
            return self._get_estimate(project_name)

        # --- Draft / send proposal ---
        prop_match = re.match(
            r"(?:draft proposal|send proposal|proposal for|write proposal)\s+(?:for\s+)?(.+)",
            msg_lower,
        )
        if prop_match:
            project_name = prop_match.group(1).strip().strip('"\'')
            return self._draft_proposal(project_name)

        # --- Follow up ---
        follow_match = re.match(
            r"(?:follow up|followup|follow-up)\s+(?:on\s+)?(.+)",
            msg_lower,
        )
        if follow_match:
            project_name = follow_match.group(1).strip().strip('"\'')
            return self._send_followup(project_name)

        # --- Record outcome ---
        won_match = re.match(r"(?:we won|won)\s+(.+)", msg_lower)
        if won_match:
            project_name = won_match.group(1).strip().strip('"\'')
            return self.crm.record_outcome(project_name, "won")

        lost_match = re.match(r"(?:we lost|lost)\s+(.+)", msg_lower)
        if lost_match:
            project_name = lost_match.group(1).strip().strip('"\'')
            return self.crm.record_outcome(project_name, "lost")

        # --- List all projects ---
        if msg_lower in ("list projects", "all projects", "show projects"):
            return self._list_all_projects()

        # --- Which is largest / closest / best / soonest / by GC / by type ---
        if any(k in msg_lower for k in [
            "largest", "biggest", "closest", "nearest",
            "best bid", "top bid", "best score", "top score",
            "soonest", "urgent", "next due",
            "by gc", "by contractor", "group by gc",
            "by type", "by facility", "by category",
        ]) or msg_lower.strip() in ("by gc", "by type", "by facility", "soonest due"):
            return self.scout.get_ranked_bids(msg_lower)

        # --- Check email for bids ---
        if any(k in msg_lower for k in ["check email", "email bids", "inbox", "check gmail"]):
            return self.scout.check_email_bids()

        # --- Email report ---
        if any(k in msg_lower for k in ["send email", "email report", "send report", "email me"]):
            return self._send_email_report()

        # --- Scrape / refresh ---
        if any(k in msg_lower for k in ["scrape", "refresh bids", "update bids", "rescrape"]):
            return self.scout.run_scrapers()

        # --- Dedup ---
        if any(k in msg_lower for k in ["dedup", "deduplicate", "remove duplicates"]):
            removed = self.scout.dedup_bids()
            return f"Deduplication complete. Removed {removed} duplicates."

        # --- Help ---
        if msg_lower in ("help", "commands", "what can you do"):
            return self._help_text()

        # --- Default: try to be helpful ---
        return (
            "I didn't catch that. Here's what I can do:\n\n"
            + self._help_text()
        )

    # ------------------------------------------------------------------
    # Pipeline actions
    # ------------------------------------------------------------------

    def _start_bid_pipeline(self, project_name: str) -> str:
        """Start the full bid pipeline for a project."""
        bid = self._find_bid(project_name)
        if not bid:
            return f"Couldn't find a bid matching \"{project_name}\" in active_bids.json."

        slug = self._make_slug(bid["project_name"])
        source_tag = "(CC)" if bid.get("source") == "constructconnect" else "(BC)"

        # Check if already in pipeline
        existing = self.pipeline.get_project(slug)
        if existing:
            stage = existing["stage"]
            return (
                f"{bid['project_name']} {source_tag} is already in the pipeline.\n"
                f"Current stage: {stage}\n"
                f"Use \"status {bid['project_name']}\" for details."
            )

        # Create project in pipeline
        project = self.pipeline.create_project(slug, bid["project_name"], bid)

        # Create project directory
        proj_dir = PROJECTS_DIR / slug
        proj_dir.mkdir(parents=True, exist_ok=True)
        (proj_dir / "documents").mkdir(exist_ok=True)

        # Save project state
        state_file = proj_dir / "state.json"
        state_file.write_text(json.dumps(project, indent=2, ensure_ascii=False), encoding="utf-8")

        dist = bid.get("distance_miles")
        dist_str = f"{dist:.0f} mi" if dist else "? mi"

        return (
            f"Starting bid pipeline for {bid['project_name']} {source_tag}\n\n"
            f"GC: {bid.get('gc', 'Unknown')}\n"
            f"Location: {bid.get('city', '')}, {bid.get('state', '')[:2]} ({dist_str})\n"
            f"Due: {bid.get('due_date', 'TBD')}\n"
            f"Pricing tier: TARGET\n\n"
            f"Stage: scouted -> docs_downloading\n"
            f"Next step: Download bid documents.\n"
            f"Running document fetch now..."
        )

    def _continue_pipeline(self, project_name: str) -> str:
        """Continue the estimation pipeline from wherever it left off."""
        slug = self._find_project_slug(project_name)
        if not slug:
            return f"No project matching \"{project_name}\" found in the pipeline."
        return self.estimator.continue_pipeline(slug)

    def _get_estimate(self, project_name: str) -> str:
        slug = self._find_project_slug(project_name)
        if not slug:
            return f"No project matching \"{project_name}\" found in the pipeline."
        return self.estimator.get_estimate(slug)

    def _draft_proposal(self, project_name: str) -> str:
        slug = self._find_project_slug(project_name)
        if not slug:
            return f"No project matching \"{project_name}\" found in the pipeline."
        proj = self.pipeline.get_project(slug)
        if proj and proj["stage"] not in ("estimate_ready", "proposal_drafting", "proposal_ready"):
            return (
                f"{proj['name']} is at stage \"{proj['stage']}\".\n"
                f"Need to complete estimating before drafting a proposal."
            )
        return self.proposal.draft_proposal(slug)

    def _send_followup(self, project_name: str) -> str:
        slug = self._find_project_slug(project_name)
        if not slug:
            return f"No project matching \"{project_name}\" found in the pipeline."
        return self.crm.send_followup(slug)

    def _send_email_report(self) -> str:
        """Run the email bid report script."""
        import subprocess
        script = BASE_DIR / "scripts" / "email_bid_report.py"
        result = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            return "Email bid report sent to your Gmail."
        return f"Email failed: {result.stderr[:200]}"

    # ------------------------------------------------------------------
    # Status views
    # ------------------------------------------------------------------

    def get_all_statuses(self) -> str:
        """Return a formatted status board of all active projects."""
        active = self.pipeline.list_active()
        if not active:
            return "No active projects in the pipeline. Use \"bid [project name]\" to start one."

        lines = ["PIPELINE STATUS BOARD\n"]
        stage_emoji = {
            "scouted": "🔍", "docs_downloading": "📥", "docs_ready": "📄",
            "sow_building": "📝", "sow_ready": "✅",
            "takeoff_planning": "📐", "takeoff_uploading": "⬆️", "takeoff_done": "📏",
            "estimating": "🧮", "estimate_ready": "💰",
            "proposal_drafting": "📄", "proposal_ready": "📨",
            "submitted": "📬",
        }

        for slug, proj in active.items():
            stage = proj.get("stage", "unknown")
            emoji = stage_emoji.get(stage, "❓")
            source = proj.get("source", "?")
            dist = proj.get("distance_miles")
            dist_str = f"{dist:.0f}mi" if dist else "?mi"
            lines.append(
                f"{emoji} {proj['name']} ({source}) — {proj.get('gc', '?')} — "
                f"{dist_str} — {stage}"
            )
            if proj.get("bid_amount"):
                lines.append(f"   Bid: ${proj['bid_amount']:,.0f}")
            if proj.get("due_date"):
                lines.append(f"   Due: {proj['due_date']}")

        lines.append(f"\nTotal active: {len(active)} projects")
        return "\n".join(lines)

    def get_project_status(self, project_name: str) -> str:
        """Return detailed status for a single project."""
        slug = self._find_project_slug(project_name)
        if not slug:
            return f"No project matching \"{project_name}\" found in the pipeline."

        proj = self.pipeline.get_project(slug)
        source = proj.get("source", "?")
        dist = proj.get("distance_miles")
        dist_str = f"{dist:.0f} mi" if dist else "? mi"

        lines = [
            f"{proj['name']} ({source})",
            f"{'='*50}",
            f"GC: {proj.get('gc', 'Unknown')}",
            f"Location: {proj.get('city', '')}, {proj.get('state', '')} ({dist_str})",
            f"Due: {proj.get('due_date', 'TBD')}",
            f"Pricing tier: {proj.get('pricing_tier', 'TARGET')}",
            f"",
            f"Current stage: {proj.get('stage', 'unknown')}",
            f"Completed: {', '.join(proj.get('stages_completed', [])) or 'none'}",
        ]

        if proj.get("bid_amount"):
            lines.append(f"Bid amount: ${proj['bid_amount']:,.0f}")
        if proj.get("submitted_at"):
            lines.append(f"Submitted: {proj['submitted_at']}")
        if proj.get("outcome"):
            lines.append(f"Outcome: {proj['outcome']}")
        if proj.get("follow_up_dates"):
            lines.append(f"Follow-ups: {', '.join(proj['follow_up_dates'])}")
        if proj.get("notes"):
            lines.append(f"Notes: {proj['notes']}")

        lines.append(f"\nCreated: {proj.get('created_at', '?')}")
        lines.append(f"Updated: {proj.get('updated_at', '?')}")

        return "\n".join(lines)

    def _list_all_projects(self) -> str:
        """List all projects in the pipeline."""
        all_projects = self.pipeline.list_all()
        if not all_projects:
            return "No projects in the pipeline yet."

        active = []
        completed = []
        for slug, proj in all_projects.items():
            stage = proj.get("stage", "")
            entry = f"  {proj['name']} ({proj.get('source','?')}) — {stage}"
            if stage in ("won", "lost", "no_award"):
                completed.append(entry)
            else:
                active.append(entry)

        lines = []
        if active:
            lines.append(f"ACTIVE ({len(active)}):")
            lines.extend(active)
        if completed:
            lines.append(f"\nCOMPLETED ({len(completed)}):")
            lines.extend(completed)

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Bid lookup helpers
    # ------------------------------------------------------------------

    def _find_bid(self, project_name: str) -> dict | None:
        """Fuzzy match project name against active_bids.json."""
        if not BIDS_FILE.exists():
            return None
        bids = json.loads(BIDS_FILE.read_text(encoding="utf-8"))
        names = [b["project_name"] for b in bids]
        matches = get_close_matches(
            project_name.lower(),
            [n.lower() for n in names],
            n=1, cutoff=0.4,
        )
        if matches:
            for b in bids:
                if b["project_name"].lower() == matches[0]:
                    return b
        # Fallback: substring match
        for b in bids:
            if project_name.lower() in b["project_name"].lower():
                return b
        return None

    def _find_project_slug(self, project_name: str) -> str | None:
        """Fuzzy match project name against pipeline.json slugs."""
        projects = self.pipeline.list_all()
        if not projects:
            return None
        names = {s: p["name"] for s, p in projects.items()}
        matches = get_close_matches(
            project_name.lower(),
            [n.lower() for n in names.values()],
            n=1, cutoff=0.4,
        )
        if matches:
            for slug, name in names.items():
                if name.lower() == matches[0]:
                    return slug
        # Fallback: substring
        for slug, name in names.items():
            if project_name.lower() in name.lower():
                return slug
        return None

    @staticmethod
    def _make_slug(project_name: str) -> str:
        """Convert 'Whole Foods Market (GFR)' -> 'whole_foods_market_gfr'."""
        slug = project_name.lower()
        slug = re.sub(r'[^a-z0-9\s]', ' ', slug)
        slug = re.sub(r'\s+', '_', slug.strip())
        return slug[:80]

    # ------------------------------------------------------------------
    # Formatting helpers
    # ------------------------------------------------------------------

    @staticmethod
    def fmt_money(amount: float) -> str:
        return f"${amount:,.0f}"

    @staticmethod
    def fmt_sf(sf: float) -> str:
        return f"{sf:,.0f} SF"

    def _help_text(self) -> str:
        return (
            "CAROL COMMANDS:\n\n"
            "BID MONITORING:\n"
            "  \"what bids do we have\" — today's bids with scores\n"
            "  \"bids for tomorrow\" — tomorrow's bids\n"
            "  \"bids this week\" — full week view\n"
            "  \"scrape\" / \"refresh bids\" — re-scrape CC+BC\n"
            "  \"email report\" — send HTML report to Gmail\n"
            "  \"check email\" — scan Gmail for bid invitations\n"
            "\n"
            "RANKINGS:\n"
            "  \"which is largest\" — by square footage\n"
            "  \"which is closest\" — by distance\n"
            "  \"best bids\" — by Carol's score\n"
            "  \"soonest due\" — by due date\n"
            "  \"by gc\" — grouped by contractor\n"
            "  \"by type\" — grouped by facility type\n"
            "\n"
            "ESTIMATING:\n"
            "  \"bid [project]\" — start bid pipeline\n"
            "  \"skip [project]\" — decline a bid\n"
            "  \"estimate for [project]\" — show estimate\n"
            "  \"draft proposal [project]\" — write proposal\n"
            "\n"
            "TRACKING:\n"
            "  \"status\" — pipeline status board\n"
            "  \"status [project]\" — project details\n"
            "  \"list projects\" — all projects\n"
            "  \"follow up [project]\" — send follow-up\n"
            "  \"we won [project]\" — record win\n"
            "  \"we lost [project]\" — record loss\n"
            "  \"dedup\" — remove duplicate bids\n"
        )


# ------------------------------------------------------------------
# CLI for testing
# ------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Carol Core — Coordinator Agent")
    parser.add_argument("--message", "-m", help="Message to process")
    parser.add_argument("--from", dest="from_number", default=OWNER_NUMBER)
    args = parser.parse_args()

    carol = CarolCore()

    if args.message:
        response = carol.handle_message(args.message, args.from_number)
        print(response)
    else:
        # Interactive mode
        print("Carol Core — Interactive Mode (type 'quit' to exit)")
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
