# Carol Follow-Up System — God-Level Design
_Built from the full history: 311 deduped chases (Apr 6–Jun 16), ~900 raw May emails, 22 daily plans, AGENTS_LESSONS (1,748 lines), ~90 distinct user corrections, 5 sender pipelines._

## 1. The bombshell (the data nobody had looked at end-to-end)
**More chasing correlates with WORSE outcomes.** Across the full CRM history:
- **Lost** bids got chased far more than won bids.
- Still-open submitted bids sat in the middle.
- Withdrawn bids were chased the least.

Causation isn't proven (we may chase weak bids harder), but the direction is unambiguous: **hammering a GC does not win work — and the best-relationship GCs have the HIGHEST reply rates with the LEAST chasing.** The "daily pressure until they reply" policy is the thing most worth revisiting.

## 2. Three eras of Carol's chasing
- **ERA 1 (Apr 6–May 5):** legacy bot mis-firing — two retail bids re-sent **32× each in one day**.
- **ERA 2 — THE RUNAWAY LOOP (May 11–18):** **741 chase emails in 8 days** for 64 bid×GC pairs (**11.6× over-send**, peaked 156/day). The worst-hit GC got **351 raw emails**, the next 185, the next 93. This is the catastrophe the whole reply-awareness/throttle stack was built to end.
- **ERA 3 (post 5/19–5/21):** throttle + reply-awareness landed → **2–8/day.** Sane. The current system is Era 3.

## 3. ~90 corrections — the recurring failure classes (what you kept fixing)
1. **Brief/assert off a label or memory, not the source** — 24× (the single biggest class; → "verify before briefing" law)
2. **Reply attribution / routing** — 12× (exact-FROM missed sibling PMs; [ID:] tag bled across bids; corrupt tags)
3. **Cadence policy churn** — 12× (timer → 7-day buffer → indefinite hold → 6/11 daily-pressure)
4. **Reply classification** — 8× (Gemini unreliable → deterministic regex; REDIRECT-before-LOST; subject-bound LOST)
5. **Re-chasing a GC who already replied** — 7× (a replied contact 5/21, mid-batch 5/29, soft-timer 6/5, **a held bid 6/16**)
6. **Noise / duplicate pings** — 7× (UNCLEAR to Telegram, double briefs, recurring audit spam)
7. **CRM/status hygiene** — 5× · **Coverage** — 2× · **Ball-in-court/redirect/stop** — 2×

## 4. What you actually want (intent model, distilled from your words)
1. **Never re-chase a GC who replied.** A reply = ball in their court. (the #1 repeated demand)
2. **Verify the source before you say anything** — read the real email/Sent/CRM cell, never a label/memory.
3. **Full coverage, honestly accounted** — every active bid evaluated daily; no silent drops; show the skip reasons.
4. **Protect relationships** — chasing is a cost, not free; don't annoy a GC, especially a repeat one.
5. **One brain, no duplicates** — one chase decision, one send, one brief; no contradictory pipelines or double pings.
6. **Ground truth = Gmail Sent + the actual reply + the CRM cell** — not caches or report labels.
7. **Autonomy for tool ops; external sends are gated** — chases are external; they need real authorization.
8. **Stable identity** — route by Internal ID, cross-checked against the subject; never by row-position Bid#.

## 5. What is STILL broken (post-Era-3)
- **5 sender pipelines** (chase_executor [active], execute_approved_chases, chase_silent_followups, chase_consolidated, send_followups_throttled) + 2 conflicting state systems → double-fire risk.
- **The approval gap (HIGH):** the brief says "reply approve to execute," but `chase_executor` autopilots at 08:45 **regardless** — the approval is a no-op. This is why a held bid went out repeatedly.
- **LOST not always sticking:** one bid was chased 6/10 **after** the GC replied "we were not awarded" on 5/27 — a terminal state leaked.
- **Chasing into the void:** one bid hit 27 chases / 0 replies ever; no lifetime cap on a dead-silent bid.
- **Fail-OPEN guards:** an IMAP hiccup disables reply-awareness → blind sends.
- **Reply attribution fragility** on bare/odd subjects.

## 6. God-level target architecture
**ONE pipeline, one state machine, one brain:**
```
CRM (ground truth: active bids, contacts, Internal ID)
   │  backfill_chase_history (Gmail Sent → true send counts)
   ▼
DECISION ENGINE (single module; the only place rules live)
   • per-bid REPLY STATE MACHINE (below)
   • cadence by RELATIONSHIP TIER (not one-size daily)
   • terminal-state registry (LOST/WON/DO_NOT_CONTACT — sticky, GC-wide)
   • lifetime cap on silent bids
   ▼
PLAN (proposed_chases_DATE.json) + BRIEF (Telegram, opt-in)
   ▼
APPROVAL GATE  ← the real decision point
   ▼
ONE SENDER (consolidated per-GC email, fail-CLOSED guards, business-hours+holiday aware)
   ▼
RECAP (Gmail All-Mail truth) → CRM Notes/Status
```

### Per-bid reply state machine (the core)
```
NEW_SUBMIT ──chase(tier cadence)──▶ SILENT ──(lifetime cap)──▶ DORMANT (manual only)
   │                                   │
   ▼ reply                             ▼ reply
classify ─▶ LOST/WON/DO_NOT_CONTACT ─▶ TERMINAL (never chase, sticky, GC-wide)
        ─▶ "we'll reach out" ────────▶ BALL_IN_COURT (hold until THEY reply)
        ─▶ "check back <date>" ──────▶ SCHEDULED (resume that date only)
        ─▶ asked us / re-price ──────▶ WE_OWE (human item, never chase)
        ─▶ OOO + return date ────────▶ hold to return+1; OOO no date ─▶ hold
        ─▶ still pending (vague) ────▶ short hold, then tier cadence
```

### Cadence by relationship tier (replace one-size daily)
- **Repeat/relationship GC:** gentle — ~weekly, max 3–4 touches, then hold. (Data: repeat/relationship GCs reply most with least chasing.)
- **New/competitive GC:** moderate — every 3–4 business days, lifetime cap ~6.
- **Cold/silent:** cap at ~6 touches total, then DORMANT (flag for owner, stop auto-chasing). Ends the 27-chases-into-the-void problem.

### Safety invariants (always true)
- Never two sends to the same GC in one day (consolidate).
- Guards FAIL-CLOSED: IMAP error → skip + flag, never send blind.
- No sends on weekends/US holidays.
- Terminal states are sticky and persisted (a LOST can't be un-stuck by fuzzy attribution).
- Every send re-checks for a reply in the last few hours immediately before sending.

## 7. Already implemented (safe, shipped 6/16)
- `_new_text` quote-strip + `WILL_INITIATE` ball-in-court hard hold (the re-chased-held-bid fix)
- `DO_NOT_CONTACT` classifier → permanent stop
- OOO-without-return-date → hold (no 5-day re-chase of an absent person)
- `parse_comeback` requires comeback-intent words near a bare m/d (no fabricated dates)

## 8. Decisions — DECIDED + BUILT (2026-06-16)
- **D1 — Approval model:** ✅ DECIDED *require daily approval* → BUILT. `chase_executor.approval_ok()` requires today in `approved_dates` (chase_autopilot.json); else HOLD + Telegram ping. Grant via `scripts/approve_chases.py` (Carol runs it when the user says "approve"). Verified.
- **D2 — Cadence:** ✅ DECIDED *relationship-tiered + lifetime cap* → BUILT in morning_chase_report decision loop via `gc_tiers.json`: relationship GCs ~7d gap / cap 4; competitive ~3d / cap 6; chased-to-cap-with-zero-replies → DORMANT. Verified (bids now spaced, not daily-hammered).
- **D3 — Pipelines:** ✅ DECIDED *collapse to one* → BUILT. Active = morning_chase_report → chase_executor (gated). 4 legacy senders (execute_approved_chases, chase_consolidated, send_followups_throttled, chase_silent_followups) hard-no-op with a RETIRED message unless `--force-legacy`; followup_scheduler + chase_silent stay disabled in heartbeat. Verified.
- **D4 — Tier defaults:** relationship_domains = pkwycon, lfjennings, valiantconstruct, wimcocorp, weekesconstruction, pathcc; caps 4/6; gaps 7/3 — all editable in `data/config/gc_tiers.json`.

## 9. Completion build — ALL BUILT + verified (2026-06-16)
- ✅ **Lifetime-count backfill** — `scripts/backfill_lifetime_counts.py` scanned Gmail Sent → true per-bid counts (several silent bids at 9–12 lifetime touches each). DORMANT cap now real: verified 6 over-chased silent bids flipped to DORMANT in the live plan.
- ✅ **Fail-CLOSED guards** — `presend_reply_guard` both functions now return a skip-signal on IMAP error (was fail-open). A Gmail outage now PAUSES chases instead of sending blind.
- ✅ **US-holiday calendar** — `chase_executor.business_hours_ok` blocks federal holidays (2026-27 set), not just weekends.
- ✅ **Sticky terminal-state registry** — `_lib/terminal_states.py` (data/memory/terminal_states.json). morning_chase_report checks it first (permanent skip) and marks it on any LOST/WON/DO_NOT_CONTACT. Fixes the chase-after-LOST leak.
- ✅ **REDIRECT parenthesized-email fix** — process_followup_replies now extracts the new PM email even in `Name (email@dom)` form (was missed → one bid kept old contact). Verified.
- ✅ **Telegram command handler** — `scripts/chase_command_watcher.py` (daemon every 2 min) reads the owner's Telegram messages and acts: **"approve"** → adds today to approved_dates + launches the (guarded) executor → chases send; **"pause/stop chases"** → pause_dates + un-approve; **"resume chases"** → lift pause. Safety: owner-only, last-25-min recency gate (no stale replay), dedup, and chase-scoped matching so "approve the estimate" does NOT fire chases (19/19 classifier test). Now: you reply "approve" in Telegram → chases go, no agent in the loop.

## 10. The system now (end-to-end, all live)
CRM → backfill(Sent truth) → **decision engine** (terminal-registry skip → user-handled → tier cadence + lifetime cap/DORMANT → reply state machine: ball-in-court / scheduled / we-owe / OOO-hold / terminal) → plan + brief → **APPROVAL GATE** (nothing sends until approved) → **one sender** (fail-closed guards, business-hours + holiday, consolidated per-GC) → recap (Gmail truth) → CRM + terminal registry. Result: chases *fewer, gentler, never to a replied/lost/silent-capped GC, never without your OK.*
