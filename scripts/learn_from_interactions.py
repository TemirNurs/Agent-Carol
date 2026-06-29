#!/usr/bin/env python3
r"""learn_from_interactions.py — Carol's automatic learning loop.

GOAL (Nursultan, 2026-06-26): "You have to learn from EVERYTHING, like humans do.
From me, from yourself — your own discoveries — from everything, always,
automatically! Build the god-level automatic learning system to turn Carol into
a god-level estimator."

WHAT IT LEARNS FROM (both sides of every exchange)
  1. FROM NURSULTAN — moments he corrected, taught, or praised something
     (his reaction to what Carol just did) -> corrections / preferences / praise.
  2. FROM CAROL HERSELF — techniques/insights she discovered solving a problem
     (her own work arc), e.g. "the GC's real email is in the BuildingConnected
     invite BODY, masked in the From header" -> reusable techniques / root-causes.

SOURCES
  - CLI session transcripts:  ~/.claude/projects/C--Agent-Carol/*.jsonl
  - Telegram team chats:       data/memory/team_conversations/nursultan_*.md

PIPELINE
  parse transcripts into EXCHANGES (user_msg -> Carol's solution arc -> next user_msg)
  -> derive two candidate streams:
       correction-candidates  (Nursultan's reaction to an arc)
       discovery-candidates   (a problem + the arc Carol used to solve it)
  -> regex prefilter (teaching signal | problem/discovery signal)
  -> LLM extraction (claude-code, strict schema, is_lesson gate + confidence)
  -> dedup vs existing AGENTS_LESSONS.md headings + ledger
  -> write: AGENTS_LESSONS.md (backed up, tagged, revertible) + ledger + digest [+ Telegram]

SAFETY
  - AGENTS_LESSONS.md is hand-curated & precious: always backed up first, appended
    ONLY under a clearly-marked auto-section, every entry tagged with its ledger id.
  - Dry-run by default; --apply to write. Cursor + dedup make re-runs idempotent.
  - Output scrubbed of anything resembling a secret before storage.

USAGE
  python scripts/learn_from_interactions.py                 # dry-run, incremental
  python scripts/learn_from_interactions.py --apply         # write new lessons
  python scripts/learn_from_interactions.py --session <id>  # one session, full re-read
  python scripts/learn_from_interactions.py --backfill --since 2026-06-01 --max 40
  python scripts/learn_from_interactions.py --kind discovery   # only mine self-discoveries
  python scripts/learn_from_interactions.py --revert <ledger_id>
"""
from __future__ import annotations
import argparse
import json
import os
import re
import sys
from datetime import datetime, date, timezone
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

# ---- paths ----------------------------------------------------------------
CLI_TRANSCRIPT_DIR = Path(os.environ.get("HOME") or os.environ.get("USERPROFILE") or
                          Path.home()) / ".claude" / "projects" / "C--Agent-Carol"
TEAM_DIR = ROOT / "data" / "memory" / "team_conversations"
LESSONS_MD = ROOT / "AGENTS_LESSONS.md"
CURSOR = ROOT / "data" / "memory" / "lessons_cursor.json"
LEDGER = ROOT / "data" / "memory" / "learned_lessons.json"
BACKUP_DIR = ROOT / "data" / "memory" / "backups"
DIGEST_DIR = ROOT / "data" / "memory"
AUTO_SECTION_MARK = "## 🤖 Auto-learned lessons (mined from interactions)"

# ---- tuning ---------------------------------------------------------------
MIN_CONFIDENCE = 0.72
DEDUP_TOKEN_OVERLAP = 0.62
MAX_PER_RUN_DEFAULT = 25
WORK_CHARS = 1600       # how much of Carol's solution arc to show the LLM
USER_MSG_CHARS = 1400

# Nursultan correcting / teaching / praising (lesson lives in HIS reaction).
SIGNAL = re.compile(
    r"\b(no+|wrong|incorrect|not what|don'?t|do not|never|always|every time|"
    r"from now on|going forward|you have to|you should|you must|make sure|"
    r"why (are|do|did|is|would|the fuck) you|why you|i meant|i told you|"
    r"i said|you forgot|forget(ting)?|again\b|stop\b|instead|fix it|"
    r"remember|make (this|it) a lesson|learn|lesson|you can'?t|"
    r"that'?s not|should(n'?t)? (be|have)|has to|must be|"
    r"dumb|stupid|idiot|imbecile|lazy|asshole|mothaf|fuck|shit|"
    r"good job|great job|perfect|exactly|nailed it|yes do that|do that|"
    r"that'?s right|correct\b|love it)\b",
    re.IGNORECASE,
)
STRONG = re.compile(r"make (this|it) a lesson|remember this|learn from|from now on|"
                    r"every ?day|automatically|from everything", re.IGNORECASE)

# A problem Carol was asked to figure out (discovery may live in HER solution).
PROBLEM_SIGNAL = re.compile(
    r"\b(how (do|can|to|did|would|should)|how'?d|find (it|the|that|me|my)|"
    r"figure (it )?out|i have no idea|no idea how|where (is|are|do|can)|"
    r"what'?s wrong|why (is|isn'?t|won'?t|does|doesn'?t|did)|can'?t (find|get|figure|see)|"
    r"stuck|debug|broken|not working|doesn'?t work|trace|root cause|"
    r"build (a|the|me|an|this|that)|crack|decode|solve|fix)\b",
    re.IGNORECASE,
)
# Carol's arc shows she discovered/used a non-obvious method.
DISCOVERY_SIGNAL = re.compile(
    r"\b(found it|turns out|root cause|the (trick|key|fix|reason|issue|catch|gotcha|method) "
    r"(is|was|here|=)|leaks? in|unmask|hidden in|actually (it'?s|the)|here'?s how|"
    r"workaround|figured out|the bug was|because the|masked|in the body|reusable|"
    r"for next time|works because|i discovered|verified (from|via|across)|"
    r"confirmed (from|via|across)|cross-check|the pattern (is|holds)|aha)\b",
    re.IGNORECASE,
)

SECRET_RE = re.compile(
    r"(?i)(password|passwd|api[_-]?key|secret|token|bearer|authorization)\s*[:=]\s*\S+")

# Additional PII/confidential scrubbers applied to every mined lesson field
# before it is written to the (gitignored) lessons file + ledger. Catches the
# things SECRET_RE misses: personal emails, phone numbers, and dollar figures
# that may surface verbatim in a quoted exchange. Company role addresses on the
# company's own domain are preserved (own-domain emails carry no third-party PII).
_OWN_DOMAINS = tuple(
    d.strip().lower()
    for d in os.environ.get("CCF_OWN_DOMAINS", "carolinacommercialfinishes").split(",")
    if d.strip()
)
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?1[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)")
MONEY_RE = re.compile(r"\$\s?\d{1,3}(?:,\d{3})+(?:\.\d{2})?|\$\s?\d{4,}(?:\.\d{2})?")


# ---------------------------------------------------------------------------
# transcript parsing -> exchanges -> candidates
# ---------------------------------------------------------------------------
def _msg_text(msg: dict) -> str:
    c = msg.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(b.get("text", "") for b in c
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _is_tool_result(msg: dict) -> bool:
    c = msg.get("content")
    return isinstance(c, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in c)


def _tool_names(msg: dict) -> list[str]:
    c = msg.get("content")
    if isinstance(c, list):
        return [b.get("name", "?") for b in c
                if isinstance(b, dict) and b.get("type") == "tool_use"]
    return []


def _genuine_user(text: str) -> bool:
    if not text or not text.strip():
        return False
    head = text.lstrip()[:40].lower()
    return not head.startswith(("<system-reminder", "caveat:", "[request interrupted",
                                "<command-", "<local-command"))


def _exchanges_from_file(fp: Path) -> list[dict]:
    """One pass -> list of exchanges. Each = a genuine Nursultan message plus the
    assistant solution-arc that FOLLOWS it (his next message ends the arc)."""
    out = []
    cur = None
    last_user = ""
    try:
        fh = fp.open(encoding="utf-8")
    except Exception:
        return out

    def _close(ex):
        if ex is None:
            return
        ex["work"] = ("\n".join(ex.pop("_arc"))[-WORK_CHARS:]).strip()
        ex["tools"] = sorted(ex.pop("_tools"))[:10]
        out.append(ex)

    with fh:
        for ln_no, raw in enumerate(fh):
            raw = raw.strip()
            if not raw:
                continue
            try:
                o = json.loads(raw)
            except Exception:
                continue
            typ = o.get("type")
            msg = o.get("message") if isinstance(o.get("message"), dict) else None
            if not msg:
                continue
            if typ == "assistant":
                if cur is not None:
                    t = _msg_text(msg).strip()
                    if t:
                        cur["_arc"].append(t)
                    cur["_tools"].update(_tool_names(msg))
                continue
            if typ == "user":
                if _is_tool_result(msg) or o.get("isMeta") or o.get("isCompactSummary"):
                    continue
                t = _msg_text(msg).strip()
                if not _genuine_user(t):
                    continue
                _close(cur)
                cur = {"line": ln_no, "ts": o.get("timestamp", ""),
                       "user_msg": t[:USER_MSG_CHARS], "prev_user": last_user[:400],
                       "_arc": [], "_tools": set()}
                last_user = t
    _close(cur)
    return out


def candidates_from_cli(only_session, since_dt, cursor, want_kinds) -> list[dict]:
    cands = []
    if not CLI_TRANSCRIPT_DIR.exists():
        return cands
    seen = cursor.get("sessions", {})
    for fp in sorted(CLI_TRANSCRIPT_DIR.glob("*.jsonl")):
        sid = fp.stem
        if only_session and sid != only_session:
            continue
        if since_dt is not None:
            try:
                if datetime.fromtimestamp(fp.stat().st_mtime, tz=timezone.utc) < since_dt:
                    continue
            except Exception:
                pass
        start_line = 0 if only_session else int(seen.get(sid, 0))
        exs = _exchanges_from_file(fp)
        max_line = start_line
        for i, ex in enumerate(exs):
            max_line = max(max_line, ex["line"] + 1)
            # DISCOVERY: this problem + the arc Carol used to solve it.
            if "discovery" in want_kinds and ex["line"] >= start_line and ex["work"]:
                cands.append({"kind": "discovery", "source": "cli", "session": sid,
                              "line": ex["line"], "ts": ex["ts"],
                              "trigger": ex["user_msg"], "work": ex["work"],
                              "tools": ex["tools"], "prev_user": ex["prev_user"]})
            # CORRECTION: Nursultan's NEXT message reacting to this arc.
            if "correction" in want_kinds and i + 1 < len(exs):
                nxt = exs[i + 1]
                if nxt["line"] >= start_line:
                    cands.append({"kind": "correction", "source": "cli", "session": sid,
                                  "line": nxt["line"], "ts": nxt["ts"],
                                  "trigger": nxt["user_msg"], "work": ex["work"],
                                  "tools": ex["tools"], "prev_user": ex["user_msg"][:400]})
        if not only_session:
            seen[sid] = max_line
    cursor["sessions"] = seen
    return cands


def candidates_from_team(since_dt, cursor, want_kinds) -> list[dict]:
    cands = []
    if "correction" not in want_kinds or not TEAM_DIR.exists():
        return cands
    done = set(cursor.get("team_files", []))
    for fp in sorted(TEAM_DIR.glob("nursultan*_*.*")):
        if since_dt is not None:
            try:
                if datetime.fromtimestamp(fp.stat().st_mtime, tz=timezone.utc) < since_dt:
                    continue
            except Exception:
                pass
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        prev_assistant = ""
        for raw in text.splitlines():
            low = raw.strip()
            if not low:
                continue
            m = re.match(r"^[\-\*\s>]*([A-Za-z][\w ]+?)\s*[:\-]\s*(.+)$", low)
            if not m:
                continue
            who, said = m.group(1).strip().lower(), m.group(2).strip()
            if who.startswith(("nursultan", "user", "you")):
                cands.append({"kind": "correction", "source": "telegram", "session": fp.name,
                              "line": 0, "ts": "", "trigger": said[:USER_MSG_CHARS],
                              "work": prev_assistant[-WORK_CHARS:], "tools": [], "prev_user": ""})
            elif who.startswith(("carol", "assistant", "bot")):
                prev_assistant = said
        done.add(fp.name)
    cursor["team_files"] = sorted(done)
    return cands


def prefilter(cands: list[dict]) -> list[dict]:
    out = []
    for c in cands:
        if c["kind"] == "correction":
            if STRONG.search(c["trigger"]) or SIGNAL.search(c["trigger"]):
                out.append(c)
        else:  # discovery
            if PROBLEM_SIGNAL.search(c["trigger"] or "") or DISCOVERY_SIGNAL.search(c["work"] or ""):
                out.append(c)
    return out


# ---------------------------------------------------------------------------
# LLM extraction
# ---------------------------------------------------------------------------
_SCHEMA = """Return ONLY a JSON object:
{
  "is_lesson": true|false,
  "type": "correction"|"technique"|"preference"|"praise"|"root_cause",
  "category": "takeoff"|"crm"|"email"|"chase"|"gmail"|"estimating"|"proposal"|"behavior"|"research"|"data"|"other",
  "title": "short imperative rule, <=90 chars",
  "rule": "the durable instruction, 1-3 sentences, second person",
  "why": "the incident in one sentence + a SHORT verbatim quote (<=20 words) if one exists",
  "how_to_apply": "concrete, mechanical step(s) future-Carol can follow",
  "dedup_key": "5-10 lowercase keywords, space-separated",
  "confidence": 0.0-1.0
}
NEVER include passwords, tokens, or API keys in any field."""

_CORRECTION_SYS = ("You distill durable LESSONS for an autonomous estimating agent (Carol) "
                   "from how its owner Nursultan reacts to its work. Be strict — most messages "
                   "are NOT lessons (one-off tasks, questions, chatter). A real lesson "
                   "generalizes beyond the specific bid/email. Output JSON only.")
_DISCOVERY_SYS = ("You find reusable TECHNIQUES, methods, and root-causes that Carol (an "
                  "autonomous estimating agent) DISCOVERED or USED while solving a problem, so "
                  "future-Carol can reuse them automatically. Be strict: routine actions with no "
                  "transferable insight are NOT lessons. A real one is non-obvious and "
                  "generalizes beyond this one case (e.g. a hidden data source, a parsing trick, "
                  "a verification method, a root-cause and its fix). Output JSON only.")


def extract_lesson(ep: dict) -> dict | None:
    try:
        from _lib import llm
    except Exception as e:
        return {"_error": f"llm import failed: {e}"}
    tools = ", ".join(ep.get("tools") or []) or "(none)"
    if ep["kind"] == "correction":
        sysmsg = _CORRECTION_SYS
        usermsg = (
            f"CONTEXT — what Carol had just done (tools: {tools}):\n"
            f'"""\n{ep["work"] or "(no prior assistant text)"}\n"""\n\n'
            f"NURSULTAN'S REACTION:\n"
            f'"""\n{ep["trigger"]}\n"""\n\n{_SCHEMA}\n'
            "If Nursultan is just assigning a task / asking / chatting: is_lesson=false.")
    else:
        sysmsg = _DISCOVERY_SYS
        usermsg = (
            f"THE PROBLEM Carol was working on:\n"
            f'"""\n{ep["trigger"]}\n"""\n\n'
            f"WHAT CAROL DID TO SOLVE IT (tools: {tools}):\n"
            f'"""\n{ep["work"] or "(no solution text captured)"}\n"""\n\n{_SCHEMA}\n'
            "Capture the reusable TECHNIQUE/insight/root-cause Carol used here. "
            "If it was a routine action with nothing reusable: is_lesson=false.")
    r = llm.chat_json(
        [{"role": "system", "content": sysmsg}, {"role": "user", "content": usermsg}],
        model="claude-code", max_tokens=700, temperature=0.2)
    if not isinstance(r, dict) or r.get("error"):
        return {"_error": (r or {}).get("error", "no result")}
    return r


def _scrub(s: str) -> str:
    s = SECRET_RE.sub(r"\1: [REDACTED]", s or "")

    def _email_sub(m):
        addr = m.group(0)
        low = addr.lower()
        # Keep the company's own role addresses (public business info); redact
        # any other (personal/team/GC) email that surfaced in a quote.
        if any(dom and dom in low for dom in _OWN_DOMAINS):
            return addr
        return "[EMAIL]"

    s = EMAIL_RE.sub(_email_sub, s)
    s = PHONE_RE.sub("[PHONE]", s)
    s = MONEY_RE.sub("$[AMOUNT]", s)
    return s


# ---------------------------------------------------------------------------
# dedup
# ---------------------------------------------------------------------------
_STOP = {"the", "a", "an", "to", "of", "and", "or", "for", "in", "on", "is", "be",
         "do", "not", "with", "from", "it", "this", "that", "you", "your", "when",
         "any", "all", "every", "must", "should", "always", "never", "carol"}


def _tokens(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (s or "").lower())) - _STOP


def _overlap(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def existing_keys() -> list[set[str]]:
    keys = []
    if LESSONS_MD.exists():
        for ln in LESSONS_MD.read_text(encoding="utf-8", errors="replace").splitlines():
            if ln.startswith(("## ", "### ")):
                keys.append(_tokens(ln.lstrip("# ")))
    if LEDGER.exists():
        try:
            for rec in json.loads(LEDGER.read_text(encoding="utf-8")).get("lessons", []):
                if rec.get("status") != "reverted":
                    keys.append(_tokens(rec.get("dedup_key", "") + " " + rec.get("title", "")))
        except Exception:
            pass
    return keys


def is_duplicate(lesson: dict, keys: list[set[str]]) -> bool:
    k = _tokens(lesson.get("dedup_key", "") + " " + lesson.get("title", ""))
    return any(_overlap(k, e) >= DEDUP_TOKEN_OVERLAP for e in keys)


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _backup_lessons():
    if not LESSONS_MD.exists():
        return
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    (BACKUP_DIR / f"AGENTS_LESSONS.{stamp}.bak.md").write_text(
        LESSONS_MD.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")


def _render_entry(rec: dict) -> str:
    return (
        f"\n### {rec['title']}\n"
        f"<!-- auto id={rec['id']} conf={rec['confidence']:.2f} kind={rec['kind']} "
        f"type={rec['type']} cat={rec['category']} src={rec['source']}:{rec['session']} "
        f"date={rec['learned_date']} -->\n\n"
        f"{rec['rule']}\n\n"
        f"**How to apply:** {rec['how_to_apply']}\n\n"
        f"_Why: {rec['why']}_\n")


def append_to_lessons(records: list[dict]):
    if not records:
        return
    _backup_lessons()
    text = (LESSONS_MD.read_text(encoding="utf-8", errors="replace")
            if LESSONS_MD.exists() else "# Carol — DO NOT REPEAT lessons\n")
    if AUTO_SECTION_MARK not in text:
        text = text.rstrip() + (
            f"\n\n---\n\n{AUTO_SECTION_MARK}\n\n"
            "_Lessons below were mined automatically from interactions by "
            "`learn_from_interactions.py` — both from Nursultan's corrections AND from "
            "Carol's own discoveries. Each carries a ledger id (see "
            "`data/memory/learned_lessons.json`). Revert a bad one with "
            "`python scripts/learn_from_interactions.py --revert <id>`._\n")
    text = text.rstrip() + "\n" + "".join(_render_entry(r) for r in records) + "\n"
    LESSONS_MD.write_text(text, encoding="utf-8")


def remove_from_lessons(ledger_id: str) -> bool:
    if not LESSONS_MD.exists():
        return False
    lines = LESSONS_MD.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    out, i, removed = [], 0, False
    while i < len(lines):
        if lines[i].startswith("### ") and i + 1 < len(lines) and f"id={ledger_id} " in lines[i + 1]:
            i += 1
            while i < len(lines) and not lines[i].startswith(("### ", "## ", "---")):
                i += 1
            removed = True
            continue
        out.append(lines[i]); i += 1
    if removed:
        LESSONS_MD.write_text("".join(out), encoding="utf-8")
    return removed


def load_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(p: Path, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def write_digest(new_recs, reinforced, scanned, candidates):
    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    d = date.today().isoformat()
    p = DIGEST_DIR / f"lessons_digest_{d}.md"
    nd = sum(1 for r in new_recs if r["kind"] == "discovery")
    lines = [f"# Carol learning digest — {d}", "",
             f"- scanned exchanges: {scanned}", f"- teaching/discovery candidates: {candidates}",
             f"- NEW lessons: {len(new_recs)}  ({len(new_recs)-nd} from Nursultan, {nd} self-discovered)",
             f"- reinforced (already known): {reinforced}", ""]
    for r in new_recs:
        lines += [f"## {r['title']}  ·  _{r['kind']}/{r['type']}/{r['category']}_  (conf {r['confidence']:.2f})",
                  r["rule"], f"**Apply:** {r['how_to_apply']}", f"_Why: {r['why']}_",
                  f"`id={r['id']}`  ·  src `{r['source']}:{r['session']}`", ""]
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# backfill: harvest candidates (no LLM) + ingest extracted lessons (single writer)
# ---------------------------------------------------------------------------
def do_harvest(out_path: str, since_dt) -> None:
    """Scan ALL transcripts, build + prefilter candidates, write them to a JSON
    file with stable indices. No LLM, no cursor mutation — this is the fast first
    half of a parallel backfill; the slow extraction is fanned out across agents."""
    throwaway = {"sessions": {}, "team_files": []}
    cands = candidates_from_cli(None, since_dt, throwaway, {"correction", "discovery"})
    cands += candidates_from_team(since_dt, throwaway, {"correction", "discovery"})
    scanned = len(cands)
    cands = prefilter(cands)
    for i, c in enumerate(cands):
        c["idx"] = i
    save_json(Path(out_path), {"generated": _now_iso(), "scanned": scanned,
                               "count": len(cands), "candidates": cands})
    nC = sum(1 for c in cands if c["kind"] == "correction")
    print(f"HARVEST: {scanned} exchange-candidates -> {len(cands)} after prefilter "
          f"({nC} correction, {len(cands) - nC} discovery) -> {out_path}")


def do_ingest(lessons_path: str) -> None:
    """Take a JSON list of LLM-extracted lessons (from the workflow agents), dedup
    against the whole existing corpus + within the batch, and append the genuinely
    new ones to AGENTS_LESSONS.md + ledger + digest. The ONLY writer in a backfill,
    so parallel agents never contend on the lessons file."""
    items = load_json(Path(lessons_path), [])
    if isinstance(items, dict):
        items = items.get("lessons") or items.get("candidates") or []
    keys = existing_keys()
    ledger = load_json(LEDGER, {"lessons": []})
    existing_ids = {r.get("id") for r in ledger.get("lessons", [])}
    new_recs, reinforced, run_keys = [], 0, []
    items = sorted(items, key=lambda x: float(x.get("confidence", 0) or 0), reverse=True)
    for res in items:
        if not res.get("is_lesson", True):
            continue
        if float(res.get("confidence", 0) or 0) < MIN_CONFIDENCE:
            continue
        if is_duplicate(res, keys) or is_duplicate(res, run_keys):
            reinforced += 1
            continue
        lid = f"LBF{date.today().strftime('%y%m%d')}-{len(existing_ids) + len(new_recs) + 1:03d}"
        rec = {
            "id": lid, "title": _scrub(res.get("title", ""))[:120],
            "rule": _scrub(res.get("rule", "")), "why": _scrub(res.get("why", "")),
            "how_to_apply": _scrub(res.get("how_to_apply", "")),
            "dedup_key": res.get("dedup_key", ""), "kind": res.get("kind", "backfill"),
            "type": res.get("type", "correction"), "category": res.get("category", "other"),
            "confidence": float(res.get("confidence", 0) or 0),
            "source": res.get("source", "backfill"), "session": res.get("session", "?"),
            "line": res.get("line", 0), "quote_ts": res.get("ts", ""),
            "learned_date": date.today().isoformat(), "learned_at": _now_iso(),
            "status": "active",
        }
        new_recs.append(rec)
        run_keys.append(_tokens(rec["dedup_key"] + " " + rec["title"]))
    append_to_lessons(new_recs)
    ledger.setdefault("lessons", []).extend(new_recs)
    ledger["last_backfill"] = _now_iso()
    save_json(LEDGER, ledger)
    write_digest(new_recs, reinforced, len(items), len(items))
    nd = sum(1 for r in new_recs if r["kind"] == "discovery")
    print(f"INGEST: {len(items)} extracted -> {len(new_recs)} NEW "
          f"({len(new_recs)-nd} correction, {nd} discovery), {reinforced} already-known/dup. "
          f"Appended to AGENTS_LESSONS.md + ledger.")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write lessons (default dry-run)")
    ap.add_argument("--session", help="mine ONE session id (full re-read), ignore cursor")
    ap.add_argument("--backfill", action="store_true", help="scan history (with --since)")
    ap.add_argument("--since", help="YYYY-MM-DD lower bound on transcript mtime")
    ap.add_argument("--max", type=int, default=MAX_PER_RUN_DEFAULT, help="cap LLM calls")
    ap.add_argument("--kind", choices=["both", "correction", "discovery"], default="both")
    ap.add_argument("--no-telegram", action="store_true")
    ap.add_argument("--revert", help="remove a learned lesson by ledger id")
    ap.add_argument("--harvest", help="scan ALL transcripts -> write candidates JSON (no LLM)")
    ap.add_argument("--ingest", help="ingest a JSON list of extracted lessons (single writer)")
    a = ap.parse_args()

    if a.harvest:
        sd = None
        if a.since:
            try:
                sd = datetime.strptime(a.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError:
                print("--since must be YYYY-MM-DD"); return
        do_harvest(a.harvest, sd)
        return
    if a.ingest:
        do_ingest(a.ingest)
        return

    if a.revert:
        ledger = load_json(LEDGER, {"lessons": []})
        hit = False
        for rec in ledger.get("lessons", []):
            if rec.get("id") == a.revert:
                rec["status"] = "reverted"; hit = True
        if hit:
            save_json(LEDGER, ledger)
            remove_from_lessons(a.revert)
            print(f"Reverted {a.revert} (removed from AGENTS_LESSONS.md, flagged in ledger).")
        else:
            print(f"No ledger entry with id={a.revert}")
        return

    since_dt = None
    if a.since:
        try:
            since_dt = datetime.strptime(a.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            print("--since must be YYYY-MM-DD"); return
    if a.backfill and not since_dt:
        print("--backfill needs --since YYYY-MM-DD (safety: don't mine 1885 files blind)")
        return

    want = {"correction", "discovery"} if a.kind == "both" else {a.kind}
    cursor = load_json(CURSOR, {"sessions": {}, "team_files": [], "last_run": ""})
    cands = candidates_from_cli(a.session, since_dt, cursor, want)
    if not a.session:
        cands += candidates_from_team(since_dt, cursor, want)
    scanned = len(cands)
    cands = prefilter(cands)
    cands.sort(key=lambda e: e.get("ts", ""), reverse=True)   # newest corrections first
    cands = cands[: a.max]
    nC = sum(1 for c in cands if c["kind"] == "correction")
    print(f"{'APPLY' if a.apply else 'DRY-RUN'} — {scanned} exchange-candidate(s); "
          f"{len(cands)} after prefilter (cap {a.max}): {nC} correction, {len(cands)-nC} discovery.")

    keys = existing_keys()
    ledger = load_json(LEDGER, {"lessons": []})
    existing_ids = {r.get("id") for r in ledger.get("lessons", [])}
    new_recs, reinforced, errs, run_keys = [], 0, 0, []

    for i, ep in enumerate(cands, 1):
        res = extract_lesson(ep)
        if not res or res.get("_error"):
            errs += 1
            continue
        if not res.get("is_lesson") or float(res.get("confidence", 0)) < MIN_CONFIDENCE:
            continue
        if is_duplicate(res, keys) or is_duplicate(res, run_keys):
            reinforced += 1
            continue
        lid = f"L{date.today().strftime('%y%m%d')}-{len(existing_ids) + len(new_recs) + 1:03d}"
        rec = {
            "id": lid, "title": _scrub(res.get("title", ""))[:120],
            "rule": _scrub(res.get("rule", "")), "why": _scrub(res.get("why", "")),
            "how_to_apply": _scrub(res.get("how_to_apply", "")),
            "dedup_key": res.get("dedup_key", ""), "kind": ep["kind"],
            "type": res.get("type", ep["kind"]), "category": res.get("category", "other"),
            "confidence": float(res.get("confidence", 0)),
            "source": ep["source"], "session": ep["session"], "line": ep["line"],
            "quote_ts": ep.get("ts", ""), "learned_date": date.today().isoformat(),
            "learned_at": _now_iso(), "status": "active",
        }
        new_recs.append(rec)
        run_keys.append(_tokens(rec["dedup_key"] + " " + rec["title"]))
        print(f"  [{i}/{len(cands)}] + NEW {rec['kind']}/{rec['type']} ({rec['confidence']:.2f}): {rec['title']}")

    nd = sum(1 for r in new_recs if r["kind"] == "discovery")
    print(f"\nResult: {len(new_recs)} new ({len(new_recs)-nd} from you, {nd} self-discovered), "
          f"{reinforced} reinforced/dup, {errs} error(s).")

    if a.apply:
        if new_recs:
            append_to_lessons(new_recs)
            ledger.setdefault("lessons", []).extend(new_recs)
        ledger["last_run"] = _now_iso()
        save_json(LEDGER, ledger)
        save_json(CURSOR, {**cursor, "last_run": _now_iso()})
        dp = write_digest(new_recs, reinforced, scanned, len(cands))
        print(f"Wrote {len(new_recs)} lesson(s) -> AGENTS_LESSONS.md; ledger + digest {dp.name}.")
        if new_recs and not a.no_telegram:
            try:
                from _lib import telegram
                telegram.send("📚 Carol learned %d new lesson(s):\n%s" % (
                    len(new_recs), "\n".join(f"• {r['title']}" for r in new_recs[:8])))
            except Exception:
                pass
    elif new_recs:
        print("\n(dry-run) would learn:")
        for r in new_recs:
            print(f"  - [{r['kind']}/{r['type']}/{r['category']}] {r['title']}\n      {r['rule'][:150]}")
        print("\nRe-run with --apply to write.")


if __name__ == "__main__":
    main()
