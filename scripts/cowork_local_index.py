#!/usr/bin/env python3
r"""
cowork_local_index.py — Index Claude Cowork (desktop app's scheduled/background
Claude Code sessions) into Carol's MemPalace.

Cowork sessions are stored locally on the user's machine. Each session has:
  - Metadata at: %APPDATA%\Claude\claude-code-sessions\{user}\{org}\local_*.json
  - Transcript at: ~/.claude/projects/{cwd-slug}/{cliSessionId}.jsonl

This script:
  1. Walks every metadata file
  2. Loads the matching transcript JSONL
  3. Extracts user/assistant messages
  4. Groups sessions by scheduledTaskId (when present) or working directory
  5. Also fuzzy-matches each session to active_bids.json projects
  6. Writes per-bucket digests to mempalace/wings/cowork/

Run:
  python scripts/cowork_local_index.py            # full re-index
  python scripts/cowork_local_index.py --quiet
  python scripts/cowork_local_index.py --since-days 60
"""

import argparse
import difflib
import hashlib
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
APPDATA = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
COWORK_META_ROOT = APPDATA / "Claude" / "claude-code-sessions"
TRANSCRIPTS_ROOT = Path.home() / ".claude" / "projects"
COWORK_WING = ROOT / "mempalace" / "wings" / "cowork"
ACTIVE_BIDS = ROOT / "data" / "memory" / "active_bids.json"
LOG_FILE = ROOT / "data" / "logs" / "cowork_local.log"

# Cap content sizes to keep mempalace files skimmable
MAX_MSG_CHARS = 800            # per single message
MAX_SESSION_DIGEST_CHARS = 6000  # per session summary
MAX_KEY_EXCERPTS = 20          # per session

# Patterns we consider "high-signal" lines for the digest
INTEREST_PATTERNS = [
    r"\$\s*[\d,]+",
    r"\b\d+[,]?\d*\s*(?:SF|sf|LF|lf|sq\s*ft|gal|gallons?)\b",
    r"\b(takeoff|scope|sow|markup|labor|material|crew|prevailing\s+wage|gc|general\s+contractor)\b",
    r"\b(submit|won|lost|award|decision|deadline|due\s+date|invitation)\b",
    r"\b(estimate|proposal|bid)\b",
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
    """Same matcher as import_cowork_export — substring + token overlap."""
    nb, nt = normalize(bid_name), normalize(search_text)
    if not nb or not nt: return 0.0
    if nb in nt:
        return 1.0
    bid_tokens = [t for t in nb.split() if len(t) >= 4]
    if not bid_tokens:
        return 0.0
    text_tokens = set(nt.split())
    matched = sum(1 for t in bid_tokens if t in text_tokens)
    overlap = matched / len(bid_tokens)
    nums_b = set(re.findall(r"\d{3,}", bid_name or ""))
    nums_t = set(re.findall(r"\d{3,}", search_text or ""))
    bonus = 0.2 if (nums_b and (nums_b & nums_t)) else 0.0
    return min(overlap + bonus, 1.0)


def cwd_to_project_slug(cwd: str) -> str:
    """Replicate Claude Code's cwd → project-dir slug rule."""
    if not cwd:
        return ""
    # Claude Code converts e.g. C:\Agent Carol → C--Agent-Carol
    s = cwd.replace(":", "-").replace("\\", "-").replace("/", "-").replace(" ", "-")
    return s


def find_transcript(cwd: str, cli_session_id: str) -> Path | None:
    """Locate the JSONL transcript for a session."""
    if not cli_session_id:
        return None
    # Try the slug form first
    project_slug = cwd_to_project_slug(cwd)
    candidate = TRANSCRIPTS_ROOT / project_slug / f"{cli_session_id}.jsonl"
    if candidate.exists():
        return candidate
    # Fallback: scan all project dirs for the file
    if TRANSCRIPTS_ROOT.exists():
        for proj in TRANSCRIPTS_ROOT.iterdir():
            if proj.is_dir():
                hit = proj / f"{cli_session_id}.jsonl"
                if hit.exists():
                    return hit
    return None


def parse_transcript(path: Path) -> list[dict]:
    """Parse a Claude Code JSONL transcript into a flat list of {sender,text,ts} dicts."""
    out = []
    if not path or not path.exists():
        return out
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = rec.get("type", "")
                if t not in ("user", "assistant"):
                    continue
                msg = rec.get("message") or {}
                # Content may be str or list of blocks
                content = msg.get("content", "")
                if isinstance(content, list):
                    parts = []
                    for blk in content:
                        if isinstance(blk, dict):
                            if blk.get("type") == "text":
                                parts.append(blk.get("text", ""))
                            elif blk.get("type") == "tool_use":
                                # Skip noisy tool calls but note their intent
                                tname = blk.get("name", "tool")
                                parts.append(f"[tool: {tname}]")
                    text = " ".join(p for p in parts if p).strip()
                else:
                    text = str(content).strip()
                if not text:
                    continue
                out.append({
                    "sender": t,
                    "text": text[:MAX_MSG_CHARS],
                    "ts": rec.get("timestamp") or msg.get("created_at") or "",
                })
    except Exception:
        pass
    return out


def session_digest(meta: dict, transcript: list[dict]) -> tuple[str, str]:
    """Build a markdown digest for a single Cowork session.

    Returns (digest_md, full_text_for_matching).
    """
    title = meta.get("title", "(untitled)")
    sched = meta.get("scheduledTaskId") or ""
    cwd = meta.get("cwd", "")
    created = meta.get("createdAt", 0)
    last = meta.get("lastActivityAt", 0)
    model = meta.get("model", "")
    perm = meta.get("permissionMode", "")
    n = len(transcript)

    # Convert timestamps (ms) to dates
    def to_date(ms):
        try:
            return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")
        except Exception:
            return ""
    cdate = to_date(created)
    ldate = to_date(last)

    # Build full text for matching (titles + first 2000 chars of body)
    full_text_parts = [title]
    if sched:
        full_text_parts.append(sched)
    body_concat = " ".join(m["text"] for m in transcript)[:5000]
    full_text_parts.append(body_concat)
    full_text = " ".join(full_text_parts)

    # Pick high-signal excerpts
    interesting = [m for m in transcript
                   if INTEREST_RE.search(m["text"]) and len(m["text"]) >= 30]
    interesting = interesting[:MAX_KEY_EXCERPTS]
    if not interesting and transcript:
        # Fallback: first user prompt + last assistant reply
        first_user = next((m for m in transcript if m["sender"] == "user"), None)
        last_asst = next((m for m in reversed(transcript) if m["sender"] == "assistant"), None)
        interesting = [m for m in [first_user, last_asst] if m]

    # Build markdown
    lines = [f"### {title}"]
    meta_bits = [f"_dates: {cdate} → {ldate}_", f"_messages: {n}_"]
    if sched: meta_bits.append(f"_task: `{sched}`_")
    if cwd: meta_bits.append(f"_cwd: `{cwd}`_")
    if model: meta_bits.append(f"_model: {model}_")
    lines.append(" · ".join(meta_bits))
    lines.append("")
    if interesting:
        lines.append("**Key excerpts:**")
        chars_so_far = 0
        for m in interesting:
            snippet = m["text"]
            if len(snippet) > 400:
                snippet = snippet[:397] + "..."
            line = f"- *{m['sender']}*: {snippet}"
            if chars_so_far + len(line) > MAX_SESSION_DIGEST_CHARS:
                lines.append(f"_…digest truncated, see transcripts.json for full_")
                break
            lines.append(line)
            chars_so_far += len(line)
    lines.append("")
    return "\n".join(lines), full_text


def best_bid_match(full_text: str, active_bids: list, threshold: float = 0.55) -> tuple[dict | None, float]:
    best, best_score = None, 0.0
    for bid in active_bids:
        s = match_score(bid.get("project_name", ""), full_text)
        if s > best_score:
            best_score, best = s, bid
    return (best, best_score) if best_score >= threshold else (None, best_score)


def write_md(slug: str, title: str, digests: list[str], full_records: list[dict]):
    COWORK_WING.mkdir(parents=True, exist_ok=True)
    md_path = COWORK_WING / f"{slug}.md"
    json_path = COWORK_WING / f"{slug}.transcripts.json"
    header = (
        f"# {title}\n"
        f"_synced: {datetime.now().strftime('%Y-%m-%d %H:%M')}_  \n"
        f"_source: Claude Cowork (local desktop app sessions)_  \n"
        f"_full transcripts: {json_path.name}_\n\n"
        f"---\n\n"
    )
    md_path.write_text(header + "\n".join(digests), encoding="utf-8")
    json_path.write_text(json.dumps(full_records, indent=2, ensure_ascii=False), encoding="utf-8")


def log(msg: str, quiet: bool = False):
    if not quiet:
        print(msg)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now().isoformat(timespec='seconds')}  {msg}\n")


def find_all_metadata() -> list[Path]:
    """Walk every owner/org folder under claude-code-sessions and return all local_*.json files."""
    if not COWORK_META_ROOT.exists():
        return []
    out = []
    for owner_dir in COWORK_META_ROOT.iterdir():
        if not owner_dir.is_dir(): continue
        for org_dir in owner_dir.iterdir():
            if not org_dir.is_dir(): continue
            out.extend(org_dir.glob("local_*.json"))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since-days", type=int, default=180,
                    help="Only index sessions with lastActivityAt within N days (default 180)")
    ap.add_argument("--threshold", type=float, default=0.55)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if not COWORK_META_ROOT.exists():
        log(f"[cowork-local] Cowork session dir not found: {COWORK_META_ROOT}", args.quiet)
        return 1

    meta_files = find_all_metadata()
    log(f"[cowork-local] Found {len(meta_files)} session metadata files", args.quiet)
    if not meta_files:
        return 0

    cutoff_ms = (datetime.now() - timedelta(days=args.since_days)).timestamp() * 1000
    active_bids = json.loads(ACTIVE_BIDS.read_text(encoding="utf-8")) if ACTIVE_BIDS.exists() else []
    log(f"[cowork-local] {len(active_bids)} active bids loaded for matching", args.quiet)

    # Buckets:
    #   by_task[scheduledTaskId] = [(meta, transcript), ...]
    #   by_cwd[cwd]              = [(meta, transcript), ...]  (sessions w/o scheduledTaskId)
    #   by_bid[bid_slug]         = [(meta, transcript, score), ...]
    by_task = defaultdict(list)
    by_cwd = defaultdict(list)
    by_bid = defaultdict(list)
    skipped_old = 0
    skipped_no_transcript = 0
    indexed = 0

    for mp in meta_files:
        try:
            meta = json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            continue
        last = meta.get("lastActivityAt") or meta.get("createdAt") or 0
        if last < cutoff_ms:
            skipped_old += 1
            continue
        cli_id = meta.get("cliSessionId")
        cwd = meta.get("cwd", "")
        transcript_path = find_transcript(cwd, cli_id)
        transcript = parse_transcript(transcript_path) if transcript_path else []
        if not transcript:
            skipped_no_transcript += 1
            # Still keep the metadata-only record under its bucket
        indexed += 1

        sched = (meta.get("scheduledTaskId") or "").strip()
        if sched:
            by_task[sched].append((meta, transcript))
        elif cwd:
            by_cwd[cwd].append((meta, transcript))

        # Bid matching: try title + scheduledTaskId + transcript text
        digest_md, full_text = session_digest(meta, transcript)
        match, score = best_bid_match(full_text, active_bids, threshold=args.threshold)
        if match:
            by_bid[slugify(match["project_name"])].append({
                "name": match["project_name"],
                "meta": meta,
                "transcript": transcript,
                "score": score,
                "digest": digest_md,
            })

    # Write task-grouped files
    written = 0
    for task_id, sessions in by_task.items():
        slug = "_cowork-task-" + slugify(task_id)
        digests = []
        full_records = []
        sessions.sort(key=lambda s: s[0].get("lastActivityAt", 0), reverse=True)
        for meta, transcript in sessions:
            d, _ = session_digest(meta, transcript)
            digests.append(d)
            full_records.append({
                "sessionId": meta.get("sessionId"),
                "cliSessionId": meta.get("cliSessionId"),
                "title": meta.get("title"),
                "scheduledTaskId": task_id,
                "cwd": meta.get("cwd"),
                "createdAt": meta.get("createdAt"),
                "lastActivityAt": meta.get("lastActivityAt"),
                "messages": transcript,
            })
        write_md(slug, f"Cowork Task — {task_id}", digests, full_records)
        written += 1

    # Write cwd-grouped files (one-off Cowork sessions w/o a scheduled task)
    for cwd, sessions in by_cwd.items():
        slug = "_cowork-adhoc-" + slugify(cwd_to_project_slug(cwd) or "unknown")
        digests = []
        full_records = []
        sessions.sort(key=lambda s: s[0].get("lastActivityAt", 0), reverse=True)
        for meta, transcript in sessions:
            d, _ = session_digest(meta, transcript)
            digests.append(d)
            full_records.append({
                "sessionId": meta.get("sessionId"),
                "cliSessionId": meta.get("cliSessionId"),
                "title": meta.get("title"),
                "cwd": meta.get("cwd"),
                "createdAt": meta.get("createdAt"),
                "lastActivityAt": meta.get("lastActivityAt"),
                "messages": transcript,
            })
        write_md(slug, f"Cowork Sessions (ad-hoc) — {cwd}", digests, full_records)
        written += 1

    # Write bid-matched files
    for bid_slug, items in by_bid.items():
        existing_md = COWORK_WING / f"{bid_slug}.md"
        existing_json = COWORK_WING / f"{bid_slug}.transcripts.json"
        # If a bid file already exists from claude.ai sync, append; otherwise create
        digests = [f"_match score: {it['score']:.2f}_\n\n{it['digest']}" for it in items]
        full_records = [{
            "source": "cowork_local",
            "sessionId": it["meta"].get("sessionId"),
            "cliSessionId": it["meta"].get("cliSessionId"),
            "title": it["meta"].get("title"),
            "score": it["score"],
            "messages": it["transcript"],
        } for it in items]
        # Check existing - merge if claude.ai sync wrote earlier
        if existing_md.exists():
            prev = existing_md.read_text(encoding="utf-8")
            new_section = (f"\n\n---\n\n## From Claude Cowork (local sessions)\n\n"
                           + "\n".join(digests))
            existing_md.write_text(prev + new_section, encoding="utf-8")
            # Merge transcripts JSON
            try:
                prev_json = json.loads(existing_json.read_text(encoding="utf-8"))
                if isinstance(prev_json, list):
                    prev_json.extend(full_records)
                    existing_json.write_text(json.dumps(prev_json, indent=2, ensure_ascii=False),
                                             encoding="utf-8")
            except Exception:
                existing_json.write_text(json.dumps(full_records, indent=2, ensure_ascii=False),
                                         encoding="utf-8")
        else:
            write_md(bid_slug, items[0]["name"], digests, full_records)
        written += 1

    log(f"[cowork-local] Indexed {indexed} sessions → "
        f"{len(by_task)} tasks, {len(by_cwd)} ad-hoc cwds, {len(by_bid)} bid-matches  "
        f"(skipped: {skipped_old} old, {skipped_no_transcript} no-transcript)", args.quiet)
    log(f"[cowork-local] Wrote {written} files → {COWORK_WING}", args.quiet)

    # Telegram ping when meaningful indexing happened
    if indexed > 0 and (by_task or by_bid):
        try:
            import requests
            bot = os.environ.get("TELEGRAM_BOT_TOKEN", "")
            chat = os.environ.get("USER_TELEGRAM_CHAT_ID", "")
            lines = [f"🧠 *Cowork (local) indexed*", f"{indexed} sessions"]
            if by_task:
                lines.append(f"\n*Top scheduled tasks:*")
                top = sorted(by_task.items(), key=lambda kv: -len(kv[1]))[:5]
                for tid, sess in top:
                    lines.append(f"  • {tid[:35]} ({len(sess)} runs)")
            if by_bid:
                lines.append(f"\n*Bid-matched:*")
                top = sorted(by_bid.items(), key=lambda kv: -len(kv[1]))[:3]
                for slug, items in top:
                    lines.append(f"  • {items[0]['name'][:35]} ({len(items)} sessions)")
            requests.post(
                f"https://api.telegram.org/bot{bot}/sendMessage",
                json={"chat_id": chat, "text": "\n".join(lines), "parse_mode": "Markdown"},
                timeout=10,
            )
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
