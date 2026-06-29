#!/usr/bin/env python3
"""
cowork_fetch.py — Pull claude.ai conversations into Carol's cowork pipeline.

Auto-fetches via the user's session cookie (no password, no 2FA flow).
Writes to data/cowork_imports/auto_{YYYY-MM-DD}/ in the same shape as the
official claude.ai data export, so import_cowork_export.py picks it up.

One-time setup:
  python scripts/cowork_fetch.py --setup
  → opens an instructions screen, you paste your sessionKey cookie once,
    we save it to data/config/claude_session.json (chmod 600).

Daily run (handled by daemon, but you can call manually):
  python scripts/cowork_fetch.py
  python scripts/cowork_fetch.py --since-days 30
  python scripts/cowork_fetch.py --quiet

When the cookie expires, the script pings Telegram asking you to refresh.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
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

try:
    import requests
except ImportError:
    print("ERROR: requests required. pip install requests")
    sys.exit(1)

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / "data" / "config" / "claude_session.json"
IMPORTS_DIR = ROOT / "data" / "cowork_imports"
LOG_FILE    = ROOT / "data" / "logs" / "cowork_fetch.log"

CLAUDE_BASE = "https://claude.ai"

# Telegram (re-uses Carol's bot)
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.environ.get("USER_TELEGRAM_CHAT_ID", "")


def tg_send(text: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=15,
        )
    except Exception:
        pass


def log(msg: str, quiet: bool = False):
    if not quiet:
        print(msg)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now().isoformat(timespec='seconds')}  {msg}\n")


def load_config() -> dict | None:
    if not CONFIG_FILE.exists():
        return None
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_config(cfg: dict):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    # Restrict permissions where possible (best-effort on Windows)
    try:
        os.chmod(CONFIG_FILE, 0o600)
    except Exception:
        pass


def make_session(session_key: str) -> requests.Session:
    s = requests.Session()
    s.cookies.set("sessionKey", session_key, domain=".claude.ai")
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://claude.ai/",
        "Origin": "https://claude.ai",
    })
    return s


def fetch_organizations(s: requests.Session) -> list[dict]:
    r = s.get(f"{CLAUDE_BASE}/api/organizations", timeout=30)
    if r.status_code == 401 or r.status_code == 403:
        raise PermissionError(f"Auth failed ({r.status_code}). Cookie likely expired.")
    r.raise_for_status()
    return r.json()


def list_conversations(s: requests.Session, org_uuid: str) -> list[dict]:
    """List all chat conversations for an org. Paginated."""
    all_convs = []
    # Claude.ai endpoint returns full list (no pagination param in current impl,
    # but we handle both cases defensively)
    r = s.get(
        f"{CLAUDE_BASE}/api/organizations/{org_uuid}/chat_conversations",
        timeout=60,
    )
    if r.status_code != 200:
        raise RuntimeError(f"List conversations failed: {r.status_code} {r.text[:200]}")
    data = r.json()
    if isinstance(data, list):
        all_convs = data
    elif isinstance(data, dict):
        all_convs = data.get("conversations") or data.get("results") or []
    return all_convs


def fetch_conversation(s: requests.Session, org_uuid: str, conv_uuid: str) -> dict | None:
    """Fetch one full conversation including all messages."""
    url = (
        f"{CLAUDE_BASE}/api/organizations/{org_uuid}"
        f"/chat_conversations/{conv_uuid}?tree=True&rendering_mode=raw"
    )
    r = s.get(url, timeout=60)
    if r.status_code != 200:
        return None
    return r.json()


def normalize_conversation(conv: dict, list_meta: dict | None = None) -> dict:
    """Transform claude.ai's API shape into the official export shape so
    import_cowork_export.py can ingest it without changes.

    list_meta is the lighter conversation object from list_conversations(),
    which contains the 'project' field that the full GET endpoint sometimes omits.
    """
    proj = conv.get("project")
    if not proj and list_meta:
        proj = list_meta.get("project")
    proj_name = ""
    proj_uuid = ""
    if isinstance(proj, dict):
        proj_name = (proj.get("name") or "").strip()
        proj_uuid = proj.get("uuid", "")

    out = {
        "uuid": conv.get("uuid"),
        "name": conv.get("name") or "(untitled)",
        "summary": conv.get("summary", ""),
        "model": conv.get("model"),
        "created_at": conv.get("created_at"),
        "updated_at": conv.get("updated_at"),
        "claude_project": proj_name,         # NEW: Claude Project ("Cowork") name
        "claude_project_uuid": proj_uuid,
        "chat_messages": [],
    }
    for m in conv.get("chat_messages", []) or []:
        sender = m.get("sender") or m.get("role") or "?"
        # Body can be 'text' or list of content blocks
        text = m.get("text") or ""
        if not text and isinstance(m.get("content"), list):
            for blk in m["content"]:
                if isinstance(blk, dict):
                    text += blk.get("text", "") + " "
        text = text.strip()
        out["chat_messages"].append({
            "uuid": m.get("uuid"),
            "sender": sender,
            "text": text,
            "created_at": m.get("created_at"),
            "updated_at": m.get("updated_at"),
        })
    return out


def fetch_projects(s: requests.Session, org_uuid: str) -> list[dict]:
    """Fetch project workspace list. Optional — empty list on failure."""
    try:
        r = s.get(
            f"{CLAUDE_BASE}/api/organizations/{org_uuid}/projects",
            timeout=30,
        )
        if r.status_code == 200:
            data = r.json()
            return data if isinstance(data, list) else (data.get("projects", []) or [])
    except Exception:
        pass
    return []


def setup_interactive():
    """One-time cookie capture flow."""
    print("=" * 70)
    print("Carol — claude.ai cowork sync setup")
    print("=" * 70)
    print()
    print("To let Carol pull your claude.ai conversations automatically,")
    print("paste your sessionKey cookie value (one-time setup).")
    print()
    print("HOW TO GET IT (5 steps, ~60 seconds):")
    print("  1. Open https://claude.ai in Chrome/Edge while logged in")
    print("  2. Press F12 to open DevTools")
    print("  3. Top tabs → Application  (may be hidden under '>>')")
    print("  4. Left sidebar → Storage → Cookies → https://claude.ai")
    print("  5. Find row 'sessionKey' → double-click the Value cell → copy it")
    print()
    print("The value starts with 'sk-ant-sid01-' and is ~100 chars.")
    print("It is NOT your API key. It expires in ~30 days, then we'll ping you.")
    print()
    raw = input("Paste sessionKey: ").strip()
    if not raw:
        print("No value entered. Aborting.")
        return 1
    if raw.startswith("sessionKey="):
        raw = raw.split("=", 1)[1].strip().strip('";')
    if not raw.startswith("sk-ant-sid"):
        print(f"WARNING: value doesn't start with 'sk-ant-sid'. Saving anyway.")

    # Validate by hitting /api/organizations
    s = make_session(raw)
    try:
        orgs = fetch_organizations(s)
    except PermissionError as e:
        print(f"\nFAILED: {e}")
        print("The cookie is invalid. Double-check you copied 'sessionKey' (not something else).")
        return 1
    except Exception as e:
        print(f"\nNetwork error during validation: {e}")
        return 1

    if not orgs:
        print("\nWARNING: cookie valid but no organizations returned. Saving anyway.")
        org_uuid = ""
        org_name = ""
    else:
        # Pick first org by default; if multiple, show menu
        if len(orgs) > 1:
            print(f"\nFound {len(orgs)} workspaces:")
            for i, o in enumerate(orgs):
                print(f"  [{i+1}] {o.get('name','(unnamed)')}  ({o.get('uuid','')[:8]}...)")
            choice = input(f"Pick one [1-{len(orgs)}, default 1]: ").strip() or "1"
            try:
                org = orgs[int(choice) - 1]
            except (ValueError, IndexError):
                org = orgs[0]
        else:
            org = orgs[0]
        org_uuid = org.get("uuid", "")
        org_name = org.get("name", "")
        print(f"\nUsing workspace: {org_name} ({org_uuid[:8]}...)")

    save_config({
        "session_key": raw,
        "org_uuid": org_uuid,
        "org_name": org_name,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
    })
    print(f"\nSaved → {CONFIG_FILE}")
    print("Test fetch:  python scripts/cowork_fetch.py --since-days 7")
    return 0


def _cleanup_old_auto_folders(imports_dir: Path, days: int = 30):
    """Delete auto_* subfolders older than N days to keep disk usage bounded."""
    import shutil
    cutoff = time.time() - days * 86400
    for d in imports_dir.glob("auto_*"):
        if d.is_dir() and d.stat().st_mtime < cutoff:
            try:
                shutil.rmtree(d)
            except Exception:
                pass


# Shared with import_cowork_export.py — see data/config/cowork_exclude.json
def _load_exclude_cfg():
    try:
        from pathlib import Path
        import json as _json
        cfg_path = Path(__file__).resolve().parent.parent / "data" / "config" / "cowork_exclude.json"
        if cfg_path.exists():
            cfg = _json.load(open(cfg_path, encoding="utf-8"))
            names = {n.strip().lower() for n in cfg.get("exclude_project_names", []) if n}
            kws = {k.strip().lower() for k in cfg.get("exclude_project_keywords", []) if k}
            return names, kws
    except Exception:
        pass
    return set(), set()


def _is_excluded_project(name: str, names: set, kws: set) -> bool:
    n = (name or "").strip().lower()
    if not n: return False
    if n in names: return True
    return any(k in n for k in kws)


def _fetch_one_org(s: requests.Session, org_uuid: str, org_name: str,
                   since_days: int, max_convs: int | None, quiet: bool) -> tuple[list, list, int]:
    """Fetch conversations + projects from a single org. Returns (convs, projects, failed_count)."""
    try:
        convs = list_conversations(s, org_uuid)
    except Exception as e:
        log(f"[cowork_fetch] {org_name}: list failed: {e}", quiet)
        return [], [], 0

    log(f"[cowork_fetch] {org_name}: {len(convs)} total conversations", quiet)
    # Drop conversations from excluded (personal) Claude Projects up front so
    # we don't even pay the per-conv fetch cost.
    _exc_names, _exc_kws = _load_exclude_cfg()
    if _exc_names or _exc_kws:
        before = len(convs)
        convs = [c for c in convs
                 if not _is_excluded_project(
                     (c.get("project") or {}).get("name", "") if isinstance(c.get("project"), dict) else "",
                     _exc_names, _exc_kws)]
        dropped = before - len(convs)
        if dropped:
            log(f"[cowork_fetch] {org_name}: excluded {dropped} convs from "
                f"personal Claude projects (per cowork_exclude.json)", quiet)

    cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
    relevant = []
    for c in convs:
        upd = c.get("updated_at") or c.get("created_at") or ""
        try:
            dt = datetime.fromisoformat(upd.replace("Z", "+00:00"))
            if dt >= cutoff:
                relevant.append(c)
        except Exception:
            relevant.append(c)
    log(f"[cowork_fetch] {org_name}: {len(relevant)} updated in last {since_days} days", quiet)

    if max_convs:
        relevant = relevant[:max_convs]

    full_convs = []
    failed = 0
    for i, c in enumerate(relevant, 1):
        cuuid = c.get("uuid")
        if not cuuid:
            continue
        full = fetch_conversation(s, org_uuid, cuuid)
        if not full:
            failed += 1
            continue
        nc = normalize_conversation(full, list_meta=c)
        nc["_org_name"] = org_name  # trace where it came from
        full_convs.append(nc)
        if i % 10 == 0:
            log(f"  ...{org_name}: {i}/{len(relevant)}", quiet)
        time.sleep(0.3)

    projects = fetch_projects(s, org_uuid)
    return full_convs, projects, failed


def fetch_all(since_days: int = 30, quiet: bool = False, max_convs: int | None = None) -> dict:
    cfg = load_config()
    if not cfg or not cfg.get("session_key"):
        msg = "[cowork_fetch] No session config. Run: python scripts/cowork_fetch.py --setup"
        log(msg, quiet)
        return {"error": "no_config"}

    session_key = cfg["session_key"]
    s = make_session(session_key)

    # Always discover ALL orgs the cookie has access to and fetch from all of them.
    # The user's estimating chats might be in either workspace; better to grab everything.
    try:
        orgs = fetch_organizations(s)
    except PermissionError:
        log("[cowork_fetch] AUTH FAILED — cookie expired", quiet)
        tg_send(
            "🔑 *Carol cowork sync expired*\n\n"
            "Your claude.ai sessionKey has expired (~30 days).\n"
            "Refresh it once and you're set for another month:\n\n"
            "`python scripts/cowork_fetch.py --setup`"
        )
        return {"error": "auth_expired"}
    except Exception as e:
        log(f"[cowork_fetch] Network error: {e}", quiet)
        return {"error": f"network: {e}"}

    if not orgs:
        return {"error": "no_orgs"}

    log(f"[cowork_fetch] Fetching from {len(orgs)} workspace(s)", quiet)

    all_convs = []
    all_projects = []
    total_failed = 0
    for org in orgs:
        org_uuid = org.get("uuid", "")
        org_name = org.get("name", "(unnamed)")
        if not org_uuid:
            continue
        convs, projs, failed = _fetch_one_org(s, org_uuid, org_name, since_days, max_convs, quiet)
        all_convs.extend(convs)
        all_projects.extend(projs)
        total_failed += failed

    # Write all conversations + projects to one dated folder
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    out_dir = IMPORTS_DIR / f"auto_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "conversations.json").write_text(
        json.dumps(all_convs, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (out_dir / "projects.json").write_text(
        json.dumps(all_projects, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    cfg["last_fetch"] = datetime.now().isoformat(timespec="seconds")
    cfg["last_fetched_count"] = len(all_convs)
    cfg["last_orgs_count"] = len(orgs)
    save_config(cfg)
    _cleanup_old_auto_folders(IMPORTS_DIR, days=30)

    log(
        f"[cowork_fetch] Wrote {len(all_convs)} conversations + {len(all_projects)} projects → {out_dir.name}"
        + (f" ({total_failed} failed)" if total_failed else ""),
        quiet,
    )
    return {
        "ok": True,
        "fetched": len(all_convs),
        "failed": total_failed,
        "projects": len(all_projects),
        "orgs": len(orgs),
        "output": str(out_dir),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--setup", action="store_true", help="Interactive cookie capture")
    ap.add_argument("--since-days", type=int, default=30,
                    help="Only fetch conversations updated in the last N days")
    ap.add_argument("--max", type=int, default=None,
                    help="Cap number of conversations (testing)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if args.setup:
        return setup_interactive()

    result = fetch_all(since_days=args.since_days, quiet=args.quiet, max_convs=args.max)
    if args.quiet:
        if result.get("ok"):
            print(f"cowork_fetch: ok fetched={result['fetched']} failed={result.get('failed',0)}")
        else:
            print(f"cowork_fetch: {result.get('error','unknown error')}")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
