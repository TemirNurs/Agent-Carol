#!/usr/bin/env python3
"""One-off — strip hardcoded credential FALLBACKS from source code so the
secret only lives in env vars / .env file. Replaces patterns like:

   os.environ.get("GMAIL_APP_PASSWORD", "")
   GMAIL_PASS = os.environ.get("GMAIL_APP_PASSWORD", "")
   BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8607183389:AAEluq...")

with:

   os.environ.get("GMAIL_APP_PASSWORD", "")
   GMAIL_PASS = os.environ.get("GMAIL_APP_PASSWORD", "")
   BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

Safe to run repeatedly — idempotent. Reports each file changed.
"""
import re, sys
from pathlib import Path

try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

ROOT = Path(r"C:/Agent Carol")

# Patterns of literal secrets to scrub. Each: (regex, what-it-replaces-with).
SECRETS = [
    # Gmail app password (current + any prior format)
    (r'""', '""'),
    (r"''", "''"),
    # Telegram bot token (the one in current source)
    (r'""', '""'),
    (r"''", "''"),
    # User Telegram chat ID — not really a secret but keep out of repo too
    (r'""', '""'),
    (r"''", "''"),
]

# Also: standalone literal assignments where there's no env-var pattern.
# e.g. `GMAIL_PASS = ""` (no os.environ.get wrapper).
# Patch them to read from env.
LITERAL_REASSIGN = [
    (re.compile(
        r'^(\s*)(GMAIL_PASS|APP_PASSWORD|EMAIL_PASS)\s*=\s*""\s*$',
        re.M,
    ), r'\1\2 = os.environ.get("GMAIL_APP_PASSWORD", "")'),
    (re.compile(
        r'^(\s*)(BOT_TOKEN|TELEGRAM_TOKEN)\s*=\s*""\s*$',
        re.M,
    ), r'\1\2 = os.environ.get("TELEGRAM_BOT_TOKEN", "")'),
]

changed = []
for p in ROOT.rglob("*.py"):
    if "__pycache__" in p.parts: continue
    if "_legacy" in p.parts: continue
    try:
        text = p.read_text(encoding="utf-8")
    except Exception:
        continue
    orig = text
    for pat, repl in SECRETS:
        text = re.sub(pat, repl, text)
    for pat, repl in LITERAL_REASSIGN:
        text = pat.sub(repl, text)
    if text != orig:
        p.write_text(text, encoding="utf-8")
        changed.append(p.relative_to(ROOT))

print(f"Scrubbed secrets from {len(changed)} file(s):")
for f in changed[:50]:
    print(f"  {f}")
if len(changed) > 50:
    print(f"  ... and {len(changed) - 50} more")
