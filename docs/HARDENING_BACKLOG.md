# Carol Hardening Backlog — from the 2026-06-10 four-agent code sweep

47 verified findings across 54k lines. P0s were fixed same-day (see git log).
This file tracks the DEFERRED P1/P2 items, highest value first.

## Tier 1 — wrong-money / wrong-send risk
1. **Per-send reply guard in batch senders** — send_followups_throttled.py:287 and
   chase_consolidated.py:232 check replies only at queue build, then sleep 20-25 min
   between sends for hours. A reply landing mid-batch can't stop later sends.
   Fix: call `presend_reply_guard.recipient_replied_recently()` immediately before
   EACH subprocess send (morning_chase_report + _send_today_chases_* already do).
2. **Port subject-bound LOST patterns to classify_reply** — morning_chase_report.py:93
   classifies "PM has not awarded any contracts yet" as LOST (no we/you binding);
   line 608 then parks the bid permanently. process_followup_replies.py:459 has the
   correct patterns — share them via _lib. Same loose substrings in check_replies.py:109.
3. **Amount label priority** — track_submissions.py:158: bare "TOTAL" label + max()
   still lets "OPTION TOTAL" beat "TOTAL BID PRICE" when "base bid" phrase is absent.
   Order labels by specificity; reject amounts preceded by option/alternate/allowance.
4. **Resolver guessing** — process_followup_replies.py:318 last-resort picks lowest
   row (comment claims newest). Return None + log for manual review instead.
5. **Retry-after-send duplicates** — all batch senders sniff stdout for success;
   subprocess timeout/garble = re-send next run. Persist intent before spawn,
   verify against Sent folder before retry.
6. **estimator_agent ↔ togal_pipeline schema contract** — estimator_agent.py:1033
   reads keys ("measurements"/"grand_totals") the pipeline never writes → silent
   $0 takeoffs marked estimate_ready. Parse output["takeoff"]; fail loudly on empty.
7. **export_estimate_xlsx.py:174** — reads est["items"]/["labor"] (wrong keys) →
   empty detail sheets; line 214 hardcodes "N. Myrtle Beach, SC" + 7-Eleven notes
   onto every customer-facing workbook. Map line_items/labor_cost; address from project.

## Tier 2 — reliability / state integrity
8. **Atomic state writes + lock** — bid_status.json / active_bids.json / both activity
   logs are lockless read-modify-write across daemon + detached processes. Writers:
   temp + os.replace under lock; readers: last-good fallback. (crm_writeback.py:376,
   track_submissions.py:542, log_activity.py:67-131, activity_log.py:97.)
9. **_retry coverage** — many sheet calls bypass crm_lib._retry (crm_writeback:356,
   audit_crm_submissions:360, audit_crm_health:247, backfill_contacts:317+,
   crm_daily_summary:98, activity_log:51, apply_crm_formatting:79+). Route through
   crm_lib; extend _retry to 500/503.
10. **Internal ID on placeholder fills** — crm_writeback.py:849 filter drops empty
    values; audit_crm_submissions row_data lacks the key — placeholder-filled rows
    have no stable PK. Mint new_internal_id() in both.
11. **backfill_contacts row targeting** — writes via stale snapshot row index;
    re-resolve by Internal ID at write time (backfill_contacts.py:375,397).
12. **audit_crm_submissions: existing-row dedupe** — same-run guard added 6/10;
    still needs the vs-existing-rows (project-core + GC-domain) check crm_writeback has.
13. **Telegram robustness** — _lib/telegram.py: retry once with parse_mode=None on
    non-200 (unbalanced _/* drops messages today); auto-route >4096 to send_long.
14. **Cost kill-switch gating** — cost_watchdog kills only openclaw.json; _lib/llm.py
    DEFAULT_CHAIN keeps paid gemini first regardless. llm.chat() must read
    cost_watchdog.json and drop gemini/* when killed_today == today.
15. **Scale verification** — togal_pipeline.py:553 defaults 0.125/dpi 150 silently;
    estimator_agent stamps 1/8" on every sheet. Refuse/flag when page lacks a
    confirmed scale; require explicit --scale (3/16 vs 1/8 = 2.25× area error).
16. **wait_for_processing polls wrong state** — polls page.state but vectorization is
    per-VIEW; poll view.state or geojson feature-count stability (togal_pipeline:1129).
17. **find_or_create_project 409 fallback** — returns projects[-1] (last row of page!)
    — re-list and name-match instead (togal_client.py:196; pagination helper now
    makes the re-list correct).
18. **Guard window slicing** — presend_reply_guard mids[-20:], report [-15:]/[-80:]:
    log when len(mids) > cap; pre-filter with header-only fetches.

## Tier 3 — correctness polish / perf
19. watch_aps_reply + process_followup_replies: IMAP sequence numbers as identity →
    use UIDs or Message-ID (watch_aps_reply.py:156, process_followup_replies.py:394).
20. chase_silent_followups --seed writes legacy BID-NNNN keys into IID-keyed state.
21. track_submissions new_sends lacks message_id + logged_ids assigned after write →
    "proposal sent" activity lines never emitted (track_submissions.py:559,584).
22. recap.py:28 returns yesterday's log as "today" before rotation — check date heading.
23. apply_crm_formatting hardcodes $P2= in color rules; interpolate computed status_letter.
24. Scrapers: BC/CC merge_bids no cross-source dedupe (scrape_bc_inbox.py:528,
    scrape_cc_inbox.py:431); Procore STOP_WORDS blanks "Rock Hill"/"Chapel Hill"
    cities (scrape_procore_portal.py:183); Parkway state-center fallback silently
    DROPS far bids instead of flagging (scrape_parkway_portal.py:316, procore:409);
    BC dateDue UTC→ET off-by-a-day (scrape_bc_inbox.py:250); fetch_project_docs BC
    login pre-SSO flow dead (fetch_project_docs.py:253) — reuse bc_storage_state.
25. llm_extract_due_dates.py:79 — verify fetched email subject matches full project
    name before accepting extraction.
26. Perf: _lib/gmail.py:191 double-fetches every message (walrus misuse);
    process_followup_replies find_bid_id_by_sender does full sheet fetch per reply;
    one IMAP session per run (not per bid); cost_watchdog/team_chat_watcher re-read
    full session logs every cycle — persist byte offsets; daemon run_task inline on
    event loop (wrap in asyncio.to_thread); rotate side logs.
27. Estimator: pass timeout=60 to litellm completions; validate numeric SOW fields;
    surface Togal auth failure (ask for creds per standing rule) instead of silent
    SOW fallback (estimator_agent.py:610,1024).

## Architecture (the "billion-dollar" structural items)
A. **One reply-matcher in _lib** — INBOX-vs-AllMail, store-number, LOST patterns,
   [ID:] handling reimplemented per script is the root of most P0s. Single
   `_lib/reply_match.py` used by report, guards, processors, senders.
B. **One "record send" routine** — presend check + send + verified stamp keyed by
   Internal ID, used by every sender.
C. **Schema contracts** — takeoff JSON, estimate JSON, state files: one dataclass/
   JSON-schema module; writers validate on write, readers on read.
D. **Daily self-audit** — extend hallucination sentry: CRM ↔ Sent-folder ↔ chase-state
   reconciliation with a one-line Telegram digest ("3 mismatches found & fixed").
