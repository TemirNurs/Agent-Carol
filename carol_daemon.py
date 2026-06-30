#!/usr/bin/env python3
"""
carol_daemon.py — The Heartbeat
================================
Async event loop that keeps Carol alive. Runs scheduled tasks
(bid scraping, daily briefing, follow-up checks, email monitoring,
pipeline advancement) on configurable intervals.

Usage:
  python carol_daemon.py                  # Start daemon
  python carol_daemon.py --once scrape    # Run one task and exit
  python carol_daemon.py --list           # Show task schedule
"""

import asyncio
import argparse
import json
import logging
import os
import signal
import sys
import time
import traceback
from datetime import datetime, timedelta
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

# Project root
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# Load .env BEFORE any child script imports — all `os.environ.get(...)`
# fallbacks were stripped on 2026-05-22 (commit before public push), so
# child scripts MUST find these vars in the environment. The .env file
# is gitignored and lives only on the operator's machine.
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    # dotenv not installed → child scripts will still work if user set
    # env vars at the OS level. Log a warning so it's visible.
    print("⚠️  python-dotenv not installed — install with `pip install python-dotenv` "
          "or set GMAIL_APP_PASSWORD, TELEGRAM_BOT_TOKEN, USER_TELEGRAM_CHAT_ID "
          "as OS environment variables.")

from carol_core import CarolCore

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
DATA_DIR = ROOT / "data"
LOGS_DIR = DATA_DIR / "logs"
PID_FILE = DATA_DIR / "carol.pid"
HEARTBEAT_CONFIG = DATA_DIR / "config" / "heartbeat.json"

# ---------------------------------------------------------------------------
# Default schedule — used if heartbeat.json doesn't exist
# ---------------------------------------------------------------------------
DEFAULT_TASKS = [
    {
        "name": "scrape_bids",
        "description": "Scrape CC + BC for new bids",
        "interval_minutes": 30,
        "enabled": True,
    },
    {
        "name": "check_email_bids",
        "description": "Scan Gmail for bid invitations",
        "interval_minutes": 15,
        "enabled": True,
    },
    {
        "name": "daily_briefing",
        "description": "Morning briefing + email report",
        "cron_hour": 6,
        "cron_minute": 30,
        "enabled": True,
    },
    {
        "name": "check_followups",
        "description": "Check and notify about due follow-ups",
        "cron_hour": 9,
        "cron_minute": 0,
        "enabled": True,
    },
    {
        "name": "pipeline_advance",
        "description": "Auto-advance projects with pending steps",
        "interval_minutes": 5,
        "enabled": True,
    },
    {
        "name": "verified_intelligence",
        "description": "Live-fetch missing docs + verify-read real scope + re-rank all open projects",
        "interval_minutes": 60,
        "enabled": True,
    },
    {
        "name": "build_report_snapshots",
        "description": "Render read-only reports (chases/open/recap/stats/today) to data/memory/report_cache/*.txt for Telegram-Carol to READ (OpenClaw can't exec)",
        "interval_minutes": 12,
        "enabled": True,
    },
    {
        "name": "learn_from_interactions",
        "description": "Mine the day's transcripts for Nursultan's corrections/teachings → distill + save new lessons",
        "cron_hour": 23,
        "cron_minute": 30,
        "enabled": True,
    },
    {
        "name": "claude_sub_shim",
        "description": "Keepalive: Claude Max-subscription shim (127.0.0.1:8199) so Telegram runs on Opus 4.8 (free); relaunch if down",
        "interval_minutes": 5,
        "enabled": True,
    },
]


# ---------------------------------------------------------------------------
# Logger setup
# ---------------------------------------------------------------------------
def setup_logging() -> logging.Logger:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("carol_daemon")
    logger.setLevel(logging.INFO)

    # Rotating file handler (daily, keep 7 days)
    fh = TimedRotatingFileHandler(
        LOGS_DIR / "carol_daemon.log",
        when="midnight", backupCount=7, encoding="utf-8",
    )
    fh.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logger.addHandler(fh)

    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(logging.Formatter("%(asctime)s | %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(ch)

    return logger


# ---------------------------------------------------------------------------
# PID file guard
# ---------------------------------------------------------------------------
def check_pid_file() -> bool:
    """Return True if another daemon is running."""
    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text().strip())
            # Check if process is alive (Windows-compatible)
            if sys.platform == "win32":
                import ctypes
                kernel32 = ctypes.windll.kernel32
                handle = kernel32.OpenProcess(0x1000, False, old_pid)  # PROCESS_QUERY_LIMITED_INFORMATION
                if handle:
                    kernel32.CloseHandle(handle)
                    return True
            else:
                os.kill(old_pid, 0)
                return True
        except (OSError, ValueError, PermissionError):
            pass  # Process not running, stale PID file
    return False


def write_pid_file():
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))


def remove_pid_file():
    if PID_FILE.exists():
        PID_FILE.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Task schedule
# ---------------------------------------------------------------------------
def load_schedule() -> list[dict]:
    """Load task schedule from heartbeat.json, or create defaults."""
    if HEARTBEAT_CONFIG.exists():
        try:
            config = json.loads(HEARTBEAT_CONFIG.read_text(encoding="utf-8"))
            return config.get("tasks", DEFAULT_TASKS)
        except (json.JSONDecodeError, KeyError):
            pass

    # Write defaults
    HEARTBEAT_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    HEARTBEAT_CONFIG.write_text(
        json.dumps({"tasks": DEFAULT_TASKS}, indent=2),
        encoding="utf-8",
    )
    return DEFAULT_TASKS


# ---------------------------------------------------------------------------
# Task runners — each wraps an existing CarolCore / agent method
# ---------------------------------------------------------------------------
class TaskRunner:
    """Executes named tasks using CarolCore agents."""

    def __init__(self, carol: CarolCore, logger: logging.Logger):
        self.carol = carol
        self.log = logger

    def run_task(self, task_name: str) -> str:
        """Run a named task. Returns result string."""
        dispatch = {
            "scrape_bids": self._scrape_bids,
            "check_email_bids": self._check_email_bids,
            "chase_silent_followups": self._chase_silent_followups,
            "ingest_email_invites": self._ingest_email_invites,
            "daily_briefing": self._daily_briefing,
            "check_followups": self._check_followups,
            "pipeline_advance": self._pipeline_advance,
            "bid_reminder_morning": self._bid_reminder_morning,
            "bid_reminder_imminent": self._bid_reminder_imminent,
            "bid_reminder_new": self._bid_reminder_new,
            "gmail_organize": self._gmail_organize,
            "track_submissions": self._track_submissions,
            "crm_sync": self._crm_sync,
            "gmail_rules_from_crm": self._gmail_rules_from_crm,
            "crm_writeback": self._crm_writeback,
            "apply_crm_formatting": self._apply_crm_formatting,
            "followup_intelligence_brief": self._followup_intelligence_brief,
            "backfill_chase_history": self._backfill_chase_history,
            "audit_chase_subject_ids": self._audit_chase_subject_ids,
            "morning_chase_report": self._morning_chase_report,
            "rebuild_known_recipient_domains": self._rebuild_known_recipient_domains,
            "geocode_distances": self._geocode_distances,
            "lessons_audit": self._lessons_audit,
            "hallucination_sentry": self._hallucination_sentry,
            "fetch_worker": self._fetch_worker,
            "deadbid_guard": self._deadbid_guard,
            "inbox_monitor": self._inbox_monitor,
            "pbi_sync": self._pbi_sync,
            "stage2_pick": self._stage2_pick,
            "pbi_deepen": self._pbi_deepen,
            "self_audit": self._self_audit,
            "dashboard_keepalive": self._dashboard_keepalive,
            "cowork_agent_index": self._cowork_agent_index,
            "loss_learning_digest": self._loss_learning_digest,
            "direct_ask_watcher": self._direct_ask_watcher,
            "morning_bid_brief": self._morning_bid_brief,
            "weekly_bid_plan": self._weekly_bid_plan,
            "chase_executor": self._chase_executor,
            "cleanup_project_docs": self._cleanup_project_docs,
            "crm_full_sync": self._crm_full_sync,
            "proposal_audit": self._proposal_audit,
            "cost_watchdog": self._cost_watchdog,
            "daemon_self_health": self._daemon_self_health,
            "team_chat_audit": self._team_chat_audit,
            "team_chat_watcher": self._team_chat_watcher,
            "chase_command_watcher": self._chase_command_watcher,
            "scrape_parkway": self._scrape_parkway,
            "forward_div27": self._forward_div27,
            "scrape_procore": self._scrape_procore,
            "import_cowork": self._import_cowork,
            "cowork_fetch": self._cowork_fetch,
            "cowork_sync": self._cowork_sync,
            "cowork_local": self._cowork_local,
            "sync_openclaw": self._sync_openclaw,
            "loss_postmortem_watch": self._loss_postmortem_watch,
            "process_followup_replies": self._process_followup_replies,
            "crm_daily_summary": self._crm_daily_summary,
            "followup_scheduler": self._followup_scheduler,
            "pbi_refresh": self._pbi_refresh,
            "pbi_self_audit": self._pbi_self_audit,
            "verified_intelligence": self._verified_intelligence,
            "build_report_snapshots": self._build_report_snapshots,
            "learn_from_interactions": self._learn_from_interactions,
            "claude_sub_shim": self._claude_sub_shim,
        }
        fn = dispatch.get(task_name)
        if not fn:
            return f"Unknown task: {task_name}"
        return fn()

    def _scrape_bids(self) -> str:
        result = self.carol.scout.run_scrapers()
        deduped = self.carol.scout.dedup_bids()
        # Health check: detect silent failures where a scraper exits cleanly
        # but returns 0 results. Suppress alerts for scrapers we already know
        # are broken (tracked in data/config/known_broken_scrapers.json) so
        # we don't spam the user while waiting for fixes/credentials.
        import re, json
        silent_failures = []
        for scraper_name in ("BC", "CC"):
            m = re.search(rf"{scraper_name}\s+scraper:\s*OK[\s\S]*?SCRAPED\s+(\d+)\s+PROJECTS", result, re.I)
            if m and int(m.group(1)) == 0:
                silent_failures.append(scraper_name)
        if silent_failures:
            # Filter out known-broken scrapers
            try:
                known = json.loads((ROOT / "data" / "config" / "known_broken_scrapers.json").read_text(encoding="utf-8"))
                known_set = set(known.get("scrapers", []))
            except Exception:
                known_set = set()
            unexpected = [s for s in silent_failures if s not in known_set]
            if unexpected:
                msg = f"⚠️ Silent scraper failure: {', '.join(unexpected)} returned 0 projects despite 'OK' status — likely login broken"
                try:
                    from scripts._lib import telegram
                    telegram.send(f"🚨 *Carol scraper health*: {msg}", chat_id="")
                except Exception: pass
                try:
                    sys.path.insert(0, str(ROOT / "scripts"))
                    from log_activity import log_activity
                    log_activity("⚠️ Scraper alert", msg)
                except Exception: pass
                return f"Scrape complete. Removed {deduped} duplicates. {msg}\n{result}"
            # All silent failures are expected — note in return but don't alert
            return f"Scrape complete. Removed {deduped} duplicates. [silenced: {','.join(silent_failures)} known-broken]\n{result}"
        return f"Scrape complete. Removed {deduped} duplicates.\n{result}"

    def _watch_aps_reply(self) -> str:
        return self._run_helper_script("watch_aps_reply", [], timeout=60)

    def _chase_silent_followups(self) -> str:
        # Daily 10:30 AM ET: fires next-attempt follow-ups to non-responders
        # at cadence days 3/7/14/21. Stops when each replies or hits 5 attempts.
        # The chase paces at 25-min intervals (~8+ hours for 21 emails) so we
        # CANNOT run it inline — daemon would time out / block other tasks.
        # Solution: dry-run inline first to see if there's work, then launch
        # the actual apply as a DETACHED background process.
        import subprocess as _sub
        script = ROOT / "scripts" / "chase_silent_followups.py"
        # Dry-run first to count what's queued for today (cheap)
        dry = _sub.run([sys.executable, str(script)],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=60)
        if "[FIRE]" not in (dry.stdout or ""):
            return f"chase_silent_followups: no sends queued today\n{(dry.stdout or '').splitlines()[-1] if dry.stdout else ''}"
        # Count how many will fire
        import re as _re
        m = _re.search(r"\[FIRE\]\s+(\d+):", dry.stdout)
        fire_count = int(m.group(1)) if m else 0
        # Launch detached
        log_path = ROOT / "data" / "logs" / "chase_silent_run.log"
        err_path = ROOT / "data" / "logs" / "chase_silent_run.err"
        with open(log_path, "a", encoding="utf-8") as lf, open(err_path, "a", encoding="utf-8") as ef:
            proc = _sub.Popen(
                [sys.executable, str(script), "--apply", "--interval", "1500"],
                stdout=lf, stderr=ef,
                creationflags=_sub.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
        try:
            sys.path.insert(0, str(ROOT / "scripts"))
            from log_activity import log_activity
            log_activity("📤 Chase loop launched",
                f"chase_silent_followups firing {fire_count} attempt-N emails today, "
                f"25-min spacing → completes in ~{fire_count*25//60}h. CC the internal team. "
                f"Detached PID {proc.pid}. Log: data/logs/chase_silent_run.log")
        except Exception:
            pass
        return f"chase_silent_followups: launched detached PID {proc.pid}, {fire_count} chase emails queued at 25-min intervals"

    def _verified_intelligence(self) -> str:
        # ALWAYS-ON verified bid intelligence: enumerate open projects → live-fetch
        # any missing docs → LLM verify-read REAL painting scope → re-rank →
        # data/memory/verified_rank_state.json (the source of truth every Carol
        # query reads). Long-running (fetches + LLM reads), so launch DETACHED
        # like the chase loop — never block the daemon loop.
        import subprocess as _sub
        script = ROOT / "scripts" / "verified_intelligence.py"
        if not script.exists():
            return "verified_intelligence.py not found"
        log_path = ROOT / "data" / "logs" / "verified_intelligence.log"
        with open(log_path, "a", encoding="utf-8") as lf:
            proc = _sub.Popen(
                [sys.executable, str(script)],
                stdout=lf, stderr=_sub.STDOUT,
                creationflags=_sub.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
        return f"verified_intelligence: launched detached PID {proc.pid} (fetch + verify-read + rank → verified_rank_state.json)"

    def _check_email_bids(self) -> str:
        return self.carol.scout.check_email_bids()

    def _run_reminder(self, mode: str) -> str:
        """Invoke scripts/bid_reminders.py --mode <mode>. Zero LLM cost."""
        import subprocess
        script = ROOT / "scripts" / "bid_reminders.py"
        if not script.exists():
            return "bid_reminders.py not found"
        r = subprocess.run(
            [sys.executable, str(script), "--mode", mode],
            capture_output=True, text=True, timeout=30,
        )
        out = (r.stdout or "") + (r.stderr or "")
        first_line = next((ln for ln in out.splitlines() if ln.strip()), "")
        return f"reminder[{mode}]: {first_line[:120]}"

    def _bid_reminder_morning(self) -> str:
        return self._run_reminder("morning")

    def _bid_reminder_imminent(self) -> str:
        return self._run_reminder("imminent")

    def _bid_reminder_new(self) -> str:
        return self._run_reminder("new")

    def _run_helper_script(self, name: str, args: list[str], timeout: int = 600) -> str:
        """Generic helper to run a Python script under scripts/ or repo root."""
        import subprocess
        for candidate in [ROOT / "scripts" / f"{name}.py", ROOT / f"{name}.py"]:
            if candidate.exists():
                script = candidate; break
        else:
            return f"{name}: script not found"
        try:
            r = subprocess.run(
                [sys.executable, str(script), *args],
                capture_output=True, text=True, timeout=timeout,
            )
            out = (r.stdout or "").strip().splitlines()
            return out[-1] if out else f"{name}: ran (no output)"
        except subprocess.TimeoutExpired:
            return f"{name}: TIMEOUT (>{timeout}s)"
        except Exception as e:
            return f"{name}: ERROR — {e}"

    def _pbi_refresh(self) -> str:
        # Project Bid Intelligence: mechanical tick (ensure skeletons, enqueue
        # Claude-read queue, revalue from stored evidence, rebuild index).
        # No LLM, no downloads — the deep doc read is done by Claude in-session.
        return self._run_helper_script("pbi_refresh", [], timeout=600)

    def _pbi_self_audit(self) -> str:
        return self._run_helper_script("pbi_self_audit", [], timeout=300)

    def _learn_from_interactions(self) -> str:
        # Carol's automatic learning loop: mine the day's CLI + Telegram
        # transcripts for moments Nursultan corrected/taught/praised, distill
        # durable lessons (claude-code, free), dedup, and append genuinely-new
        # ones to AGENTS_LESSONS.md (+ ledger + digest). Rolling 3-day --since
        # window so it only opens recently-touched transcripts, not all 1885.
        from datetime import timedelta
        since = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
        return self._run_helper_script(
            "learn_from_interactions", ["--apply", "--since", since], timeout=1200)

    def _claude_sub_shim(self) -> str:
        """Keepalive (user-approved 6/29) for the Claude MAX-SUBSCRIPTION shim
        (scripts/claude_sub_shim.py) — a local OpenAI endpoint on 127.0.0.1:8199
        that serves the OpenClaw/Telegram agent on Opus 4.8 via Nursultan's Claude
        subscription (free). If it's down, OpenClaw silently falls back to Gemini,
        so relaunch it detached. See [[reference_openclaw_runtime]]."""
        import socket
        import subprocess as _sub
        sk = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sk.settimeout(2)
        try:
            sk.connect(("127.0.0.1", 8199))
            sk.close()
            return "claude_sub_shim: up (127.0.0.1:8199)"
        except Exception:
            try:
                sk.close()
            except Exception:
                pass
        script = ROOT / "scripts" / "claude_sub_shim.py"
        logp = ROOT / "data" / "logs" / "claude_sub_shim.log"
        try:
            lf = open(logp, "a", encoding="utf-8")
            proc = _sub.Popen(
                [sys.executable, str(script)], stdout=lf, stderr=lf,
                creationflags=_sub.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
            return f"claude_sub_shim: was DOWN -> relaunched detached PID {proc.pid}"
        except Exception as e:
            return f"claude_sub_shim: relaunch FAILED — {e}"

    def _gmail_organize(self) -> str:
        return self._run_helper_script("gmail_organize", ["--quiet"], timeout=600)

    def _build_report_snapshots(self) -> str:
        """Render the read-only reports to data/memory/report_cache/*.txt so the
        Telegram agent can READ them (OpenClaw's allowlist exec blocks the .cmd
        runner). Read-only — cannot send or need approval. See [[reference_openclaw_runtime]]."""
        return self._run_helper_script("build_report_snapshots", [], timeout=600)

    def _track_submissions(self) -> str:
        return self._run_helper_script("track_submissions", ["--quiet", "--days", "30"], timeout=300)

    def _crm_sync(self) -> str:
        return self._run_helper_script("crm_sync", ["--auto"], timeout=120)

    def _gmail_rules_from_crm(self) -> str:
        return self._run_helper_script("gmail_rules_from_crm", ["--quiet"], timeout=60)

    def _crm_writeback(self) -> str:
        # Step 0: ESTIMATE.JSON RECONCILE — guarantee every bid we ESTIMATED has a CRM
        # row, even if it arrived by direct email (never scraped to active_bids) AND was
        # sent manually (never logged to bid_status). Those fall through both crm_writeback
        # paths. ROOT-CAUSE FIX (6/26): Broad St UMC / VA Salisbury / USC were estimated/sent
        # yet missing from the CRM until this step. crm_sync_estimates reads data/projects/*/
        # estimate.json (carries verified send_state) and appends any not already present.
        try:
            sync_out = self._run_helper_script("crm_sync_estimates", ["--apply"], timeout=120)
        except Exception as e:
            sync_out = f"crm_sync_estimates error: {e}"
        # Step 1: append/update CRM rows
        wb_out = self._run_helper_script("crm_writeback",
            ["--apply", "--notify", "--quiet"], timeout=120)
        # Step 2: ALWAYS re-apply Sort Priority formula + re-sort the sheet so
        # newly-appended bids float to the top. This was a chronic complaint
        # from Nursultan — newly added rows landed at the BOTTOM because
        # apply_crm_formatting wasn't auto-scheduled. Chaining it here means
        # every writeback that adds a row leaves the sheet correctly sorted.
        try:
            sort_out = self._run_helper_script("apply_crm_formatting",
                ["--apply-sort"], timeout=120)
            return f"est-sync: {sync_out[-90:]} || {wb_out} || sort: {sort_out[-90:]}"
        except Exception as e:
            return f"{wb_out} || sort FAILED: {e}"

    def _apply_crm_formatting(self) -> str:
        """Standalone sort + formatting re-application (in case writeback
        didn't add anything but a manual Status edit needs the row to
        move from active → dead bucket)."""
        return self._run_helper_script("apply_crm_formatting",
            ["--apply-sort"], timeout=120)

    def _followup_intelligence_brief(self) -> str:
        """Morning 5AM per-bid follow-up intelligence — who chased, who
        replied, what they said, next action plan. Refreshes chase history
        from Gmail Sent FIRST so the brief uses ground truth, not stale JSON."""
        # Step 1 — refresh chase history from Gmail Sent (ground truth)
        try:
            self._run_helper_script("backfill_chase_history",
                ["--days", "90", "--quiet"], timeout=180)
        except Exception as e:
            self.log.warning(f"backfill_chase_history failed pre-brief: {e}")
        # Step 2 — now run the brief against fresh data
        return self._run_helper_script("followup_intelligence_brief",
            ["--days", "60", "--email", "--quiet"], timeout=600)

    def _backfill_chase_history(self) -> str:
        """Sync Gmail Sent → bid_status.json chase events. Runs hourly to keep
        the follow-up intelligence brief accurate without waiting until 5AM."""
        return self._run_helper_script("backfill_chase_history",
            ["--days", "30", "--quiet"], timeout=180)

    def _audit_chase_subject_ids(self) -> str:
        """Detect Bid#-vs-Internal-ID drift in outbound subjects after CRM sorts.
        Pings owner if any drifted (a drifted subject means future replies will
        be routed to the WRONG bid by process_followup_replies)."""
        return self._run_helper_script("audit_chase_subject_ids",
            ["--days", "14", "--telegram", "--quiet"], timeout=300)

    def _geocode_distances(self) -> str:
        """Backfill distance_miles on active_bids.json entries where it's
        missing. Uses local hardcoded city table + Nominatim fallback. Runs
        after every scrape so 'what do we have today' always has distances."""
        return self._run_helper_script("geocode_distances", [], timeout=300)

    def _pbi_deepen(self) -> str:
        """KNOW the projects, not just list them (user 2026-06-19). Each run takes the
        next worth-first open in-radius project we only have metadata on, FETCHES its
        bid docs off the portal, READS the RFP/finish-schedule/spec on Claude, and
        writes real CCF scope + value band into the dossier + data/memory/
        deep_knowledge.json. One per run → the ~60-project backlog becomes real
        knowledge over a day or two. Fetch works for ConstructConnect; BC/Procore
        when their sessions are alive."""
        return self._run_helper_script("pbi_deepen", ["--max", "1", "--quiet"], timeout=600)

    def _stage2_pick(self) -> str:
        """LIVE PROJECT PICK INTELLIGENCE (user 2026-06-19: 'find projects worth
        bidding'). Every ~60 min, takes the open in-radius opportunities off the
        live board and runs an LLM scope-sniff that estimates CCF's PAINTING/WC
        sub-scope value (not the whole-project name-guess), re-ranks BID/MAYBE/PASS,
        and writes data/memory/pick_state.json — the instant-read 'what's worth
        bidding now' board."""
        return self._run_helper_script("stage2_pick", ["--quiet"], timeout=600)

    def _pbi_sync(self) -> str:
        """Keep Project Bid Intelligence LIVE (user 2026-06-19). Every ~40 min, pulls
        the best real value for each active bid (amended estimate > estimate.json >
        submitted CRM value > SF band), writes the instant-read store
        data/memory/pbi_state.json, and syncs our real estimates back into each
        dossier so deep work (large deep-read projects) is never lost."""
        return self._run_helper_script("pbi_sync", ["--quiet"], timeout=300)

    def _inbox_monitor(self) -> str:
        """ALWAYS-ON inbox intelligence (user 2026-06-19). Every few minutes, reads
        new GC mail through the LLM thread-comprehension engine, keeps a per-bid
        live ground-truth state (data/memory/inbox_state.json), and pings the owner
        the instant a thread flips to WON / LOST / WE-OWE. So Carol already knows
        what's happening in the inbox before anyone asks."""
        return self._run_helper_script("inbox_monitor", ["--quiet"], timeout=420)

    def _deadbid_guard(self) -> str:
        """Daily safety net against dead-bid-as-live-work (TMSA 6/18). Flags +
        auto-closes any active bid where we owe a re-price/answer, our last
        outbound is older than the GC's last ask, and the GC's emailed deadline
        passed (or the we-owe is >10d stale). Keeps corpses out of the chase/draft
        path so they never waste the owner's time again."""
        return self._run_helper_script("deadbid_guard", ["--apply"], timeout=400)

    def _rebuild_known_recipient_domains(self) -> str:
        """Refresh the trusted-domains cache from Gmail Inbox FROM addresses.
        send_email.py uses this to detect typo'd recipient domains like
        newcoconstruction.co (vs .com). Runs daily."""
        return self._run_helper_script("rebuild_known_recipient_domains",
            ["--days", "180", "--quiet"], timeout=300)

    def _morning_chase_report(self) -> str:
        """Daily 7AM brief: yesterday's chase outcomes + today's proposed plan.
        NO emails sent — user reviews on Telegram and approves before any send.
        This is the new entry-point for all daily chase activity (user's
        5/25 rule)."""
        # Backfill chase history first so yesterday's stats are accurate
        try:
            self._run_helper_script("backfill_chase_history",
                ["--days", "7", "--quiet"], timeout=180)
        except Exception as e:
            self.log.warning(f"backfill failed before morning report: {e}")
        # --telegram: the daemon's 7 AM run is the ONE place that pushes the
        # brief to Telegram. On-demand runs (Carol answering "give me a
        # brief") omit it so the user isn't sent the report twice.
        return self._run_helper_script("morning_chase_report",
            ["--quiet", "--telegram"], timeout=300)

    def _lessons_audit(self) -> str:
        """Nightly audit — verify codebase still complies with
        AGENTS_LESSONS.md rules. Ping if regressions detected."""
        return self._run_helper_script("_lessons_audit", [], timeout=120)

    def _hallucination_sentry(self) -> str:
        """Hourly truth-check on Carol's answers. Pings Telegram if she lies."""
        return self._run_helper_script("_hallucination_sentry", [], timeout=120)

    def _fetch_worker(self) -> str:
        """Run ONE queued background doc-fetch (enqueue_fetch.py jobs) and push the
        result to the requester on Telegram. Makes 'I'll message you when done' real."""
        return self._run_helper_script("fetch_worker", [], timeout=600)

    def _self_audit(self) -> str:
        """Nightly self-audit: reconcile CRM ↔ Sent Mail ↔ chase-state and
        Telegram the mismatch digest. Carol finds her own errors before the
        user does (amounts, ghost rows, dupes, orphans, status drift)."""
        return self._run_helper_script("self_audit", [], timeout=600)

    def _chase_executor(self) -> str:
        """08:45 AUTOPILOT: execute the morning chase plan through the
        10-layer guard stack (user-authorized 6/11; 'pause chases' vetoes)."""
        return self._run_helper_script("chase_executor", [], timeout=14400)

    def _weekly_bid_plan(self) -> str:
        """Monday 05:35: audit all invitations due this week, pick the best
        up-to-10, schedule 2/day Mon-Fri by earliest due → Telegram plan."""
        return self._run_helper_script("weekly_bid_plan", [], timeout=900)

    def _morning_bid_brief(self) -> str:
        """05:45 daily: top-3 due TODAY (history-ranked) + tomorrow preview →
        Telegram before the user's 6 AM start. User picks; Carol estimates;
        proposals become Gmail drafts the user sends himself."""
        return self._run_helper_script("morning_bid_brief", [], timeout=600)

    def _direct_ask_watcher(self) -> str:
        """Win-channel fast lane: detect GC direct pricing asks within 30 min
        and ping URGENT (the only contract ever won started as one)."""
        return self._run_helper_script("direct_ask_watcher", [], timeout=300)

    def _loss_learning_digest(self) -> str:
        """Weekly: rebuild loss-learning scorecards from the CRM and Telegram
        the digest (GC flags, facility tier advice, bucket counts)."""
        return self._run_helper_script("loss_learning", ["--digest"], timeout=300)

    def _cowork_agent_index(self) -> str:
        """Index the desktop app's Cowork-tab sessions (local-agent-mode-
        sessions audit.jsonl) into the MemPalace — the user's estimating
        threads, incremental (state-tracked)."""
        return self._run_helper_script("cowork_agent_index", [], timeout=900)

    def _dashboard_keepalive(self) -> str:
        """Keep the read-only pipeline dashboard alive on 127.0.0.1:8765.
        Spawns it detached if the port doesn't answer; no-op when healthy."""
        import urllib.request
        try:
            urllib.request.urlopen("http://127.0.0.1:8765/", timeout=5)
            return "dashboard healthy"
        except Exception:
            pass
        try:
            import subprocess
            DETACHED = 0x00000008 | 0x00000200   # DETACHED_PROCESS | NEW_PROCESS_GROUP
            subprocess.Popen(
                [sys.executable, str(Path(__file__).parent / "scripts" / "dashboard.py")],
                creationflags=DETACHED,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                cwd=str(Path(__file__).parent),
            )
            return "dashboard (re)started on :8765"
        except Exception as e:
            return f"dashboard start FAILED: {e}"

    def _cleanup_project_docs(self) -> str:
        """Weekly garbage collection — delete bid doc folders for closed
        projects (Lost/Won/Withdrawn) older than 14 days. Recovers GBs of
        disk on the Windows host."""
        return self._run_helper_script("cleanup_project_docs",
            ["--min-age", "14", "--apply", "--quiet"], timeout=600)

    def _proposal_audit(self) -> str:
        return self._run_helper_script("proposal_audit",
            ["--days", "30", "--apply", "--threshold", "0.45", "--quiet"], timeout=300)

    def _cost_watchdog(self) -> str:
        return self._run_helper_script("cost_watchdog", ["--quiet"], timeout=30)

    def _team_chat_audit(self) -> str:
        """Snapshot per-user Telegram transcripts to data/memory/team_conversations/."""
        return self._run_helper_script("team_chat_audit", ["--save", "--quiet"], timeout=120)

    def _team_chat_watcher(self) -> str:
        """Real-time Telegram alert when a teammate (non-owner) messages Carol."""
        return self._run_helper_script("team_chat_watcher", ["--quiet"], timeout=60)

    def _chase_command_watcher(self) -> str:
        """Act on the owner's Telegram chase commands (approve/pause/resume) so
        chases fire on his word. Was scheduled every 2 min but NEVER registered
        in the dispatch -> ran as 'Unknown task' ~194x/day, silently breaking the
        Telegram chase-approval loop (registered 6/29)."""
        return self._run_helper_script("chase_command_watcher", ["--quiet"], timeout=60)

    def _forward_div27(self) -> str:
        # 6/29: 300->600s. The 45-min job timed out at 300s forwarding matching
        # Div-27 invites over IMAP/SMTP; a transient slow run shouldn't read as a
        # hard failure.
        return self._run_helper_script("forward_div27", ["--quiet"], timeout=600)

    def _scrape_parkway(self) -> str:
        """Scrape Parkway Construction private bidding portal (a top GC)."""
        return self._run_helper_script("scrape_parkway_portal",
            ["--max-distance", "300", "--quiet"], timeout=300)

    def _scrape_procore(self) -> str:
        """Scrape Procore sub bid invitations."""
        return self._run_helper_script("scrape_procore_portal",
            ["--max-distance", "300", "--quiet"], timeout=300)

    def _daemon_self_health(self) -> str:
        """Self-report a healthy heartbeat. (Watchdog is external — runs even
        if THIS daemon is dead. This task just keeps the heartbeat file fresh
        during long-running tasks like scrape_bids that might block writes.)"""
        from pathlib import Path
        import json as _json
        from datetime import datetime as _dt
        try:
            hb = Path(__file__).resolve().parent / "data" / "health" / "daemon.heartbeat"
            hb.parent.mkdir(parents=True, exist_ok=True)
            hb.write_text(_json.dumps({
                "ts": _dt.now().isoformat(timespec="seconds"),
                "pid": os.getpid(),
                "active_task": "self_health_ping",
            }), encoding="utf-8")
            return "heartbeat refreshed"
        except Exception as e:
            return f"heartbeat write failed: {e}"

    def _import_cowork(self) -> str:
        """Auto-import any new claude.ai exports dropped into data/cowork_imports/."""
        return self._run_helper_script("import_cowork_export", ["--quiet"], timeout=600)

    def _cowork_fetch(self) -> str:
        """Pull fresh conversations from claude.ai via session cookie."""
        return self._run_helper_script("cowork_fetch", ["--quiet", "--since-days", "30"], timeout=900)

    def _cowork_sync(self) -> str:
        """Full cowork pipeline: fetch from claude.ai → index into MemPalace."""
        fetch = self._cowork_fetch()
        ingest = self._import_cowork()
        local = self._cowork_local()
        return f"cowork_sync: fetch={fetch[:40]} | ingest={ingest[:40]} | local={local[:40]}"

    def _cowork_local(self) -> str:
        """Index local Claude Cowork (desktop app scheduled/background) sessions."""
        return self._run_helper_script("cowork_local_index", ["--quiet", "--since-days", "180"], timeout=300)

    def _sync_openclaw(self) -> str:
        """Keep Telegram Carol's AGENTS.md / USER.md in sync with the canonical Carol files."""
        return self._run_helper_script("sync_openclaw_workspace", ["--quiet"], timeout=30)

    def _loss_postmortem_watch(self) -> str:
        """Auto-generate postmortems for newly-lost bids, ping Telegram."""
        return self._run_helper_script("loss_postmortem",
            ["--new-only", "--write-notes", "--telegram", "--quiet"], timeout=900)

    def _process_followup_replies(self) -> str:
        """Auto-classify Inbox replies to follow-ups and update CRM. Telegram ping."""
        return self._run_helper_script("process_followup_replies",
            ["--since-days", "14", "--quiet"], timeout=900)

    def _crm_daily_summary(self) -> str:
        """Daily CRM status breakdown to Telegram. Runs morning + evening."""
        return self._run_helper_script("crm_daily_summary", ["--quiet"], timeout=120)

    def _followup_scheduler(self) -> str:
        """Auto-cadence engine: draft due follow-ups, auto-send <$25K, stage rest."""
        return self._run_helper_script("followup_scheduler", ["--quiet"], timeout=900)

    def _crm_full_sync(self) -> str:
        """End-of-day reconciliation: scrape + ingest + sync + writeback in sequence."""
        steps = []
        for task, args in [
            ("scrape_bids", []),
            ("ingest_email_invites", []),
            ("track_submissions", []),
            ("crm_sync", []),
            ("crm_writeback", []),
        ]:
            try:
                fn = getattr(self, f"_{task}")
                r = fn() if not args else fn(*args)
                steps.append(f"{task}={r[:60] if r else 'ok'}")
            except Exception as e:
                steps.append(f"{task}=err({e})")
        return "EOD CRM sync: " + " | ".join(steps)

    def _ingest_email_invites(self) -> str:
        """Parse Gmail invitations and add new ones to active_bids.json."""
        import subprocess
        script = ROOT / "scripts" / "ingest_email_invites.py"
        if not script.exists():
            return "ingest_email_invites.py not found"
        r = subprocess.run(
            [sys.executable, str(script), "--days", "7", "--skip-past-due"],
            capture_output=True, text=True, timeout=180,
        )
        out = (r.stdout or "") + (r.stderr or "")
        # Extract summary line
        for line in out.splitlines():
            if "NEW to add" in line or "wrote" in line or "nothing new" in line:
                return line.strip()
        return out.strip()[-300:] if out else "ingest_email_invites ran (no output)"

    def _daily_briefing(self) -> str:
        # Scrape first
        self.carol.scout.run_scrapers()

        # Briefing text
        briefing = self.carol.scout.get_briefing(days_ahead=0)

        # Email report
        import subprocess
        script = ROOT / "scripts" / "email_bid_report.py"
        if script.exists():
            subprocess.run(
                [sys.executable, str(script)],
                capture_output=True, text=True, timeout=60,
            )

        # Follow-ups
        due = self.carol.crm.check_followups_due()
        followup_count = len(due) if due else 0

        return f"Briefing sent. {followup_count} follow-ups due.\n{briefing}"

    def _check_followups(self) -> str:
        due = self.carol.crm.check_followups_due()
        if not due:
            return "No follow-ups due."
        results = []
        for fu in due:
            result = self.carol.crm.send_followup(fu["slug"])
            results.append(f"  {fu['name']}: {result}")
        return f"{len(due)} follow-ups processed:\n" + "\n".join(results)

    def _pipeline_advance(self) -> str:
        """Check all active projects and advance any with auto-completable steps."""
        active = self.carol.pipeline.list_active()
        if not active:
            return "No active projects."

        advanced = []
        for slug, proj in active.items():
            stage = proj.get("stage", "")
            # Auto-advance stages that don't need human approval:
            # - docs_downloading → docs_ready (check if docs exist)
            # - takeoff_uploading → takeoff_done (check if Togal finished)
            proj_dir = ROOT / "data" / "projects" / slug

            if stage == "docs_downloading":
                doc_dir = proj_dir / "documents"
                if doc_dir.exists() and any(doc_dir.iterdir()):
                    self.carol.pipeline.update_stage(slug, "docs_ready")
                    advanced.append(f"{proj['name']}: docs_downloading -> docs_ready")

            elif stage == "takeoff_uploading":
                takeoff_file = proj_dir / "togal_takeoff.json"
                if takeoff_file.exists():
                    self.carol.pipeline.update_stage(slug, "takeoff_done")
                    advanced.append(f"{proj['name']}: takeoff_uploading -> takeoff_done")

        if advanced:
            return f"Advanced {len(advanced)} projects:\n" + "\n".join(advanced)
        return f"Checked {len(active)} projects. No auto-advances."


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------
class Scheduler:
    """Tracks when tasks are due based on interval or cron-style schedule."""

    def __init__(self, tasks: list[dict]):
        self.tasks = tasks
        self.last_run: dict[str, datetime] = {}
        # PERSISTED across restarts — a 09:30 daemon restart must NOT re-fire
        # the whole 04:30-07:30 morning cron set (6/11 duplicate-spam incident:
        # the user got the entire morning briefing wave twice).
        self._fired_file = ROOT / "data" / "memory" / "daemon_daily_fired.json"
        self._daily_fired: dict[str, str] = {}
        try:
            sys.path.insert(0, str(ROOT / "scripts"))
            from _lib.state import load_json
            d = load_json(self._fired_file, default={})
            if isinstance(d, dict):
                self._daily_fired = d
        except Exception:
            pass

    def get_due_tasks(self) -> list[dict]:
        """Return list of tasks that should run now."""
        now = datetime.now()
        due = []

        for task in self.tasks:
            if not task.get("enabled", True):
                continue

            name = task["name"]

            # Cron-style (daily at specific time)
            if "cron_hour" in task:
                # Honor day-of-week when present (0=Mon..6=Sun). Without this
                # the weekly cleanup_project_docs --apply DELETION job ran
                # EVERY day at 03:00.
                dow = task.get("cron_day_of_week")
                if dow is not None and now.weekday() != int(dow):
                    continue
                target_time = now.replace(
                    hour=task["cron_hour"],
                    minute=task.get("cron_minute", 0),
                    second=0, microsecond=0,
                )
                today_str = now.strftime("%Y-%m-%d")
                # Key fired-state by name+schedule so two same-named tasks at
                # different times (e.g. crm_daily_summary 07:30 and 18:00)
                # each fire instead of the first marking both done.
                fired_key = f"{name}@{task['cron_hour']:02d}:{task.get('cron_minute', 0):02d}"
                already_fired = self._daily_fired.get(fired_key) == today_str

                if not already_fired and now >= target_time:
                    due.append(task)
                    self._daily_fired[fired_key] = today_str
                    try:
                        from _lib.state import save_json
                        save_json(self._fired_file, self._daily_fired)
                    except Exception:
                        pass
                continue

            # Interval-based
            interval = task.get("interval_minutes", 30)
            last = self.last_run.get(name)
            if last is None or (now - last).total_seconds() >= interval * 60:
                due.append(task)

        return due

    def mark_run(self, task_name: str):
        self.last_run[task_name] = datetime.now()


# ---------------------------------------------------------------------------
# Main daemon loop
# ---------------------------------------------------------------------------
async def daemon_loop(logger: logging.Logger):
    """The heartbeat. Runs forever, checking for due tasks every 60s."""
    carol = CarolCore()
    runner = TaskRunner(carol, logger)
    tasks = load_schedule()
    scheduler = Scheduler(tasks)

    # Pipeline status on startup
    active = carol.pipeline.list_active()
    active_count = len(active)
    logger.info(f"Carol daemon started. PID={os.getpid()}. {active_count} active projects.")

    # Log configured tasks
    for t in tasks:
        if t.get("enabled", True):
            if "cron_hour" in t:
                logger.info(f"  Task: {t['name']} — daily at {t['cron_hour']:02d}:{t.get('cron_minute',0):02d}")
            else:
                logger.info(f"  Task: {t['name']} — every {t.get('interval_minutes', 30)} min")

    # Heartbeat file — touched every loop. External watchdog reads its mtime
    # to detect a silently-dead daemon (the failure mode that bit us 5/4 at 8:44am).
    HEARTBEAT_FILE = DATA_DIR / "health" / "daemon.heartbeat"
    HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)

    def _write_heartbeat(active_task: str = "idle"):
        try:
            HEARTBEAT_FILE.write_text(
                json.dumps({
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "pid": os.getpid(),
                    "active_task": active_task,
                }),
                encoding="utf-8",
            )
        except Exception:
            pass

    _write_heartbeat("startup")

    while True:
        try:
            _write_heartbeat("scheduling")
            due_tasks = scheduler.get_due_tasks()
            for task in due_tasks:
                name = task["name"]
                logger.info(f"Running: {name}")
                start = time.time()
                _write_heartbeat(active_task=name)
                try:
                    result = runner.run_task(name)
                    elapsed = time.time() - start
                    # Log first 200 chars of result
                    preview = result[:200].replace("\n", " ") if result else "(no output)"
                    logger.info(f"Done: {name} ({elapsed:.1f}s) — {preview}")
                except Exception as e:
                    logger.error(f"FAILED: {name} — {e}")
                    logger.debug(traceback.format_exc())
                finally:
                    scheduler.mark_run(name)

        except Exception as e:
            logger.error(f"Scheduler error: {e}")

        await asyncio.sleep(60)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Carol Daemon — The Heartbeat")
    parser.add_argument("--once", metavar="TASK",
                        help="Run one task and exit (scrape_bids, daily_briefing, etc.)")
    parser.add_argument("--list", action="store_true",
                        help="Show configured tasks and exit")
    args = parser.parse_args()

    logger = setup_logging()

    if args.list:
        tasks = load_schedule()
        print("CAROL HEARTBEAT — Task Schedule")
        print("=" * 50)
        for t in tasks:
            status = "ON" if t.get("enabled", True) else "OFF"
            if "cron_hour" in t:
                sched = f"daily at {t['cron_hour']:02d}:{t.get('cron_minute',0):02d}"
            else:
                sched = f"every {t.get('interval_minutes', 30)} min"
            print(f"  [{status}] {t['name']:<25} {sched:<20} {t.get('description', '')}")
        return

    if args.once:
        carol = CarolCore()
        runner = TaskRunner(carol, logger)
        logger.info(f"Running single task: {args.once}")
        result = runner.run_task(args.once)
        print(result)
        return

    # Full daemon mode
    if check_pid_file():
        print(f"Another Carol daemon is already running. PID file: {PID_FILE}")
        print("Delete the PID file if the previous daemon crashed.")
        sys.exit(1)

    write_pid_file()

    # Graceful shutdown
    def shutdown(signum, frame):
        logger.info("Carol daemon shutting down.")
        remove_pid_file()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        asyncio.run(daemon_loop(logger))
    except KeyboardInterrupt:
        logger.info("Carol daemon stopped by user.")
    finally:
        remove_pid_file()


if __name__ == "__main__":
    main()
