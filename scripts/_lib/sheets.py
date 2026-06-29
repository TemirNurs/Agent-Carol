"""Sheets — robust gspread wrapper with retry, per-process cache, batch I/O.

Solves three recurring problems:
  1. Sheets API 60-reads-per-minute quota — every script hit it under load
  2. Bare exceptions on 429 made daemon scripts fail silently
  3. Hot loops calling get_all_records() per-bid (process_followup_replies
     took 200 seconds because of this)

Usage:
    from scripts._lib import sheets
    rows = sheets.records("Bid Log")          # cached per process
    rows = sheets.records("Bid Log", fresh=True)  # force refresh
    sheets.write_cells("Bid Log", [(row, "Status", "Lost"), ...])
    bid = sheets.row_by_value("Bid Log", "Bid #", "BID-0023")
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any, Iterable

# Lazy imports — gspread is heavy
_workbook = None
_records_cache: dict[str, list[dict]] = {}
_headers_cache: dict[str, list[str]] = {}
_cache_age: dict[str, float] = {}

# Retry config
DEFAULT_RETRIES = 4
BACKOFF_BASE = 2  # seconds
BACKOFF_MAX = 60
# Read cache TTL — within this window, return cached records instead of refetching
CACHE_TTL = 30  # seconds


def _config_dir() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "data" / "config"


def _sheet_id() -> str:
    import os, json
    env = os.environ.get("CRM_SHEET_ID")
    if env:
        return env
    cfg_path = _config_dir() / "crm_sheet.json"
    return json.loads(cfg_path.read_text(encoding="utf-8"))["sheet_id"]


def workbook():
    """Return cached gspread Spreadsheet, opening on first call."""
    global _workbook
    if _workbook is not None:
        return _workbook
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from google_auth import get_gspread_client
    client = get_gspread_client()
    _workbook = client.open_by_key(_sheet_id())
    return _workbook


def _is_rate_limit(exc: Exception) -> bool:
    """True if the exception is a Google Sheets 429 quota error."""
    msg = str(exc)
    return "429" in msg or "Quota exceeded" in msg or "Rate Limit" in msg.lower()


def with_retry(fn, *args, retries: int = DEFAULT_RETRIES, **kwargs):
    """Run a gspread call with exponential backoff on 429.

    Other gspread errors propagate immediately.
    """
    for attempt in range(retries + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            if not _is_rate_limit(e) or attempt == retries:
                raise
            delay = min(BACKOFF_BASE ** attempt, BACKOFF_MAX)
            time.sleep(delay)
    return None


def get_sheet(name: str):
    """Return a worksheet handle, with retry."""
    return with_retry(workbook().worksheet, name)


def headers(name: str, fresh: bool = False) -> list[str]:
    """Return header row for a sheet. Cached per-process."""
    if not fresh and name in _headers_cache:
        return _headers_cache[name]
    ws = get_sheet(name)
    h = with_retry(ws.row_values, 1)
    _headers_cache[name] = h
    return h


def records(name: str, fresh: bool = False) -> list[dict]:
    """Return all rows of a sheet as list of dicts. Cached for CACHE_TTL seconds.

    The cache is the difference between hot scripts running in 5 sec vs 200.
    """
    now = time.time()
    if not fresh and name in _records_cache:
        if now - _cache_age.get(name, 0) < CACHE_TTL:
            return _records_cache[name]
    ws = get_sheet(name)
    rows = with_retry(ws.get_all_records)
    _records_cache[name] = rows
    _cache_age[name] = now
    return rows


def invalidate(name: str | None = None):
    """Force the next records() call to refetch. Call after writes."""
    if name is None:
        _records_cache.clear()
        _headers_cache.clear()
        _cache_age.clear()
    else:
        _records_cache.pop(name, None)
        _headers_cache.pop(name, None)
        _cache_age.pop(name, None)


def row_by_value(name: str, column: str, value: Any) -> dict | None:
    """Return the first row matching column == value, or None."""
    for r in records(name):
        if r.get(column) == value:
            return r
    return None


def row_index_of(name: str, column: str, value: Any) -> int | None:
    """1-based row index of first match, or None. Includes header row."""
    ws = get_sheet(name)
    hdrs = headers(name)
    if column not in hdrs:
        return None
    col_idx = hdrs.index(column) + 1
    col_vals = with_retry(ws.col_values, col_idx)
    try:
        return col_vals.index(value) + 1
    except ValueError:
        return None


def write_cells(name: str, updates: Iterable[tuple[int, str, Any]],
                value_input_option: str = "USER_ENTERED"):
    """Batch-update cells. Each update is (row_idx_1based, column_name, value).

    Single API call regardless of how many cells. Invalidates cache after.
    """
    from gspread.utils import rowcol_to_a1
    ws = get_sheet(name)
    hdrs = headers(name)
    cell_updates = []
    for row_idx, col_name, val in updates:
        if col_name not in hdrs:
            continue
        col_idx = hdrs.index(col_name) + 1
        cell_updates.append({
            "range": rowcol_to_a1(row_idx, col_idx),
            "values": [[val]],
        })
    if not cell_updates:
        return 0
    with_retry(ws.batch_update, cell_updates,
               value_input_option=value_input_option)
    invalidate(name)
    return len(cell_updates)


def append_row(name: str, row_dict: dict, value_input_option: str = "USER_ENTERED"):
    """Append one row, mapping dict keys to header columns."""
    ws = get_sheet(name)
    hdrs = headers(name)
    row = [row_dict.get(h, "") for h in hdrs]
    with_retry(ws.append_row, row, value_input_option=value_input_option)
    invalidate(name)
