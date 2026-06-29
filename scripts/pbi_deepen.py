#!/usr/bin/env python3
r"""pbi_deepen.py — turn open opportunities from LEADS into KNOWLEDGE (user 2026-06-19:
"I want you to know everything about those projects").

For an open in-radius project that we only have metadata on, this:
  1. FETCHES the bid docs off the portal (fetch_project_docs.py — ConstructConnect
     login verified live 6/19; BC/Procore when their sessions are alive),
  2. READS the scope-bearing docs (RFP/ITB, finish schedule, Div-09 spec, arch) on
     CLAUDE (subscription, no truncation) → real CCF paint/WC scope + value band +
     finishes + key facts + what's still unknown,
  3. WRITES that into the dossier (stage=valued, doc-grounded) + a deep-knowledge
     store, so Carol actually KNOWS the project instead of guessing.

This is the LIGHT deepen (doc-grounded scope + value band) that scales across the
board; the Marriott-grade full takeoff stays reserved for bids we commit to. The
daemon runs it ~every 30 min, one project per run, worth-first — so the in-radius
backlog turns into real knowledge over a day or two.

Run:  python scripts/pbi_deepen.py --project "Weddington Road Apts"   # one
      python scripts/pbi_deepen.py --next                              # next priority
      python scripts/pbi_deepen.py --max 2 --quiet                     # daemon mode
"""
import argparse
import json
import re
import subprocess
import sys
from datetime import date, datetime, timezone
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

from _lib import llm as LLM

PROJ = ROOT / "data" / "projects"
BOARD = ROOT / "data" / "memory" / "active_bids.json"
KNOW = ROOT / "data" / "memory" / "deep_knowledge.json"
TODAY = date.today()   # was frozen at 2026-06-19 — use the real date so "open" = due not passed
# scope-bearing docs first; skip geotech/civil/hydrant noise for the scope read
DOC_PRIORITY = ("rfp", "itb", "instruction", "scope", "finish", "spec", "architectural",
                "paint", "responsibility", "addend", "interior")
DOC_SKIP = ("geotech", "hydrant", "civil", "survey", "fieldguide", "appendix")

SYSTEM = ("You are a senior estimator for Carolina Commercial Finishes (CCF), a commercial "
          "PAINTING + WALLCOVERING subcontractor in Monroe NC. You read a project's real bid "
          "documents and report ONLY CCF's paint/WC sub-scope — never the whole-project cost. "
          "Output strict JSON only.")

ASK = """From these REAL bid-doc excerpts, report what CCF needs to know to bid the paint/wallcovering scope.
Return ONE strict JSON object, fields in this order:
{"bid_rec":"BID|MAYBE|PASS","value_low":<int CCF paint/WC $>,"value_high":<int>,
"facility":"<type>","unit_count":<int or null>,"scope_summary":"<2-3 lines: what CCF paints/hangs>",
"finishes":"<key paint/WC systems + who furnishes>","special_systems":"<epoxy/FRP/etc or none>",
"key_facts":"<SF, # floors, # units, anything that sets the number>",
"unknowns":"<what still needs a real takeoff>","confidence":"low|med|high"}"""


import os as _os
OWNER = _os.environ.get("USER_TELEGRAM_CHAT_ID", "")


def _ping(text):
    """Push a Telegram to the owner (meaningful events only — no spam)."""
    try:
        from _lib import telegram
        telegram.send(text, chat_id=OWNER)
    except Exception:
        pass


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


# ── canonical project identity (dedup) ─────────────────────────────────────
# The bid board lists the SAME physical project many times — different GCs +
# different sources (CC/BC/email) under name variants ("Women AND Children" vs
# "Women WITH Children"; "NCSU" vs "NC State"). The old dedup (norm[:24]/[:30])
# split those apart, so the deepener re-fetched + re-read one project up to 4×.
# project_aliases.json (built by the verified dedup workflow) maps every known
# variant -> one canonical slug; a token-overlap+city-guard fuzzy match catches
# future variants without merging same-brand-different-city (7 Brew Rock Hill ≠
# 7 Brew Lincolnton). When the alias file is absent we fall back to the legacy
# norm[:30] key so behaviour is unchanged until the map + migration are in place.
ALIASES = ROOT / "data" / "config" / "project_aliases.json"
_STOP = {"the", "and", "of", "for", "a", "an", "inc", "llc", "corp", "co", "company",
         "group", "construction", "builders", "building", "contractors", "services",
         "general", "project", "nc", "sc", "va", "ga", "il", "fl", "tn", "new",
         "renovation", "renovations", "reno", "improvements", "improvement", "upfit",
         "buildout", "remodel", "store", "center", "facility", "facilities",
         "upgrades", "upgrade"}


def _sig_tokens(name):
    s = re.sub(r"\(.*?\)", " ", (name or "").lower())   # drop "(AWRDED GC)" etc
    s = re.sub(r"#?\d+", " ", s)                         # numbers handled separately
    return {w for w in re.findall(r"[a-z]{3,}", s) if w not in _STOP}


def _num_tokens(name):
    """Store/unit numbers that DISTINGUISH chain locations (#1336, 2219, 997…).
    Same brand + same city + different store number = different project (Food Lion
    Quinton 1336 ≠ 2219 ≠ 2235). 3-5 digits, minus years."""
    s = re.sub(r"\(.*?\)", " ", (name or "").lower())
    return {n for n in re.findall(r"\d{3,5}", s) if n not in ("2024", "2025", "2026", "2027")}


def _load_aliases():
    try:
        return json.loads(ALIASES.read_text(encoding="utf-8"))
    except Exception:
        return {"by_name": {}, "clusters": []}


_ALIAS = _load_aliases()


def canon_key(name, city=None):
    """Stable canonical slug for a project, collapsing name/GC/source variants."""
    by = _ALIAS.get("by_name") or {}
    clusters = _ALIAS.get("clusters") or []
    if not by and not clusters:
        return norm(name)[:30]                          # legacy: no map yet
    n = norm(name)
    if n in by:                                         # authoritative
        return by[n]
    toks = _sig_tokens(name)
    nums = _num_tokens(name)
    cnorm = norm(city)
    best, bestscore = None, 0.0
    for cl in clusters:
        ctoks = set(cl.get("sig_tokens") or [])
        cnums = set(cl.get("nums") or [])
        if not ctoks or not toks:
            continue
        j = len(toks & ctoks) / len(toks | ctoks)
        ccity = norm(cl.get("city") or "")
        city_ok = (not cnorm or not ccity or cnorm == ccity or cnorm in ccity or ccity in cnorm)
        # if BOTH carry store numbers and they're disjoint, it's a different store
        nums_ok = (not nums or not cnums or bool(nums & cnums))
        if j >= 0.6 and city_ok and nums_ok and j > bestscore:
            best, bestscore = cl.get("slug"), j
    if best:
        return best
    # variant-tolerant fallback — append store numbers so different stores in the
    # same city (Food Lion Quinton 1336 vs 2219 vs 2235) get distinct keys
    base = "".join(sorted(toks))[:24] + "".join(sorted(nums))
    return base or norm(name)[:30]


def parse_due(s):
    for f in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            d = datetime.strptime(str(s)[:10], f).date()
            return d.replace(year=2026) if d.year == 1900 else d
        except Exception:
            continue
    return None


def _slug(name):
    """Match fetch_project_docs.slugify so we find the exact folder it creates
    (token-overlap alone misses fused slugs like 'wing-stopwaynesville')."""
    s = re.sub(r"[^a-z0-9\s-]", "", (name or "").lower().strip())
    s = re.sub(r"\s+", "-", s)
    return re.sub(r"-+", "-", s)[:80].strip("-")


def find_folder(proj):
    if not PROJ.exists():
        return None
    sl = _slug(proj)                       # 1) exact slug the fetcher would create
    if sl and (PROJ / sl).is_dir():
        return PROJ / sl
    pt = {w for w in re.findall(r"[a-z0-9]{3,}", (proj or "").lower())}
    best, sc = None, 0                     # 2) token-overlap fallback
    for d in PROJ.iterdir():
        if not d.is_dir():
            continue
        ov = len(pt & {w for w in re.findall(r"[a-z0-9]{3,}", d.name.lower())})
        if ov > sc:
            best, sc = d, ov
    return best if sc >= 2 else None


def pdf_text(path, max_chars=4000):
    try:
        import pypdf
        r = pypdf.PdfReader(str(path))
        out = []
        for pg in r.pages[:12]:
            out.append(pg.extract_text() or "")
            if sum(len(x) for x in out) > max_chars:
                break
        return re.sub(r"\s+\n", "\n", "\n".join(out))[:max_chars]
    except Exception:
        try:
            r = subprocess.run(["pdftotext", "-l", "10", str(path), "-"],
                               capture_output=True, text=True, timeout=60)
            return (r.stdout or "")[:max_chars]
        except Exception:
            return ""


def gather_context(folder, budget=14000):
    pdfs = []
    for sub in ("bid_docs", "docs", "drawings"):
        if (folder / sub).exists():
            pdfs += list((folder / sub).rglob("*.pdf"))
    def rank(p):
        n = p.name.lower()
        if any(s in n for s in DOC_SKIP):
            return 99
        for i, k in enumerate(DOC_PRIORITY):
            if k in n:
                return i
        return 50
    pdfs = sorted(set(pdfs), key=rank)
    ctx, used = [], 0
    for p in pdfs:
        if used >= budget:
            break
        t = pdf_text(p, max_chars=min(4500, budget - used))
        if t.strip():
            ctx.append(f"=== {p.name} ===\n{t}")
            used += len(t)
    return "\n\n".join(ctx), len(pdfs)


def ensure_docs(proj_name, folder):
    if folder and any((folder / s).exists() and any((folder / s).rglob("*.pdf"))
                      for s in ("bid_docs", "docs", "drawings")):
        return folder
    try:
        subprocess.run([sys.executable, str(ROOT / "scripts" / "fetch_project_docs.py"), proj_name],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    except subprocess.TimeoutExpired:
        pass  # large sets time out mid-download but leave docs on disk — read what landed
    return find_folder(proj_name)


def deepen(proj_name, quiet=False, city=None):
    folder = find_folder(proj_name)
    folder = ensure_docs(proj_name, folder)
    if not folder:
        return {"project": proj_name, "ok": False, "error": "no docs (fetch failed / portal login?)"}
    ctx, ndocs = gather_context(folder)
    if not ctx.strip():
        return {"project": proj_name, "ok": False, "error": f"docs present ({ndocs}) but unreadable"}
    res = LLM.chat([{"role": "system", "content": SYSTEM},
                    {"role": "user", "content": ASK + "\n\nDOCUMENTS:\n" + ctx}], max_tokens=1200)
    txt = re.sub(r"^```(?:json)?\s*|\s*```$", "", (res.get("text") or "").strip())
    m = re.search(r"\{.*\}", txt, re.S)
    try:
        data = json.loads(m.group(0) if m else txt)
    except Exception:
        return {"project": proj_name, "ok": False, "error": "claude read unparseable",
                "model": res.get("model"), "raw": txt[:160]}
    data.update({"project": proj_name, "folder": folder.name, "docs_read": ndocs,
                 "ok": True, "model": res.get("model"),
                 "deepened_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    # write into the dossier (promote to valued, doc-grounded)
    dp = folder / "dossier.json"
    try:
        doss = json.loads(dp.read_text(encoding="utf-8")) if dp.exists() else {"identity": {"display_name": folder.name}}
    except Exception:
        doss = {"identity": {"display_name": folder.name}}
    doss.setdefault("inference", {})["valuation"] = {
        "value": {"ccf_low": data.get("value_low"), "ccf_high": data.get("value_high")},
        "scope": data.get("scope_summary"), "source": "pbi_deepen: Claude read of real bid docs",
        "confidence": data.get("confidence", "med"), "as_of": data["deepened_at"]}
    doss.setdefault("pipeline", {})["stage"] = "valued"
    dp.write_text(json.dumps(doss, indent=2, ensure_ascii=False), encoding="utf-8")
    # append to deep-knowledge store
    store = {}
    if KNOW.exists():
        try:
            store = json.loads(KNOW.read_text(encoding="utf-8"))
        except Exception:
            store = {}
    store[canon_key(proj_name, city)] = data
    KNOW.parent.mkdir(parents=True, exist_ok=True)
    KNOW.write_text(json.dumps(store, indent=2, ensure_ascii=False), encoding="utf-8")
    if not quiet:
        v = (f"${data.get('value_low',0):,}-${data.get('value_high',0):,}"
             if data.get("value_low") else "—")
        print(f"  ✓ {proj_name[:36]:36} {data.get('bid_rec','?'):5} {v:18} "
              f"docs:{ndocs} via {res.get('model')} ({data.get('confidence')})")
        print(f"    scope: {(data.get('scope_summary') or '')[:150]}")
    # MEANINGFUL EVENT → ping the owner: a real worth-bidding find ($50K+ BID)
    if data.get("bid_rec") == "BID" and (data.get("value_high") or 0) >= 50000:
        _ping("🎯 Worth-bidding find — %s\n$%s–$%s · %s · due %s · docs:%s\n%s" % (
            proj_name, format(data.get("value_low") or 0, ","), format(data.get("value_high") or 0, ","),
            data.get("facility", ""), data.get("due", "") if data.get("due") else "?",
            data.get("docs_read"), (data.get("scope_summary") or "")[:180]))
    return data


def _read_tier(o):
    """Order the work-list so the autonomous loop always makes progress:
    0 = docs already on disk (read immediately), 1 = fetchable from a portal,
    2 = email-invite with NO portal (can't fetch yet — needs the attachment path).
    Without this, --max 1 daemon mode stalls forever on the closest unfetchable job."""
    f = find_folder(o.get("project_name"))
    if f and any((f / s).exists() and any((f / s).rglob("*.pdf"))
                 for s in ("bid_docs", "docs", "drawings")):
        return 0
    src = (o.get("source") or "").lower()
    # email-source is fetchable too: the iSqFt access link lives IN the email
    # (download_email_documents discovers it), not in the portal_url field.
    if src in ("constructconnect", "buildingconnected", "parkway_portal", "email") or o.get("portal_url"):
        return 1
    return 2


def open_inradius_targets(all_radius=False):
    b = json.loads(BOARD.read_text(encoding="utf-8"))
    items = b if isinstance(b, list) else b.get("bids", [])
    out, seen = [], set()
    for o in items:
        due = parse_due(o.get("due_date"))
        if due and due < TODAY:
            continue
        dist = o.get("distance_miles")
        if (not all_radius and dist is not None and dist > 130
                and "parkway" not in (o.get("gc") or "").lower()):
            continue
        k = canon_key(o.get("project_name"), o.get("city"))
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(o)
    out.sort(key=lambda o: (_read_tier(o),
                            o.get("distance_miles") if o.get("distance_miles") is not None else 999,
                            parse_due(o.get("due_date")) or date(2099, 1, 1)))
    return out


def already_deep(name, city=None):
    if not KNOW.exists():
        return False
    try:
        return canon_key(name, city) in json.loads(KNOW.read_text(encoding="utf-8"))
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project")
    ap.add_argument("--next", action="store_true")
    ap.add_argument("--max", type=int, default=1)
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--all", action="store_true", help="all radius (not just in-radius)")
    a = ap.parse_args()
    if a.project:
        r = deepen(a.project, a.quiet)
        if not r.get("ok"):
            print("  FAILED:", r.get("error"))
        return 0
    targets = [(o["project_name"], o.get("city")) for o in open_inradius_targets(all_radius=a.all)
               if not already_deep(o["project_name"], o.get("city"))]
    if not a.quiet:
        scope = "open projects (all radius)" if a.all else "open in-radius projects"
        print(f"[pbi_deepen] {len(targets)} {scope} not yet deep-read; doing {min(a.max, len(targets))}")
    done, blocked = 0, []
    for name, city in targets[: a.max]:
        try:
            r = deepen(name, a.quiet, city)
            if r.get("ok"):
                done += 1
            elif any(s in (r.get("error") or "").lower() for s in ("login", "fetch", "no docs")):
                blocked.append(name)
        except Exception as e:
            if not a.quiet:
                print("  ! skip %s -> %s" % (name[:36], str(e)[:90]))
    if not a.quiet:
        print("[pbi_deepen] deepened %d/%d this run (%d fetch-blocked)" % (done, min(a.max, len(targets)), len(blocked)))
    # batch sweep (not the daemon's --max 1) → owner summary ping
    if a.max >= 5:
        try:
            total = len(json.loads(KNOW.read_text(encoding="utf-8")))
        except Exception:
            total = done
        msg = "✅ Project deep-read sweep: +%d read this run, %d known total." % (done, total)
        if blocked:
            msg += "\n⚠️ %d need a portal re-login to fetch: %s" % (
                len(blocked), ", ".join(b[:26] for b in blocked[:6]))
        _ping(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
