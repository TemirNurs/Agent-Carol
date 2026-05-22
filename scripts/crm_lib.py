#!/usr/bin/env python3
"""
Shared library for Carol's CRM operations against Google Sheets.

Provides a thin layer on top of gspread that all CRM scripts use:
  - Bid Log: read/write rows
  - Completed Projects: read
  - GC Directory: read

Sheet ID is read from data/config/crm_sheet.json (created by crm_convert_to_sheet.py).
"""

import json
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
SHEET_CFG = BASE / "data" / "config" / "crm_sheet.json"

_RETRY_DELAYS = (5, 15, 45)


def _retry(fn, *args, **kwargs):
    """Call fn(*args, **kwargs), retrying on gspread 429 (rate-limit) with backoff."""
    import gspread
    for attempt in range(len(_RETRY_DELAYS) + 1):
        try:
            return fn(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status != 429 or attempt == len(_RETRY_DELAYS):
                raise
            time.sleep(_RETRY_DELAYS[attempt])


def _sheet_id():
    if not SHEET_CFG.exists():
        raise RuntimeError(f"CRM sheet config missing: {SHEET_CFG}. Run crm_convert_to_sheet.py first.")
    cfg = json.load(open(SHEET_CFG, encoding="utf-8"))
    return cfg["sheet_id"]


_client = None
_workbook = None


def workbook():
    """Return cached gspread Spreadsheet."""
    global _client, _workbook
    if _workbook is not None:
        return _workbook
    sys.path.insert(0, str(BASE / "scripts"))
    from google_auth import get_gspread_client
    _client = get_gspread_client()
    _workbook = _retry(_client.open_by_key, _sheet_id())
    return _workbook


def get_sheet(name):
    """Return a worksheet by name."""
    return _retry(workbook().worksheet, name)


def all_records(sheet_name):
    """Return list of dicts (one per row) for a sheet."""
    return _retry(get_sheet(sheet_name).get_all_records)


def update_cell_by_row(sheet_name, row_idx, col_name, value):
    """Update a single cell. row_idx is 1-based including header row.
    e.g. row_idx=2 is the first data row."""
    ws = get_sheet(sheet_name)
    headers = _retry(ws.row_values, 1)
    if col_name not in headers:
        raise KeyError(f"Column '{col_name}' not in sheet '{sheet_name}'")
    col_idx = headers.index(col_name) + 1
    _retry(ws.update_cell, row_idx, col_idx, value)


def _last_used_row(ws):
    """Return the 1-based index of the last row that has any data in col A.
    Used to anchor appends so gspread's append_rows can't drift sideways when
    the sheet has sparse rows or right-edge data."""
    col_a = _retry(ws.col_values, 1)
    return len(col_a)  # gspread returns trimmed list, so length = last non-empty row


def append_row(sheet_name, row_dict):
    """Append a single row using a dict of {col_name: value}.
    Anchors at column A explicitly to avoid gspread shifting the insert when
    upper rows have sparse data. Auto-generates Internal ID if not provided
    (stable primary key — never changes when rows sort)."""
    ws = get_sheet(sheet_name)
    headers = _retry(ws.row_values, 1)
    # Auto-fill Internal ID for new bid rows
    if sheet_name == "Bid Log" and "Internal ID" in headers \
            and not (row_dict.get("Internal ID") or "").strip():
        row_dict = {**row_dict, "Internal ID": new_internal_id()}
    new_row = [row_dict.get(h, "") for h in headers]
    target_row = _last_used_row(ws) + 1
    end_col = chr(ord("A") + len(headers) - 1) if len(headers) <= 26 else None
    if end_col:
        rng = f"A{target_row}:{end_col}{target_row}"
    else:
        # >26 cols → use A1 notation with double-letter column
        from gspread.utils import rowcol_to_a1
        rng = f"A{target_row}:{rowcol_to_a1(target_row, len(headers))}"
    _retry(ws.update, rng, [new_row], value_input_option="USER_ENTERED")
    return new_row


def append_rows(sheet_name, row_dicts):
    """Append multiple rows in a single API call. Returns count appended.
    Anchors writes at column A of (last_used_row + 1) to prevent gspread's
    table-detection from shifting the insert sideways (bug observed 2026-05-11
    where new rows landed at column P instead of A on the CRM Bid Log).
    Auto-generates Internal ID for any Bid Log row missing one."""
    if not row_dicts:
        return 0
    ws = get_sheet(sheet_name)
    headers = _retry(ws.row_values, 1)
    # Auto-fill Internal ID for new Bid Log rows
    if sheet_name == "Bid Log" and "Internal ID" in headers:
        row_dicts = [
            {**d, "Internal ID": new_internal_id()}
            if not (d.get("Internal ID") or "").strip() else d
            for d in row_dicts
        ]
    matrix = [[d.get(h, "") for h in headers] for d in row_dicts]
    target_row = _last_used_row(ws) + 1
    end_row = target_row + len(matrix) - 1
    if len(headers) <= 26:
        end_col = chr(ord("A") + len(headers) - 1)
        rng = f"A{target_row}:{end_col}{end_row}"
    else:
        from gspread.utils import rowcol_to_a1
        rng = f"A{target_row}:{rowcol_to_a1(end_row, len(headers))}"
    _retry(ws.update, rng, matrix, value_input_option="USER_ENTERED")
    return len(matrix)


def find_row_by_value(sheet_name, col_name, value):
    """Find first row index (1-based) where col_name == value. Returns None if not found."""
    ws = get_sheet(sheet_name)
    headers = _retry(ws.row_values, 1)
    if col_name not in headers:
        return None
    col_idx = headers.index(col_name) + 1
    col_values = _retry(ws.col_values, col_idx)
    for i, v in enumerate(col_values, start=1):
        if str(v).strip() == str(value).strip():
            return i
    return None


def next_bid_id():
    """Find the next BID-#### sequence number."""
    import re
    ws = get_sheet("Bid Log")
    bid_col = _retry(ws.col_values, 1)  # Bid # column
    max_num = 0
    for v in bid_col[1:]:
        m = re.match(r"BID-(\d+)", str(v))
        if m:
            try:
                max_num = max(max_num, int(m.group(1)))
            except ValueError:
                pass
    return f"BID-{max_num+1:04d}"


def new_internal_id():
    """Generate a stable UUID for a brand new bid row. This ID is the PRIMARY
    KEY for all scripts; it never changes when the user re-sorts the sheet.
    The Bid# (column A) is just a display label — it's a row-number formula
    that shifts every time rows are inserted or sorted."""
    import uuid
    return str(uuid.uuid4())


def find_row_by_internal_id(internal_id, sheet_name="Bid Log"):
    """Return the 1-based row index for the given Internal ID, or None.
    This is the SAFE way to locate a bid across sheet sorting/inserting."""
    return find_row_by_value(sheet_name, "Internal ID", internal_id)


def all_records_with_internal_id(sheet_name="Bid Log"):
    """Return list of dicts (one per row) keyed by header, with the Internal ID
    guaranteed present (generated on-the-fly if a row is missing one, but NOT
    written back here — caller is responsible for persistence if desired).
    Each dict gets a synthetic `_row_idx` for downstream cell updates."""
    ws = get_sheet(sheet_name)
    rows = _retry(ws.get_all_values)
    if not rows: return []
    hdrs = rows[0]
    out = []
    has_iid_col = "Internal ID" in hdrs
    iid_idx = hdrs.index("Internal ID") if has_iid_col else None
    for ri, r in enumerate(rows[1:], start=2):
        d = {h: (r[i] if i < len(r) else "") for i, h in enumerate(hdrs)}
        d["_row_idx"] = ri
        if not has_iid_col or not d.get("Internal ID"):
            d["Internal ID"] = ""   # caller can detect + backfill
        out.append(d)
    return out


def stable_key(row_dict):
    """Return the canonical stable identifier for a CRM row.
    Prefers `Internal ID`; falls back to a composite key derived from
    immutable-ish fields when no UUID exists yet (legacy rows)."""
    iid = (row_dict.get("Internal ID") or "").strip()
    if iid:
        return iid
    # Fallback composite — slow path, only for rows missing Internal ID
    import re
    proj = re.sub(r"\s+", " ", (row_dict.get("Project Name", "") or "").lower()).strip()
    email = (row_dict.get("Contact Email", "") or "").split(",")[0].strip().lower()
    sub_date = (row_dict.get("Bid Submitted Date", "") or "").strip()
    return f"legacy::{proj}|{email}|{sub_date}"


def batch_update_rows(sheet_name, updates):
    """Apply multiple cell updates in one API call.
    updates = [(row_idx, col_name, value), ...]
    """
    if not updates:
        return 0
    ws = get_sheet(sheet_name)
    headers = _retry(ws.row_values, 1)
    cells = []
    from gspread.utils import rowcol_to_a1
    cell_updates = []
    for row_idx, col_name, value in updates:
        if col_name not in headers:
            continue
        col_idx = headers.index(col_name) + 1
        cell_updates.append({
            "range": rowcol_to_a1(row_idx, col_idx),
            "values": [[value]],
        })
    if cell_updates:
        _retry(ws.batch_update, cell_updates, value_input_option="USER_ENTERED")
    return len(cell_updates)
