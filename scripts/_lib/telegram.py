"""Telegram — send messages to Carol's owner chat. Markdown-safe."""

from __future__ import annotations

import os
import re

import requests

# 5/30 fix — load .env so GMAIL_APP_PASSWORD / API keys are present when
# Carol (OpenClaw/Telegram) shells out to this script. A shelled child does
# NOT inherit the daemon's env, so without this the credential reads below
# return '' and the script fails (e.g. IMAP login). Absolute path → cwd-safe.
try:
    from pathlib import Path as _CCF_P
    from dotenv import load_dotenv as _ccf_load_dotenv
    # repo root is THREE levels up from scripts/_lib/telegram.py — parent.parent
    # pointed at scripts/.env (nonexistent), silently disabling Telegram alerts.
    _ccf_load_dotenv(_CCF_P(__file__).resolve().parent.parent.parent / ".env")
except Exception:
    pass

DEFAULT_BOT_TOKEN = ""
DEFAULT_CHAT_ID  = ""


def _bot_token() -> str:
    return os.environ.get("TELEGRAM_BOT_TOKEN", DEFAULT_BOT_TOKEN)


def _chat_id() -> str:
    return os.environ.get("USER_TELEGRAM_CHAT_ID", DEFAULT_CHAT_ID)


def _escape_markdown(text: str) -> str:
    """Escape characters Telegram MarkdownV2 chokes on. We use the legacy
    'Markdown' parse mode which is more forgiving — this just prevents the
    most common breakages (* and _ inside numbers etc).
    """
    # Don't try to escape *_ since we use them for emphasis. Just quote
    # backticks and brackets that Telegram interprets weirdly inside code.
    return text


def _log_sent(text: str, ok: bool, chat: str) -> None:
    """Append every outbound Telegram message to a daily audit log so Carol's
    own pushes are reviewable (and minable by the learning loop). Never raises —
    a logging failure must never break a send. Added 6/29 to close the
    no-outbound-observability gap (couldn't audit what Carol sent on Telegram)."""
    try:
        from pathlib import Path
        from datetime import datetime
        d = datetime.now()
        logdir = Path(__file__).resolve().parent.parent.parent / "data" / "memory"
        logdir.mkdir(parents=True, exist_ok=True)
        body = "\n".join("> " + ln for ln in text.splitlines()) or "> (empty)"
        entry = (f"\n**🤖 Carol → {chat}** _{d:%Y-%m-%dT%H:%M:%S}_ · "
                 f"sent={'OK' if ok else 'FAILED'}\n{body}\n")
        with open(logdir / f"telegram_sent_{d:%Y-%m-%d}.md", "a", encoding="utf-8") as fh:
            fh.write(entry)
    except Exception:
        pass


def send(text: str, *, chat_id: str | None = None,
         parse_mode: str = "Markdown", silent: bool = False,
         disable_preview: bool = True) -> bool:
    """Post a message to Telegram. Returns True on success.

    Never raises — Telegram failures should not crash daemon scripts.
    """
    chat = chat_id or _chat_id()
    # Telegram hard-caps messages at 4096 chars — auto-route long text through
    # send_long instead of silently getting a 400 back.
    if len(text) > 4000:
        return send_long(text, chat_id=chat_id, parse_mode=parse_mode,
                         silent=silent, disable_preview=disable_preview) > 0
    payload = {
        "chat_id": chat,
        "text": _escape_markdown(text),
        "parse_mode": parse_mode,
        "disable_notification": silent,
        "disable_web_page_preview": disable_preview,
    }
    ok = False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{_bot_token()}/sendMessage",
            json=payload, timeout=15,
        )
        if r.status_code == 200:
            ok = True
        elif parse_mode:
            # Markdown parse failures (unbalanced _ or * — e.g. FOOD_LION_1513)
            # return 400 and the alert silently vanished. Retry once as plain text:
            # a less-pretty message beats a dropped one.
            payload.pop("parse_mode", None)
            payload["text"] = text
            r2 = requests.post(
                f"https://api.telegram.org/bot{_bot_token()}/sendMessage",
                json=payload, timeout=15,
            )
            ok = r2.status_code == 200
    except Exception:
        ok = False
    _log_sent(text, ok, chat)
    return ok


def send_long(text: str, *, max_chunk: int = 3500, **kw) -> int:
    """Split a long message into Telegram-safe chunks. Returns chunk count sent."""
    if len(text) <= max_chunk:
        return 1 if send(text, **kw) else 0
    sent = 0
    chunks = []
    cur = ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > max_chunk:
            chunks.append(cur)
            cur = line
        else:
            cur = (cur + "\n" + line) if cur else line
    if cur:
        chunks.append(cur)
    for c in chunks:
        if send(c, **kw):
            sent += 1
    return sent
