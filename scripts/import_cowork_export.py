#!/usr/bin/env python3
r"""
import_cowork_export.py — Import claude.ai data exports into Carol's MemPalace.

How to use:
  1. On claude.ai → Settings → Privacy → "Export data" → wait for email → download zip
  2. Drop the zip (or extracted folder) into:  C:\Agent Carol\data\cowork_imports\
  3. Run:    python scripts/import_cowork_export.py
     or let the daemon do it automatically.

What it does:
  - Parses conversations.json and projects.json from the export
  - Fuzzy-matches each conversation to an active bid by project name keywords
  - Writes per-project markdown digests to mempalace/wings/cowork/{slug}.md
  - Stores full transcripts as JSON next to the digest for drill-down
  - Tracks processed export ids in state.json to avoid re-importing

Re-running with the same file is safe (idempotent — refreshes existing digests).
"""

import argparse
import difflib
import hashlib
import json
import re
import sys
import zipfile
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
IMPORTS_DIR = ROOT / "data" / "cowork_imports"
RAW_DIR     = IMPORTS_DIR / "raw"
STATE_FILE  = IMPORTS_DIR / "state.json"
COWORK_WING = ROOT / "mempalace" / "wings" / "cowork"
ACTIVE_BIDS = ROOT / "data" / "memory" / "active_bids.json"
LOG_FILE    = ROOT / "data" / "logs" / "import_cowork.log"

# Min chars in a conversation message to be worth indexing
MIN_MSG_LEN = 30
# Min fuzzy score to call a match (0.0-1.0)
MATCH_THRESHOLD = 0.55
# Always-indexed: messages containing these get pulled into the digest verbatim
INTEREST_PATTERNS = [
    r"\$\s*[\d,]+",                    # dollar amounts
    r"\b\d+[,]?\d*\s*(SF|sf|LF|lf|sq\s*ft|gal|gallons?)\b",  # measurements
    r"\b(takeoff|scope|sow|markup|labor|material|crew|prevailing\s+wage)\b",
    r"\b(submit|won|lost|award|decision|deadline)\b",
]
INTEREST_RE = re.compile("|".join(INTEREST_PATTERNS), re.IGNORECASE)


def normalize(s: str) -> str:
    if not s: return ""
    s = s.lower()
    s = re.sub(r"#\s*\d+", "", s)
    s = re.sub(r"[(),\-/_]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9\s-]", "", (name or "").lower())
    s = re.sub(r"\s+", "-", s).strip("-")
    return re.sub(r"-+", "-", s)[:80]


def match_score(bid_name: str, search_text: str) -> float:
    """Match a bid name against arbitrary search text.

    Asymmetric: bid_name is short (a few words), search_text may be long
    (conversation title + body chunk). We want to know whether the bid
    name appears in the text, not how similar two strings are overall.
    """
    nb, nt = normalize(bid_name), normalize(search_text)
    if not nb or not nt: return 0.0

    # Strongest signal: bid name appears verbatim in search text
    if nb in nt:
        return 1.0

    # Token-overlap: how many of the bid's significant tokens appear in the text?
    bid_tokens = [t for t in nb.split() if len(t) >= 4]
    if not bid_tokens:
        return 0.0
    text_tokens = set(nt.split())
    matched = sum(1 for t in bid_tokens if t in text_tokens)
    overlap = matched / len(bid_tokens)

    # Bonus: shared 4-digit numbers (store numbers, addresses)
    nums_b = set(re.findall(r"\d{3,}", bid_name or ""))
    nums_t = set(re.findall(r"\d{3,}", search_text or ""))
    num_bonus = 0.2 if (nums_b and (nums_b & nums_t)) else 0.0

    return min(overlap + num_bonus, 1.0)


def load_state() -> dict:
    if STATE_FILE.exists():
        try: return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception: pass
    return {"processed": {}}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def file_id(path: Path) -> str:
    """Stable id from filename + size — exports are dated, contents reproducible."""
    h = hashlib.sha1(f"{path.name}:{path.stat().st_size}".encode()).hexdigest()[:12]
    return h


def extract_zip(zip_path: Path) -> Path:
    """Extract a claude.ai export zip into raw/{name}/ and return that path."""
    target = RAW_DIR / zip_path.stem
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(target)
    return target


def find_export_roots() -> list[Path]:
    """Find every unprocessed export — zips and pre-extracted folders."""
    if not IMPORTS_DIR.exists(): return []
    roots = []
    # Zips
    for z in IMPORTS_DIR.glob("*.zip"):
        roots.append(z)
    # Pre-extracted folders containing conversations.json
    for d in IMPORTS_DIR.iterdir():
        if d.is_dir() and d.name not in ("raw",) and (d / "conversations.json").exists():
            roots.append(d)
    # Also scan inside raw/ in case user extracted manually
    if RAW_DIR.exists():
        for d in RAW_DIR.iterdir():
            if d.is_dir() and (d / "conversations.json").exists():
                roots.append(d)
    return roots


def parse_export(folder: Path) -> tuple[list, list]:
    """Return (conversations, projects) from an extracted export folder."""
    conv_file = folder / "conversations.json"
    proj_file = folder / "projects.json"
    convs = json.loads(conv_file.read_text(encoding="utf-8")) if conv_file.exists() else []
    projs = json.loads(proj_file.read_text(encoding="utf-8")) if proj_file.exists() else []
    return convs, projs


def conv_to_text(conv: dict) -> tuple[str, list[str]]:
    """Flatten a conversation's messages. Returns (full_concat, per_message_list)."""
    parts = []
    for m in conv.get("chat_messages", []):
        sender = m.get("sender", "?")
        ts = (m.get("created_at") or "")[:10]
        # Body can be in 'text' or list of content blocks
        body = m.get("text") or ""
        if not body and isinstance(m.get("content"), list):
            for blk in m["content"]:
                if isinstance(blk, dict):
                    body += blk.get("text", "") + " "
        body = body.strip()
        if body:
            parts.append(f"[{ts} {sender}] {body}")
    full = "\n\n".join(parts)
    return full, parts


def best_project_match(conv: dict, full_text: str, active_bids: list) -> tuple[dict | None, float]:
    """Match conversation to an active bid by name + body content."""
    title = conv.get("name", "") or ""
    # Search target = title weighted heavily + first 500 chars of body
    target = (title + " " + title + " " + full_text[:500]).strip()
    best, best_score = None, 0.0
    for bid in active_bids:
        score = match_score(bid.get("project_name", ""), target)
        if score > best_score:
            best_score, best = score, bid
    return (best, best_score) if best_score >= MATCH_THRESHOLD else (None, best_score)


def build_digest(conv: dict, parts: list[str]) -> str:
    """Pick out the high-signal lines for the digest."""
    title = conv.get("name", "(untitled)")
    created = (conv.get("created_at") or "")[:10]
    updated = (conv.get("updated_at") or "")[:10]
    n = len(parts)
    interesting = [p for p in parts if INTEREST_RE.search(p) and len(p) >= MIN_MSG_LEN]
    # Cap to avoid bloating mempalace
    interesting = interesting[:25]

    lines = [
        f"### {title}",
        f"_dates: {created} → {updated} · {n} messages_",
        "",
    ]
    if interesting:
        lines.append("**Key excerpts:**")
        for p in interesting:
            # Truncate each line to keep digest skimmable
            snippet = p if len(p) <= 400 else p[:397] + "..."
            lines.append(f"- {snippet}")
    else:
        # Fallback: first user message + last assistant message
        first = next((p for p in parts if "[".lower() and "human" in p[:30].lower()), parts[0] if parts else "")
        last  = next((p for p in reversed(parts) if "assistant" in p[:30].lower()), parts[-1] if parts else "")
        if first: lines.append(f"- (open) {first[:300]}")
        if last and last != first: lines.append(f"- (close) {last[:300]}")
    lines.append("")
    return "\n".join(lines)


def write_project_file(slug: str, project_name: str, digests: list[str], full_convs: list[dict]):
    """Write per-project digest .md and full transcripts .json."""
    COWORK_WING.mkdir(parents=True, exist_ok=True)
    md_path = COWORK_WING / f"{slug}.md"
    json_path = COWORK_WING / f"{slug}.transcripts.json"

    header = (
        f"# Cowork sessions — {project_name}\n"
        f"_synced: {datetime.now().strftime('%Y-%m-%d %H:%M')}_  \n"
        f"_source: claude.ai export → Carol MemPalace_  \n"
        f"_full transcripts: {json_path.name}_\n\n"
        f"---\n\n"
    )
    md_path.write_text(header + "\n".join(digests), encoding="utf-8")
    json_path.write_text(json.dumps(full_convs, indent=2, ensure_ascii=False), encoding="utf-8")


def write_unmatched_file(unmatched: list[tuple[dict, list[str]]]):
    """Conversations that didn't match any active bid still get saved for browsing."""
    if not unmatched: return
    path = COWORK_WING / "_unmatched.md"
    parts = [
        "# Cowork sessions — unmatched\n",
        "_Conversations that didn't match any active bid by name. May still be relevant — review manually._\n",
        f"_synced: {datetime.now().strftime('%Y-%m-%d %H:%M')}_\n\n---\n",
    ]
    for conv, msg_parts in unmatched[:200]:  # cap to keep file sane
        parts.append(build_digest(conv, msg_parts))
    path.write_text("\n".join(parts), encoding="utf-8")


def log(msg: str):
    print(msg)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now().isoformat(timespec='seconds')}  {msg}\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="Re-process exports already marked done")
    ap.add_argument("--threshold", type=float, default=MATCH_THRESHOLD,
                    help="Match threshold 0-1 (default 0.55)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    state = load_state()
    processed = state.setdefault("processed", {})

    roots = find_export_roots()
    if not roots:
        log(f"[cowork] No exports found in {IMPORTS_DIR}. Drop the claude.ai export zip there.")
        return

    active_bids = json.loads(ACTIVE_BIDS.read_text(encoding="utf-8")) if ACTIVE_BIDS.exists() else []
    log(f"[cowork] {len(active_bids)} active bids loaded for matching")

    # === Project exclude filter ===
    # Carol indexes Claude Cowork conversations for CCF estimating recall. The
    # user's PERSONAL Claude projects (Cable Venture, Migra Bot, TikTok stuff,
    # World Cup, etc.) have nothing to do with CCF and just pollute Carol's
    # memory + the Telegram digest. Driven by data/config/cowork_exclude.json
    # so the user edits the list without touching code.
    _exc_cfg = ROOT / "data" / "config" / "cowork_exclude.json"
    _exc_names = set()
    _exc_kws = set()
    if _exc_cfg.exists():
        try:
            _cfg = json.load(open(_exc_cfg, encoding="utf-8"))
            _exc_names = {n.strip().lower() for n in _cfg.get("exclude_project_names", []) if n}
            _exc_kws  = {k.strip().lower() for k in _cfg.get("exclude_project_keywords", []) if k}
        except Exception:
            pass

    def _is_excluded_project(name: str) -> bool:
        n = (name or "").strip().lower()
        if not n:
            return False
        if n in _exc_names:
            return True
        return any(k in n for k in _exc_kws)

    all_convs = []  # (conv, msg_parts, project_match, score)
    seen_conv_ids = set()  # de-dupe across multiple exports
    skipped_excluded = 0
    skipped_by_project = {}  # project_name -> count, for logging

    for root in roots:
        rid = file_id(root)
        if rid in processed and not args.force:
            log(f"[cowork] skip (already processed): {root.name}")
            continue

        # Extract zip if needed
        if root.is_file() and root.suffix.lower() == ".zip":
            log(f"[cowork] extracting {root.name}...")
            folder = extract_zip(root)
        else:
            folder = root

        convs, projs = parse_export(folder)
        log(f"[cowork] {root.name}: {len(convs)} conversations, {len(projs)} projects")

        for conv in convs:
            cid = conv.get("uuid") or conv.get("id") or hashlib.sha1(json.dumps(conv, sort_keys=True).encode()).hexdigest()[:16]
            if cid in seen_conv_ids: continue
            seen_conv_ids.add(cid)
            # Skip personal Claude Projects (Cable Venture, Migra Bot, TikTok, etc.)
            _proj = ""
            _p = conv.get("project")
            if isinstance(_p, dict):
                _proj = (_p.get("name") or "").strip()
            if not _proj:
                _proj = (conv.get("claude_project") or "").strip()
            if _is_excluded_project(_proj):
                skipped_excluded += 1
                skipped_by_project[_proj] = skipped_by_project.get(_proj, 0) + 1
                continue
            full_text, parts = conv_to_text(conv)
            if not parts: continue
            match, score = best_project_match(conv, full_text, active_bids)
            all_convs.append((conv, parts, match, score))

        processed[rid] = {
            "source": str(root),
            "imported_at": datetime.now().isoformat(timespec="seconds"),
            "conversations": len(convs),
        }

    # Group by (a) matched active bid, (b) Claude Project name
    by_bid: dict[str, dict] = {}
    by_claude_project: dict[str, dict] = {}
    unmatched: list[tuple[dict, list[str]]] = []

    for conv, parts, match, score in all_convs:
        digest = build_digest(conv, parts)
        full_record = {
            "uuid": conv.get("uuid"),
            "name": conv.get("name"),
            "created_at": conv.get("created_at"),
            "updated_at": conv.get("updated_at"),
            "claude_project": conv.get("claude_project", ""),
            "messages": conv.get("chat_messages", []),
            "matched_score": score,
        }

        # (a) File under matched active bid (if any)
        if match:
            pname = match.get("project_name", "")
            slug = slugify(pname)
            bucket = by_bid.setdefault(slug, {"name": pname, "digests": [], "full": []})
            bucket["digests"].append(f"_match score: {score:.2f}_\n\n" + digest)
            bucket["full"].append(full_record)

        # (b) Also file under Claude Project name if conversation is in one
        cp_name = (conv.get("claude_project") or "").strip()
        if cp_name:
            cslug = "_project-" + slugify(cp_name)
            cb = by_claude_project.setdefault(cslug, {"name": cp_name, "digests": [], "full": []})
            cb["digests"].append(digest)
            cb["full"].append(full_record)

        # If neither matched a bid nor in a Claude project, mark unmatched
        if not match and not cp_name:
            unmatched.append((conv, parts))

    # Write all three buckets
    for slug, data in by_bid.items():
        write_project_file(slug, data["name"], data["digests"], data["full"])
    for slug, data in by_claude_project.items():
        write_project_file(slug, f"Claude Project — {data['name']}",
                           data["digests"], data["full"])
    write_unmatched_file(unmatched)

    save_state(state)

    log(f"[cowork] Indexed {len(all_convs)} conversations → "
        f"{len(by_bid)} bid-matches, {len(by_claude_project)} Claude projects, {len(unmatched)} unmatched")
    if skipped_excluded:
        sk = ", ".join(f"{n}={c}" for n, c in sorted(skipped_by_project.items(),
                                                      key=lambda kv: -kv[1])[:6])
        log(f"[cowork] EXCLUDED {skipped_excluded} convs from personal projects ({sk}) "
            f"per data/config/cowork_exclude.json")
    log(f"[cowork] Output: {COWORK_WING}")

    # Ping Telegram if any meaningful content landed
    if (by_bid or by_claude_project) and len(all_convs) > 0:
        try:
            import os, requests
            bot = os.environ.get("TELEGRAM_BOT_TOKEN", "")
            chat = os.environ.get("USER_TELEGRAM_CHAT_ID", "")
            lines = [f"🧠 *Cowork memory updated*",
                     f"{len(all_convs)} conversations indexed"]
            if by_bid:
                top_bids = sorted(by_bid.items(), key=lambda kv: -len(kv[1]["digests"]))[:3]
                lines.append(f"\n*Matched to active bids:*")
                for slug, data in top_bids:
                    lines.append(f"  • {data['name'][:40]} ({len(data['digests'])} sessions)")
            if by_claude_project:
                top_cw = sorted(by_claude_project.items(), key=lambda kv: -len(kv[1]["digests"]))[:5]
                lines.append(f"\n*Claude Projects (Cowork):*")
                for slug, data in top_cw:
                    lines.append(f"  • {data['name'][:30]} ({len(data['digests'])} sessions)")
            requests.post(
                f"https://api.telegram.org/bot{bot}/sendMessage",
                json={"chat_id": chat, "text": "\n".join(lines), "parse_mode": "Markdown"},
                timeout=10,
            )
        except Exception:
            pass


if __name__ == "__main__":
    main()
