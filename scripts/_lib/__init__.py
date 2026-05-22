"""Carol shared library — common utilities for Carol scripts.

Why this exists: 50+ scripts each had their own copy of:
  - Gmail IMAP login + body parsing
  - gspread workbook setup + 429 handling
  - Money / date / phone formatting
  - Telegram send
  - Gemini LLM call with retry

Duplication caused: bug fixes had to be applied N times, retries were missing
or inconsistent, the slow paths (e.g. process_followup_replies hitting Sheets
API per-bid) didn't have central caching.

This module replaces all that with one well-tested implementation per concern.

Public API (stable):
  from scripts._lib import sheets, gmail, money, dates, telegram, llm, log
"""

from . import sheets, gmail, money, dates, telegram, llm, log

__all__ = ["sheets", "gmail", "money", "dates", "telegram", "llm", "log"]
