#!/usr/bin/env python3
"""Patch every IMAP FETCH in Carol's scripts to use BODY.PEEK[] / BODY.PEEK[HEADER...]
instead of RFC822 / BODY[...] so scanning doesn't mark messages as Read."""
import re, sys
from pathlib import Path

try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

ROOT = Path(r"C:/Agent Carol")
FILES = list(ROOT.rglob("*.py"))

# Patterns
SUBS = [
    # Whole-message fetches
    (re.compile(r"\.fetch\(([^,]+),\s*['\"]?\(RFC822\)['\"]?\)"),
     r".fetch(\1, '(BODY.PEEK[])')"),
    (re.compile(r"\.uid\(\s*['\"]FETCH['\"]\s*,\s*([^,]+),\s*['\"]?\(RFC822\)['\"]?\)"),
     r".uid('FETCH', \1, '(BODY.PEEK[])')"),
    # Header-only fetches
    (re.compile(r"\.fetch\(([^,]+),\s*['\"]?\(RFC822\.HEADER\)['\"]?\)"),
     r".fetch(\1, '(BODY.PEEK[HEADER])')"),
    (re.compile(r"\.uid\(\s*['\"]FETCH['\"]\s*,\s*([^,]+),\s*['\"]?\(RFC822\.HEADER\)['\"]?\)"),
     r".uid('FETCH', \1, '(BODY.PEEK[HEADER])')"),
    # Subset header fetches embedded in (X-GM-LABELS BODY[HEADER.FIELDS ...])
    # → wrap BODY[ inside the string with BODY.PEEK[
    (re.compile(r"BODY\[HEADER\.FIELDS"), r"BODY.PEEK[HEADER.FIELDS"),
    # General body subset fetches
    (re.compile(r"\.fetch\(([^,]+),\s*['\"]\((BODY\[[^\]]*\])\)['\"]\)"),
     r".fetch(\1, '(BODY.PEEK\2)')"),
    (re.compile(r"\.uid\(\s*['\"]FETCH['\"]\s*,\s*([^,]+),\s*['\"]\((BODY\[[^\]]*\])\)['\"]\)"),
     r".uid('FETCH', \1, '(BODY.PEEK\2)')"),
]

changed_files = []
for f in FILES:
    if f.name.startswith("_patch_imap_peek"): continue
    try:
        text = f.read_text(encoding="utf-8")
    except Exception:
        continue
    orig = text
    for pat, repl in SUBS:
        text = pat.sub(repl, text)
    # Defensive: if BODY.PEEK.PEEK accidentally created, collapse it
    text = re.sub(r"BODY\.PEEK\.PEEK", "BODY.PEEK", text)
    text = re.sub(r"BODY\.PEEKBODY\.PEEK", "BODY.PEEK", text)
    # Avoid double-peeking inside already-peeked strings
    text = text.replace("BODY.PEEK[HEADER.PEEK", "BODY.PEEK[HEADER")
    if text != orig:
        f.write_text(text, encoding="utf-8")
        changed_files.append(f)

print(f"Patched {len(changed_files)} file(s):")
for f in changed_files:
    print(f"  {f.relative_to(ROOT)}")
