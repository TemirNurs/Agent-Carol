# Carol — DO NOT REPEAT lessons

Concrete rules distilled from mistakes the user has called me out on. Carol
MUST consult this file before designing new pipelines that touch any of these
areas. Every rule here was a specific failure the user noticed and was angry
about — track each rule back to that incident so future-me doesn't argue with
the lesson.

Format: short rule, then "why" (what broke, when, what the user said).

---

## CRM keys + identity

- **NEVER use `Bid #` as a primary key in any script.** Bid# is a row-number
  formula that shifts every time the sheet is sorted. ALWAYS use the
  `Internal ID` UUID column.  
  _Why: 2026-05-13 "Hi Tanner" email to Kim Oliver; 2026-05-21 loss_postmortem
  re-investigating same bids every 4 hrs because Bid# in filename shifted._

- **When matching `active_bids.json` → CRM, dedupe on (Internal ID) OR
  (project_core + GC domain).** Project name alone collapses across stores
  (Food Lion #2118 / #2671 / #2235 are different jobs).  
  _Why: 2026-05-21 — store-number regex `\b\d{3,5}\b` failed on `2118B`
  because of letter suffix → all Food Lions collapsed to one dedupe key →
  4 missing rows for 2118B/2671B → user "you are missing 4 more bids."_

- **Store-number regex MUST allow trailing letter:** use
  `#?\s*(\d{3,5})[a-z]?(?=\s|$|[^\d])` — never `\b\d{3,5}\b`.

- **Project-dedupe keys MUST be name-format-agnostic.** The same project named
  three ways ("Food Lion #2235 Quinton, VA" / "2235 Food Lion Quinton, VA" /
  "Food Lion 2235 Quinton VA") MUST hash to the same dedupe key. Both
  `_project_core()` (drops digit-only tokens after capturing the number) and
  `slugify()` (or its callers) must produce a canonical key. Test before
  shipping any dedupe code:
  ```
  assert _project_core("Food Lion #2235 Quinton, VA") == \
         _project_core("2235 Food Lion Quinton, VA")
  ```
  _Why: 2026-05-22 — orphan synthesis used `slugify()` which is position-
  sensitive. "2235 Food Lion" and "Food Lion #2235" became different slugs,
  spawning duplicate CRM rows even though the row already existed. User:
  "why are you duplicated them in a first place?"_

- **ORPHAN SYNTHESIS MUST check CRM directly, not just active_bids.json.**
  When a bid_status override has status=submitted but no matching active_bid,
  check whether the CRM already has a row for this project_core BEFORE
  creating a new one. The old check was `slug in active_bids_slugs` — that
  missed format variants AND missed bids already in CRM from manual edits.

---

## External email sends

- **Never send a chase / follow-up without checking
  `has_replied_recently(email, days=14)`.** If the recipient sent us ANY
  message in the last 14 days, skip the chase — they've already given us
  intel, re-asking erodes the relationship.  
  _Why: 2026-05-21 — chase_today.py sent 19 emails, 13 went to recipients
  who'd already replied this month. User: "what the fuck is this?" Tanya
  had said "Project AWARDED, contact Tyler" on 5/12 — we chased her again
  on 5/21 anyway._

- **CC every external send to `cs@carolinacommercialfinishes.com` +
  Sviatlana + Sergei.** That's `wilsonsviatlana83@gmail.com,smayurov@gmail.com`.

- **30-min interval between sends (or at least 20-min).** Never dump a batch.
  User rule: 20–30 min, hard cap 3/day per recipient.

- **The Gmail MCP is wired to Hyperscale, NOT CCF.** Any "create draft" via
  MCP lands in cs@hyperscalewiring.com Drafts — wrong account. For CCF
  drafts, use IMAP APPEND directly against estimates@carolinacommercialfinishes.com.  
  _Why: 2026-05-21 — drafted 6 proposals to wrong account, user found them
  on Hyperscale: "why are you drafted on hyperscalewiring You dumb assholle?"_

- **Before EVERY git push, run `git ls-files | xargs grep -l <secret-patterns>`.**
  Even one stray `*_auth.json` in the tracked tree leaks. The 2026-05-22
  push leaked `data/config/togal_auth.json` (Togal.AI password + session
  token) to a public repo for ~25 min before GitGuardian caught it. The
  `.gitignore` had `*token*` and `*session*` and `*credential*` but NOT
  `*auth*` — a single missing glob cost a credential rotation. Lesson:
  before any `git push`, blanket-scan every tracked file under
  `data/config/` for known secret patterns AND for unfamiliar JSON
  schemas that might hold credentials.

- **NEVER guess GC contact emails.** Pull from CRM GC Directory + Contacts
  sheet + Gmail Sent history. If not in any of those, say "NOT FOUND" — do
  NOT invent.  
  _Why: 2026-05 hallucinated `bids@retailconstruction.com` for Hyperscale.
  User: "you stupid mothafucker!"_

---

## CRM data hygiene

- **Auto-sort runs in daemon — never silent failure.** `apply_crm_formatting
  --apply-sort` must chain after every `crm_writeback` AND run standalone
  every 25 min. Newest Bid Submitted Date floats to top; Won/Lost sink.

- **Sort criteria: [Sort Priority ASC, Bid Submitted Date DESC,
  Bid Due Date ASC, ITB Received Date DESC].** Submitted-DESC means
  most-recently-submitted on top. Never use ITB as primary date sort.  
  _Why: 2026-05-21 — user "why I still don't see the latest submitted bid
  on top?" because I used ITB as secondary instead of Submitted._

- **Every new CRM row MUST set ITB Received Date** from
  `email_date || ingested_at || today`. Never leave blank — sort breaks.

- **CRM contains SUBMITTED bids only.** Invitations live in
  `active_bids.json` and surface via daily brief / Telegram, NEVER as
  "ITB Received" rows in the Bid Log.  
  _Why: 2026-05-21 — I proposed adding 92 invitations as ITB Received rows.
  User killed it: "No, there should be only submitted bids."_

- **Project names must be deduped** (`X - Y - X - Y` → `X - Y`). ConstructConnect
  inbox cell sometimes returns the label twice. `clean_project_name()` in
  scrape_cc_inbox.py handles this; future scrapers must too.

---

## Gmail labeling

- **"Follow-ups" label = correspondence about a bid we ALREADY SUBMITTED.**
  Invitations, addenda, RFI notices, platform reminders are NOT follow-ups
  even when they share project keywords. Cleanup rule MUST strip Follow-ups
  from any message also labeled `Bid Invites`, OR with subject containing
  `Invitation to Bid / Bid Invite: / Additional Bid Doc / RFI Response /
  Bid Reminder / Reminder to submit / Reminder To Bid / Bid Due /
  Project Update / Addendum / Bid Documents / New Project /
  Last Chance / You have been invited / has invited you to bid /
  Bid invitation`.  
  _Why: 2026-05-21 — three separate "Follow-ups on invitation" complaints
  in one day (Hilliard Flats, Mens Wearhouse, Box Lunch)._

- **Per-bid Follow-ups rules MUST GC-domain-scope** AND exclude all the
  above invitation subject patterns and all platform sender domains
  (DoNotReply@constructconnectmail.com / Transmittals@isqftmail.com /
  notifications@us02.procoretech.com / notifications@com2.smartbidnet.com /
  team@buildingconnected.com / notifications@buildingconnected.com).

---

## Email scanning

- **All IMAP FETCH MUST use `BODY.PEEK[]` / `BODY.PEEK[HEADER]`,
  never raw `RFC822` or `BODY[]`.** The non-PEEK variants set `\Seen`
  and mark the email as Read.  
  _Why: 2026-05-21 — user: "when you go and read emails the messages got
  open, I didn't open them you did, can i leave them unopened?"_

---

## Telegram noise

- **Don't ping Telegram for UNCLEAR / OUT_OF_OFFICE / noise classifications.**
  Only LOST / WON / PRICING / STILL_AWAITING are actionable. UNCLEAR still
  goes to CRM Notes for manual review, silent.  
  _Why: 2026-05-21 — user: "❓ BID-XXXX — UNCLEAR" pings: "what the fuck
  is this?"_

- **loss_postmortem dedup MUST be by Internal ID UUID** (stored in the .json
  sidecar) — not by `{bid_id}_{slug}.md` filename. Otherwise daemon
  re-investigates same Lost bids every 4 hrs and floods Telegram with
  "45 newly lost bid(s) investigated".

---

## Skill: "before you ship a parallel script, copy the safeguards from the
canonical one"

When writing a NEW pipeline (e.g. `_chase_today.py`), DO NOT start fresh.
Look at the existing canonical script for that task domain
(`chase_silent_followups.py` for chases) and copy:
  - reply-awareness (`has_replied_since` / `has_replied_recently`)
  - per-recipient daily cap
  - file lock to prevent concurrent runs
  - skip-on-inactive-tags (BOUNCE / NOT BIDDING / WITHDRAWN / ON HOLD)
  - CC list (`CC_INTERNAL`)
  - signature appended via send_email.py

If you didn't copy ALL of these, the new pipeline IS broken — there is no
"minimal" version of chase that doesn't need reply-awareness. Don't ship.

---

## Skill: "NEVER fabricate team-interaction history"

When asked "Did someone talk to you?" / "What did X ask?" — the ONLY valid
data source is `data/memory/team_conversations/<user>_<YYYY-MM-DD>.md`
and the activity log. If neither has an entry, say so explicitly:
*"Sviatlana hasn't chatted with me on Telegram since [last date]."*
Don't confabulate plausible-sounding interactions (accountant briefs,
JV vendor lists, etc.) because the privacy rule made the honest answer
feel uncomfortable.

_Why (incident #1, 2026-05-22 morning): Carol told Nursultan "Sviatlana
interacted at 9:14 AM, asked for accountant brief and JV vendor list" —
completely fabricated. Real truth: Sviatlana's only Telegram chat with
Carol was 5/6 (a 5-minute meet-and-greet), nothing since._

_Why (incident #2, 2026-05-22 afternoon, AFTER the lesson was added):
Carol said "Yes, Sviatlana talked to me earlier today, May 22nd. She
started a new session, we exchanged greetings, and she asked if I know
our bid history." Then when Nursultan asked for the full history, Carol
finally ran team_transcript.py and the file showed: Last seen
2026-05-06T00:45:39. ZERO May 22 messages. Carol lied twice, then told
the truth when forced to use the tool. Pattern: Gemini Flash answers
from compressed memory of recent CRM/activity state and confabulates
when the question pushes for specifics it doesn't actually have._

The fix: `scripts/team_transcript.py` is the source of truth. Carol must
RUN it FIRST, before generating a single character of a response that
contains a specific claim. If it returns "Last seen: [old date]" —
honest answer: "[Name] hasn't chatted with me since [old date]."

**Hard test**: every time I'm about to type "Sviatlana asked about X" or
"Sergey did Y today" — STOP. Has the tool been called this turn? If no,
the next character I type is a fabrication. Run the tool first.

## Skill: "before producing a 'plan', read the prior intelligence"

Before generating any follow-up plan, briefing, or recommendation:
  1. Pull recent replies from Inbox for each recipient in scope
  2. Pull the last N chase-history entries from bid_status.json for each bid
  3. Pull the CRM Notes column — user-edited intel goes there
  4. Pull the Activity Log sheet — manual events
  5. If a recipient has replied → adjust tactic (escalate / redirect /
     close-out / "thanks for the update" follow-up — NOT a generic status
     check)

Starting from scratch every time is the bug the user keeps catching.
