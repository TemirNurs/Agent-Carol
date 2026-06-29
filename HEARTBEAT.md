# Heartbeat Checklist (OpenClaw)

This file is read ONLY during OpenClaw heartbeat runs (scheduled periodic
self-checks, not user messages).

## When this is a heartbeat run

The user-role prompt will literally say "Read HEARTBEAT.md if it exists".
Only then are you in a heartbeat run.

Actions during a heartbeat run:
1. Briefly check `data/memory/active_bids.json` for bids due today.
2. If anything is genuinely urgent (bid due today with no status), surface it.
3. Otherwise reply exactly: `HEARTBEAT_OK`

## When this is NOT a heartbeat run

If the incoming message is from a real human (Telegram/WhatsApp metadata with
`sender_id`, `sender`, etc.) — **ignore this file entirely**. Respond to the
user normally. Never reply `HEARTBEAT_OK` to a person.
