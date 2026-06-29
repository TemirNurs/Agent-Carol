# Carol Heartbeat

Carol's daemon (`carol_daemon.py`) runs an async event loop that checks for
due tasks every 60 seconds. Configure the schedule in
`data/config/heartbeat.json`.

## Default Schedule

| Task | Interval | What it does |
|------|----------|--------------|
| `scrape_bids` | Every 30 min | Scrape CC + BC, dedup |
| `check_email_bids` | Every 15 min | Scan Gmail for bid invitations |
| `daily_briefing` | Daily 6:30 AM | Scrape + briefing + email report + follow-up check |
| `check_followups` | Daily 9:00 AM | Send due follow-up emails |
| `pipeline_advance` | Every 5 min | Auto-advance projects when conditions are met |

## Running

```bash
# Start daemon (runs forever)
python carol_daemon.py

# Via run_carol.py
python scripts/run_carol.py --daemon

# Run one task and exit
python carol_daemon.py --once scrape_bids

# Show schedule
python carol_daemon.py --list
```

## Logs

Logs rotate daily, kept for 7 days: `data/logs/carol_daemon.log`

## PID Guard

Only one daemon instance runs at a time. PID file: `data/carol.pid`
Delete it manually if the daemon crashed without cleanup.
