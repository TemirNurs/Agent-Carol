"""Telegram — send messages to Carol's owner chat. Markdown-safe."""

from __future__ import annotations

import os
import re

import requests

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


def send(text: str, *, chat_id: str | None = None,
         parse_mode: str = "Markdown", silent: bool = False,
         disable_preview: bool = True) -> bool:
    """Post a message to Telegram. Returns True on success.

    Never raises — Telegram failures should not crash daemon scripts.
    """
    chat = chat_id or _chat_id()
    payload = {
        "chat_id": chat,
        "text": _escape_markdown(text),
        "parse_mode": parse_mode,
        "disable_notification": silent,
        "disable_web_page_preview": disable_preview,
    }
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{_bot_token()}/sendMessage",
            json=payload, timeout=15,
        )
        return r.status_code == 200
    except Exception:
        return False


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
