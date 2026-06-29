#!/usr/bin/env python3
r"""fetch_worker.py — daemon worker: run ONE queued doc-fetch + notify on Telegram.

Reads data/fetch_queue/*.json (status=queued), downloads the bid documents for the
oldest job (fetch_project_docs.py for CC/BC, fetch_parkway_docs.py for Parkway),
then PUSHES the result to the requester. This is what makes Carol's "I'll message
you when it's done" TRUE instead of a stall. Run by the daemon every ~3 min.
One job per run; a lock prevents overlap with a still-running fetch.

Run:  python scripts/fetch_worker.py
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    pass

QDIR = ROOT / "data" / "fetch_queue"
LOCK = QDIR / ".worker.lock"
OWNER = os.environ.get("USER_TELEGRAM_CHAT_ID", "")
FAIL_WORDS = ("session expired", "expired", "not found", "no documents",
              "no bid", "traceback", "error:", "could not", "failed")


def tg(text, chat_id=""):
    try:
        from _lib import telegram
        telegram.send(text, chat_id=chat_id or None)
    except Exception:
        pass


def resolve(job):
    """(project_name, source) — resolve bid# via CRM if only a bid was given."""
    if job.get("project"):
        return job["project"], ""
    bid = (job.get("bid") or "").strip()
    try:
        from crm_lib import get_sheet
        for r in get_sheet("Bid Log").get_all_records():
            if (r.get("Bid #") or "").strip() == bid:
                return (r.get("Project Name") or bid), (r.get("Bid Source") or "")
    except Exception:
        pass
    return bid or job.get("target", "?"), ""


def lock_ok():
    QDIR.mkdir(parents=True, exist_ok=True)
    if LOCK.exists():
        try:
            if time.time() - LOCK.stat().st_mtime < 1500:  # 25-min stale window
                return False
        except Exception:
            pass
    LOCK.write_text(str(os.getpid()), encoding="utf-8")
    return True


def main():
    if not QDIR.exists():
        print("no queue dir")
        return 0
    job_file = job = None
    for p in sorted(QDIR.glob("*.json")):
        if p.name.startswith("."):
            continue
        try:
            j = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if j.get("status") == "queued":
            job_file, job = p, j
            break
    if not job_file:
        print("no queued jobs")
        return 0
    if not lock_ok():
        print("worker busy (lock held)")
        return 0

    try:
        proj, source = resolve(job)
        job["status"] = "running"
        job["started_at"] = datetime.now().isoformat(timespec="seconds")
        job["attempts"] = job.get("attempts", 0) + 1
        job_file.write_text(json.dumps(job, indent=2), encoding="utf-8")

        is_parkway = "parkway" in (source or "").lower() or "parkway" in proj.lower()
        script = "fetch_parkway_docs.py" if is_parkway else "fetch_project_docs.py"
        try:
            r = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / script), proj],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=540,
            )
            out = (r.stdout or "")
            low = (out + "\n" + (r.stderr or "")).lower()
            rc = r.returncode
        except subprocess.TimeoutExpired:
            out, low, rc = "", "timeout", 1

        ok = (rc == 0) and not any(w in low for w in FAIL_WORDS) \
            and any(k in low for k in ("saved", "downloaded", "files", "document"))
        tail = "\n".join(l for l in out.splitlines() if l.strip())[-1000:]

        if ok:
            job["status"] = "done"
            msg = (f"✅ Done — pulled the documents for *{proj}* you asked me to fetch:\n\n"
                   f"{tail or 'Downloaded; see the project folder.'}")
        else:
            job["status"] = "failed"
            if "expired" in low or "session expired" in low:
                reason = ("the BuildingConnected session expired — re-run "
                          "`python scripts/bc_login_capture.py` (one-time login), then I'll retry.")
            elif "timeout" in low:
                reason = "the download took too long and timed out; I'll retry next cycle."
            elif "not found" in low or "no bid" in low:
                reason = f"couldn't find \"{proj}\" on the portals / in active_bids.json."
            else:
                reason = (tail or "the fetch failed")[:600]
            msg = f"⚠️ Couldn't pull docs for *{proj}*: {reason}"

        job["finished_at"] = datetime.now().isoformat(timespec="seconds")
        job["result"] = tail[:1500]
        job_file.write_text(json.dumps(job, indent=2), encoding="utf-8")
        tg(msg, chat_id=job.get("requested_by") or OWNER)
        print(f"{job['id']}: {job['status']} ({script}) -> notified "
              f"{job.get('requested_by') or OWNER}")
    finally:
        try:
            LOCK.unlink()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
