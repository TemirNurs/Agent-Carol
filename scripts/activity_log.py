#!/usr/bin/env python3
r"""
activity_log.py - Write to the 'Activity Log' sheet on the CRM workbook.

Schema (10 cols):
  A Timestamp           ISO-8601 (e.g. 2026-05-13T17:42:00)
  B Internal ID         UUID of the bid (stable across sorts)
  C Bid# (snapshot)     Bid# at time of log (display label, may shift later)
  D Project (snapshot)  Project Name at time of log
  E Type                proposal_sent | follow_up | reply_received | call |
                        note | status_change | bid_invitation | other
  F Direction           outbound | inbound | internal
  G Counterparty        GC name or contact name+email
  H Channel             email | phone | sms | telegram | sheet | system
  I Summary             one-line description (≤200 chars)
  J Reference / Link    URL or message ID or filename, optional

Usage from Python:
  from activity_log import log_event
  log_event(internal_id="abc-uuid", bid_id="BID-0042", project="Savers",
            type="follow_up", direction="outbound",
            counterparty="jhibbard@delauter (Jeff Hibbard)",
            channel="email",
            summary="FU attempt #4 — firm tone, 4x/day cadence",
            reference="")

CLI:
  python scripts/activity_log.py --bid BID-0042 \
    --type follow_up --direction outbound --counterparty "jhibbard@..." \
    --summary "FU attempt #4"
"""
from __future__ import annotations
import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _resolve_bid(bid_id=None, internal_id=None):
    """Given EITHER bid_id OR internal_id, return (internal_id, bid_id, project_name).
    Reads CRM live to ensure we have current Bid# snapshot."""
    from crm_lib import get_sheet
    sh = get_sheet("Bid Log")
    hdrs = sh.row_values(1)
    rows = sh.get_all_values()
    iid_col = hdrs.index("Internal ID") if "Internal ID" in hdrs else None
    bid_col = hdrs.index("Bid #") if "Bid #" in hdrs else None
    proj_col = hdrs.index("Project Name") if "Project Name" in hdrs else None
    for r in rows[1:]:
        iid_val = r[iid_col] if iid_col is not None and iid_col < len(r) else ""
        bid_val = r[bid_col] if bid_col is not None and bid_col < len(r) else ""
        proj_val = r[proj_col] if proj_col is not None and proj_col < len(r) else ""
        if internal_id and iid_val == internal_id:
            return iid_val, bid_val, proj_val
        if bid_id and bid_val == bid_id:
            return iid_val, bid_val, proj_val
    return internal_id or "", bid_id or "", ""


def log_event(internal_id="", bid_id="", project="",
              type="other", direction="internal", counterparty="",
              channel="system", summary="", reference="", timestamp=None):
    """Append a row to the Activity Log sheet.
    Either internal_id or bid_id should be passed; the other will be looked up.
    """
    from crm_lib import get_sheet, _retry
    sh = get_sheet("Activity Log")
    if not internal_id or not bid_id or not project:
        resolved_iid, resolved_bid, resolved_proj = _resolve_bid(
            bid_id=bid_id, internal_id=internal_id)
        internal_id = internal_id or resolved_iid
        bid_id = bid_id or resolved_bid
        project = project or resolved_proj
    ts = (timestamp or datetime.now()).isoformat(timespec="seconds") \
        if isinstance(timestamp, datetime) else (timestamp or datetime.now().isoformat(timespec="seconds"))
    row = [
        ts,
        internal_id,
        bid_id,
        project[:80],
        type,
        direction,
        counterparty[:80],
        channel,
        (summary or "")[:300],
        (reference or "")[:200],
    ]
    # Find next empty row
    existing = _retry(sh.col_values, 1)
    next_row = len(existing) + 1
    _retry(sh.update, f"A{next_row}:J{next_row}", [row], value_input_option="USER_ENTERED")
    return next_row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--internal-id", default="")
    ap.add_argument("--bid", default="", help="Bid# (e.g. BID-0042)")
    ap.add_argument("--project", default="")
    ap.add_argument("--type", required=True,
                    choices=["proposal_sent", "follow_up", "reply_received",
                             "call", "note", "status_change", "bid_invitation",
                             "other"])
    ap.add_argument("--direction", default="internal",
                    choices=["outbound", "inbound", "internal"])
    ap.add_argument("--counterparty", default="")
    ap.add_argument("--channel", default="system",
                    choices=["email", "phone", "sms", "telegram", "sheet", "system"])
    ap.add_argument("--summary", required=True)
    ap.add_argument("--reference", default="")
    args = ap.parse_args()
    row_idx = log_event(
        internal_id=args.internal_id,
        bid_id=args.bid,
        project=args.project,
        type=args.type,
        direction=args.direction,
        counterparty=args.counterparty,
        channel=args.channel,
        summary=args.summary,
        reference=args.reference,
    )
    print(f"logged to Activity Log row {row_idx}")


if __name__ == "__main__":
    main()
