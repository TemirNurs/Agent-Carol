# CCF GOD-LEVEL TAKEOFF — Architecture, Failure Taxonomy, Research, Critique

## ARCHITECTURE

I have what I need. The codebase confirms the taxonomy: `togal_pipeline.py` hardcodes the workflow, `auto_takeoff.py` has `ensure_detections`, `takeoff_scale.py` has the scale-detection helpers, and there's a graveyard of one-off `_wssu_*`, `_wedd_*`, `_clemson_*`, `_togal_m1_refire` patch scripts — that proliferation IS the whack-a-mole. Here is the architecture.

---

# CCF GOD-LEVEL TAKEOFF ARCHITECTURE

## The core diagnosis (why it's whack-a-mole)

Every failure in the taxonomy shares one root cause: **the pipeline trusts a single auto-number from a flaky source, and when that source fails it fails *silently* — so the only "detection" is you noticing a bad bid.** The fixes so far have been per-incident patches bolted onto the same trust-one-source spine (`_wssu_fix_scale`, `_wedd_m1_refire`, `_togal_m1_refire`...). New project → new way for the single source to lie → new patch. The industry research confirms the permanent fix is not a better Togal call: **no one ships a single auto-number.** Reliability comes from *independent corroboration + a confidence gate that assumes the tool will fail and catches it.*

So the redesign inverts the spine: **measure 2-3 independent ways, reconcile, and gate on agreement. Togal demoted from "the source" to "one optional corroborator."**

---

## (1) THE LAYERED ARCHITECTURE

```
┌─────────────────────────────────────────────────────────────────────┐
│ LAYER 0 — INTAKE & TRIAGE  (takeoff_intake.py)                        │
│  Per page: vector-or-raster? scale-detectable? plan/detail/site?      │
│  multifamily-repetitive? docs-present? → routes to the right path.    │
│  OUTPUT: a per-sheet manifest. NO measuring happens until this passes.│
└─────────────────────────────────────────────────────────────────────┘
            │
┌───────────▼─────────────────────────────────────────────────────────┐
│ LAYER 1 — CALIBRATION GATE  (calibrate.py)   ← THE KEYSTONE           │
│  PER SHEET, two independent scale anchors that must reconcile ≤2%:    │
│   A. title-block scale notation (regex)                               │
│   B. measure a printed dimension string / gridline span (vector) and  │
│      a SECOND one in another region + opposite axis                   │
│   C. OCR a printed room-area tag ("142 SF") and back-solve scale      │
│  Reconcile → SCALE_LOCKED(value, conf). Disagree/none → SHEET_UNSCALED│
│  (routed to human/LLM path, never measured on a guessed scale).       │
└───────────────────────────────────────────────────────────────────────┘
            │  (only SCALE_LOCKED sheets proceed to measurement)
┌───────────▼─────────────────────────────────────────────────────────┐
│ LAYER 2 — INDEPENDENT MEASUREMENT ENGINES (run all that apply)        │
│                                                                       │
│  M-VEC  Local vector engine (vector_takeoff.py, PyMuPDF)              │
│         Walls=polyline LF, rooms=area, doors=block counts.            │
│         Deterministic, offline, no cloud. PRIMARY when vector+scaled. │
│                                                                       │
│  M-LLM  Dimensioned-plan read (llm_takeoff.py)  ← promote to 1st-class│
│         Claude reads dimension strings / unit-type plans / finish     │
│         schedule → measured SF per room/unit-type. The SILVER that    │
│         actually worked at Weddington. Works on raster too (it reads).│
│                                                                       │
│  M-TOGAL  Togal AI (optional corroborator ONLY)                       │
│         Sanity-gated: reject if walls[]==0, if rooms/SF density       │
│         implausible, if garbage classes (Shaft/Hotel Room/Parking),   │
│         if scale not the locked scale. A pass = a vote, never a verdict│
│                                                                       │
│  M-PROG  Program/known-SF cross-check (always, cheap)                 │
│         Building program SF, unit schedule × count, lease SF.         │
│         Not a takeoff — a magnitude tripwire.                         │
└───────────────────────────────────────────────────────────────────────┘
            │
┌───────────▼─────────────────────────────────────────────────────────┐
│ LAYER 3 — RECONCILER & CONFIDENCE GATE  (reconcile.py)               │
│  Compare the independent results that ran:                            │
│   GOLD   ≥2 independent methods agree ≤10%  → ship, high conf         │
│   SILVER exactly 1 solid method + passes M-PROG magnitude check       │
│          → ship with explicit SILVER label + ± band                   │
│   REJECT/HUMAN  methods diverge >10%, or only Togal, or M-PROG        │
│          tripwire fires, or sheet UNSCALED → HARD STOP, loud, reasons │
│  Writes verdict + provenance + every method's number to dossier.      │
└───────────────────────────────────────────────────────────────────────┘
            │
┌───────────▼─────────────────────────────────────────────────────────┐
│ LAYER 4 — LEDGER & REGRESSION  (takeoff_benchmarks.json + guard)     │
│  Every takeoff logged with verdict/methods/deltas. New acceptance     │
│  test: replay against Cowork workbooks of KNOWN-actual jobs; a code   │
│  change that moves a known job's number >tolerance fails CI.          │
└───────────────────────────────────────────────────────────────────────┘
```

**Reconciliation rule (the whole point):** a number is GOLD only if two *independent* methods (M-VEC + M-LLM, or M-VEC + M-TOGAL) land ≤10% apart. The methods must not share a failure mode — that's why Togal can never pair with itself, and why M-LLM (reads meaning) is independent from M-VEC (reads geometry). One solid method + a passing magnitude check is an honest SILVER. Everything else stops loudly.

---

## (2) HOW IT KILLS EACH FAILURE MODE IN THE TAXONOMY

| # | Failure (observed) | Why it happened | How the architecture prevents it |
|---|---|---|---|
| **C1** | AI silent failure — detection workflow never fired / fired wrong type → 0 rooms (WSSU, UMC, Old Navy) | Single source + Togal ACKs in ms and fails silently | Togal is no longer the source. If M-TOGAL returns empty/low it's simply a vote that doesn't cast; M-VEC + M-LLM still produce GOLD/SILVER. **No single Togal call can sink a takeoff.** |
| **C2** | Walls=0 (rooms detected, `walls[]` empty) — Weddington | `workflow_type="full"` not `"paint"`; pipeline read rooms only | M-VEC computes wall LF from vector polylines directly — never depends on Togal's wall workflow. The M-TOGAL sanity gate *rejects a wall=0 result outright* instead of passing it through. |
| **C3** | Wrong scale (1/8" default vs 3/16" title block) → 2.25× off (WSSU) | Global/default scale, title block unread or unverified | **Layer 1 is a HARD gate.** Two independent anchors must reconcile ≤2% AND back-solve against an OCR'd printed SF tag. A wrong scale can't reconcile with the printed area → SHEET_UNSCALED → stop. |
| **C4** | Page selection — 206-page set, non-AIA names → all pages used → 56,267 noisy rooms, Shaft/Parking/Hotel Room | Tier-3 fallback feeds ALL pages to AI; filtering is post-hoc | Layer 0 triage classifies each sheet (plan/detail/site/elevation) BEFORE measuring; only floor-plan/RCP sheets get measured. Garbage classes rejected at the M-TOGAL gate. M-VEC/M-LLM never fire on site/detail pages. |
| **C5** | Combined-PDF duplication — 3 orphan Togal sets from retries, paint workflow on wrong set_id | Cloud-state coupling + blind retries | M-VEC and M-LLM are **stateless and local** — no cloud sets to orphan. Togal upload made idempotent (content-hash dedupe in `togal_client`, reuse existing set, never blind re-upload). Even if Togal state is broken, the local engines carry the bid. |
| **C6** | Combined multi-scale PDF — 0.1875 vs 0.125 → 694 vs 25,811 SF | One global scale across a mixed-scale set | Layer 1 calibrates **per sheet**, not per set. Each sheet locks its own scale or is flagged. The 694-vs-25,811 ambiguity becomes "this sheet's two anchors disagree → UNSCALED" instead of a silent coin-flip. |
| — | Raster/scanned sheets | Vector engine pixel-guesses | Layer 0 detects raster (no vector text/paths) → routes to M-LLM (reads the image) + OCR scale, never lets M-VEC fake-measure a raster. |
| — | Repetitive multifamily (Weddington 85-unit) | Whole-building auto-detect over-counts | Triage flags repetitive → M-LLM measures *per unit-type* × schedule count (the method that actually worked), reconciled against M-PROG unit schedule. |
| — | Federal/restricted, no docs | Pipeline has nothing to measure | Layer 0 detects "no docs" → emits a defensible SF-band estimate labeled REJECT/HUMAN-tier with reasons; never fabricates a GOLD. |
| — | Missing finish spec | Painted scope unknown | Triage flags missing schedule → SILVER cap + explicit assumption flag in the workbook (per CCF "assumptions live in FLAGS"). |

---

## (3) CONCRETE BUILD PLAN FOR `scripts/`

**Guiding principle:** stop adding per-project patch scripts. Build a small set of permanent, testable modules; retire the graveyard.

### KEEP (refactor into the new spine)
- `scripts/_lib/vector_takeoff.py` → becomes **M-VEC**. Add per-sheet scale input (from Layer 1), raster guard, wall-LF from polylines.
- `scripts/_lib/takeoff_scale.py` → absorbed into **`calibrate.py`** (its `detect_set_scale`/`crosscheck_vs_program` are the seeds of the two-anchor gate; upgrade per-page + add OCR back-solve + ≤2% reconcile).
- `scripts/togal_client.py` → keep as thin API client; add **content-hash idempotent upload** (kills C5). Demote everything that treated Togal as the source.
- `scripts/auto_takeoff.py` → becomes the **orchestrator** that runs Layers 0-4 (keep `ensure_detections` only as the M-TOGAL corroborator's internal poll). Stop making Togal mandatory; stop gating on Togal success.
- `data/memory/takeoff_benchmarks.json` → the ledger; add known-actual replay tests.

### WRITE (new permanent modules, priority order)
1. **`scripts/_lib/calibrate.py`** — the per-sheet two-anchor scale gate (regex title-block + vector dimension/gridline ×2 + OCR SF-tag back-solve → SCALE_LOCKED / SHEET_UNSCALED). *Keystone — see (4).*
2. **`scripts/_lib/takeoff_intake.py`** — Layer 0 triage (vector/raster, plan/detail/site classify, multifamily flag, docs-present, OCR if raster). Produces the per-sheet manifest.
3. **`scripts/_lib/llm_takeoff.py`** — **M-LLM** promoted to a first-class engine: Claude reads dimension strings / unit-type plans / finish schedule → measured SF. (Wraps the per-unit-type method that produced the Weddington SILVER; routes via `_lib/llm.py` claude-code-first chain.)
4. **`scripts/_lib/reconcile.py`** — Layer 3 reconciler + confidence gate (GOLD/SILVER/REJECT logic, provenance envelope, loud HARD-fail with reasons).
5. **`scripts/_lib/togal_sanity.py`** — the M-TOGAL gate: reject walls==0, garbage classes, implausible density, off-scale; a pass returns a vote, not a verdict.
6. **`scripts/takeoff.py`** — single clean CLI entrypoint: `takeoff.py <pdf> [--units N]` → runs the whole spine, prints the verdict + per-method numbers + confidence. This replaces every `_wssu_/_wedd_/_clemson_/refire` one-off.
7. **`scripts/_takeoff_regression.py`** — replay against known-actual Cowork workbooks; wire into a pre-commit/daemon check so a change that breaks a known job fails loudly.

### RETIRE (delete or move to `_legacy/` — these ARE the whack-a-mole)
`_wedd_m1_refire.py`, `_togal_m1_refire.py`, `_wssu_fix_scale_extract.py`, `_wssu_paint_workflow.py`, `_wssu_run_ai_takeoff.py`, `_wssu_probe_togal.py`, `_probe_togal_why.py`, `_overnight_retogal.py`, `_clemson_takeoff.py`/`_clemson_units.py`/`_clemson_resume.py`, `togal_ui_classify.py`, `togal_poll_extract.py`, `togal_pull_classifications.py`, `togal_run_takeoff.py`, `togal_takeoff_proper.py`. Their *logic* (re-firing, scale fixing, unit splitting) becomes a parameter/branch inside the permanent modules — not a new file per project. `togal_pipeline.py` collapses into `togal_sanity.py` + the orchestrator.

---

## (4) THE SINGLE MOST IMPORTANT CHANGE TO MAKE FIRST

**Build `scripts/_lib/calibrate.py` — the per-sheet, two-anchor scale gate — and make every measurement engine refuse to run on a sheet that isn't SCALE_LOCKED.**

Why this one first, above even the multi-engine reconciler:

- **It's the highest-leverage root cause.** Scale ambiguity (C3, C6) is what made Weddington's number swing 694 → 25,811 SF and made WSSU 2.25× off. With a wrong scale, *every downstream engine produces a confidently wrong number that agrees with itself* — the reconciler would bless garbage. Lock scale first or nothing downstream can be trusted.
- **It converts a silent failure into a loud, honest stop.** Today an unscaled/mis-scaled sheet ships a number. After this, it returns SHEET_UNSCALED with reasons — the thing the user explicitly demanded ("HARD-fail loudly instead of shipping").
- **It's a self-contained, testable unit** you can validate immediately against the Weddington set and the known-actual Cowork workbooks before any other refactor lands. It also unblocks `vector_takeoff.py` (M-VEC's missing piece per the research) without waiting on the full spine.

Acceptance test for it: on the Weddington 206-page combined PDF, it must (a) lock the correct scale on the floor-plan sheets via two reconciling anchors confirmed by an OCR'd printed SF tag, and (b) flag the mixed-scale/detail sheets UNSCALED rather than measuring them. If a sheet's two anchors disagree, it must STOP, not pick one.

---

**Relevant files:** `C:\Agent Carol\scripts\_lib\vector_takeoff.py`, `C:\Agent Carol\scripts\_lib\takeoff_scale.py`, `C:\Agent Carol\scripts\auto_takeoff.py`, `C:\Agent Carol\scripts\togal_client.py`, `C:\Agent Carol\scripts\togal_pipeline.py`, `C:\Agent Carol\data\memory\takeoff_benchmarks.json`. New modules to create: `calibrate.py`, `takeoff_intake.py`, `llm_takeoff.py`, `reconcile.py`, `togal_sanity.py`, `takeoff.py`, `_takeoff_regression.py` (all under `scripts/` or `scripts/_lib/`).

## ADVERSARIAL CRITIQUE

Both claims check out: the lib files exist and the patch-script graveyard is real and large. I have enough to critique without further digging. This is a design critique, so I'll return it directly.

The architecture is genuinely good — the diagnosis (single trusted source + silent failure) is correct, and inverting the spine to corroboration-plus-gate is the right move. But the prompt asks where it STILL fails. Here it is, adversarially.

---

# ADVERSARIAL CRITIQUE — CCF GOD-LEVEL TAKEOFF

## The meta-flaw first

The architecture quietly assumes **the failure modes are independent and the gate catches the dependent ones.** Its own GOLD rule ("two methods that don't share a failure mode") is the only thing standing between you and confidently-wrong bids. Several scenarios below are exactly the case where the two surviving methods *do* share a failure mode, and the gate blesses garbage. That's the thread running through most of the residual risk.

---

## 1. Scanned / raster-only plans (no vectors)

**Where it still fails.** On raster, M-VEC is dead by design, so Layer 3 has at most M-LLM + M-TOGAL + M-PROG. M-TOGAL on a scan is itself unreliable (it's an image model fed a JPEG of a fax). So realistically you have **M-LLM as the only real measurer, with M-PROG as a magnitude tripwire.** That is SILVER *at best* by your own rule — there is no path to GOLD on a scanned set, ever. Worse: M-LLM reading a blurry scan and M-PROG (program SF) can agree while both being wrong, because the estimator who typed the program SF and the LLM reading the same title block are pulling from a shared upstream number. That's a **correlated SILVER masquerading as corroboration.**

The OCR scale back-solve (Layer 1 anchor C) is the part most likely to silently degrade on raster: OCR misreads "142 SF" as "742 SF" or drops the decimal on a dimension string, and a wrong-but-confident OCR number will *reconcile against itself* if the same digit error feeds both the area tag and your expectation.

**Hardening.**
- Cap raster sets at SILVER **structurally** — make it impossible for the reconciler to emit GOLD when M-VEC didn't run. (State it as code, not policy.)
- Require **two independent OCR engines** (e.g. Tesseract + the LLM's own vision read of the same tag) to agree before an OCR'd number is allowed to anchor scale. Single-OCR scale anchoring is a silent-failure factory.
- Add a **dpi / legibility gate** in Layer 0: below some resolution or contrast threshold, the sheet is UNSCALED-by-illegibility → human, not "let the LLM try its best."
- For the magnitude check, M-PROG must use a source **provably independent of the sheet** (the GC's leasing SF, a published building program, the CRM SF band) — never a number transcribed off the same drawing the LLM just read.

---

## 2. Plans with no printed dimensions to calibrate scale

**Where it still fails.** Layer 1's three anchors are (A) title-block scale text, (B) measured dimension strings/gridlines, (C) OCR'd area tag. On a schematic/concept sheet or a deliberately de-dimensioned design-development set, **B and C don't exist and A is the only anchor** — and a lone title-block "1/8\" = 1'-0\"" is exactly the WSSU failure (printed scale present, actual plot scale different because someone printed-to-fit or reduced to 11×17). The gate requires *two reconciling anchors*; with only one, it correctly routes to UNSCALED. Good — but that means **a large class of real, biddable sheets produce nothing**, and a hurried estimator will experience this as "the tool gave up on a plan I can clearly read." That pressure is what reintroduces the manual-override that becomes the next silent failure.

There's also a subtle trap: gridlines as anchor B. Column grids are frequently **non-uniform** (21'-4", then 18'-0", then 30'-0"). If calibrate.py assumes uniform grid spacing to back out scale, it'll lock a confident wrong scale that passes the ≤2% self-check.

**Hardening.**
- When no dimension exists, allow **one explicit, logged human calibration**: estimator clicks a known length (a door = 3'-0", a parking stall = 9'-0") → that becomes a *recorded anchor with provenance*, not a silent default. This is the pressure-release valve that keeps the gate from being bypassed entirely. The key is it's **logged and replayable**, so it's auditable, unlike today's manual fudge.
- Never infer scale from grid *spacing*; only from a grid *dimension string* that states the spacing. If only gridlines (no stated spacing) exist, that is not a valid anchor.
- "Scale bar" graphic detection as a fourth anchor — many de-dimensioned sheets still carry a printed scale bar, which is plot-accurate even when reduced (the thing title-block text is NOT).

---

## 3. DWG-only sets (no PDF)

**Where it completely falls over.** The entire architecture is PDF-centric: PyMuPDF for M-VEC, page rasterization for OCR, Togal ingests PDFs. **A native AutoCAD .dwg set has no entry point at all** — Layer 0 triage will either reject it or, worse, someone exports a print-to-PDF that *loses the model-space scale* and reintroduces the exact scale ambiguity Layer 1 exists to catch. DWG is not a fringe case for a commercial sub; plenty of GCs share CAD.

This is the architecture's biggest blind spot — it's not mentioned anywhere, which means in practice it becomes **a new one-off script**, i.e. the whack-a-mole the redesign promised to end.

**Hardening.**
- Add a **DWG normalizer in Layer 0**: ODA File Converter (free) or a `ezdxf`-based path to read DXF/DWG directly. DWG is actually the *easy* case — model space is true-scale vector geometry, so M-VEC on DXF is **more reliable than PDF**, not less (no plot-scale ambiguity). This could be a strength, not a gap.
- If no CAD toolchain is acceptable for a one-person shop, then explicitly declare DWG **out of scope and route to manual** — but say so in Layer 0 so it's a known stop, not a silent crash.

---

## 4. Togal being down

**Where it still fails — and this is the good news.** The architecture handles this *correctly by design*: Togal is one optional vote, and M-VEC + M-LLM still reach GOLD/SILVER without it. This is the strongest part of the redesign. The residual risk is narrow but real:

- **Cascade dependency you didn't list.** If your LLM provider chain (`_lib/llm.py` claude-code-first) is *also* down or rate-limited at the same time, then on a **raster or no-dimension set** you've lost M-LLM too, and M-VEC can't run — you're at **zero engines**, not degraded. Togal-down is survivable; Togal-down + LLM-down on a scan is total. The design treats LLM availability as a given; it isn't (the memory notes Gemini truncation and the claude-code login gate).
- **Silent timeout vs. down.** "Down" is easy. The dangerous Togal state is **slow/partial** — it returns rooms but the wall workflow silently times out (literally your C2). Make sure togal_sanity treats a *partial* return as a non-vote, not a low-confidence vote that can still tip a SILVER to GOLD.

**Hardening.**
- M-LLM needs the same demotion discipline as Togal: an LLM call that **truncates, refuses, or returns implausible structure is a non-vote**, not a measurement. Truncation detection is mandatory given the documented Gemini behavior.
- Define the **zero-engine state** explicitly: raster + LLM-unavailable → REJECT/HUMAN with "no measurer available," never a fallback guess.

---

## 5. The estimator in a hurry

**This is where the architecture is most likely to fail in practice, and it's barely addressed.** Every robustness feature here *adds friction*: more UNSCALED stops, more REJECT/HUMAN verdicts, more "two anchors disagree, I won't guess." A correct, honest HARD-STOP that arrives at 3:45 PM on a bid due at 4:00 is, to a hurried estimator, **indistinguishable from a broken tool** — and the documented behavior of this shop is to write a one-off script to force the number through. The whack-a-mole doesn't return through bad engineering; it returns through **deadline pressure routing around the gate.**

The architecture optimizes for *correctness* and treats *throughput under pressure* as someone else's problem. For a one-person shop on bid day, throughput-under-pressure IS the product.

**Hardening.**
- **SILVER must be a first-class shippable deliverable, fast.** The hurried path is: M-LLM reads it, M-PROG sanity-passes, ship SILVER with a ± band and a flag — in seconds, no Togal upload, no waiting. If GOLD requires a Togal round-trip that takes minutes, nobody waits on bid day; make SILVER the *default fast lane* and GOLD the *overnight upgrade*.
- **Time-budget awareness.** Pass the bid deadline into the orchestrator. If it's tight, skip the slow corroborator entirely and emit the best honest SILVER now, labeled. This directly serves the existing 4 PM cutoff law.
- **Make the override legitimate, logged, and bounded** (per #2). An estimator who can record "I'm overriding to scale X because Y" inside the system will use it; one who can't will `python _newjob_force.py` outside it. Give the pressure somewhere safe to go.
- A one-click **"why did this stop?"** that names the specific anchor disagreement, so the fast resolution is *fix the anchor*, not *bypass the tool*.

---

## 6. Maintenance burden on a one-person shop

**Where it still fails.** The proposal trades ~25 brittle one-off scripts for **7 permanent modules with a much higher individual complexity ceiling** — a per-sheet two-anchor reconciler with OCR back-solve, a triage classifier, an LLM engine, a reconciler with provenance envelopes, a regression harness. That's not less code; it's **less code that's much harder to debug**, maintained by one estimator who is not primarily a software engineer and whose actual job is winning bids. When `calibrate.py`'s OCR back-solve misbehaves on a new GC's title-block format, debugging a multi-anchor reconciliation is *far* harder than the old "write a 30-line _newjob_fix.py." The graveyard scripts were ugly but each was independently comprehensible and disposable. **Coupling them into a spine means a bug in calibrate.py now silently affects every bid, not one.**

The regression harness (Layer 4) is the highest-maintenance, lowest-discipline-survival component: it requires curating known-actual workbooks, keeping tolerances current, and *actually running* a pre-commit/CI gate. A one-person shop will let CI rot the first busy week. A green test suite nobody runs is worse than none — it manufactures false confidence.

**Hardening.**
- **Sequence ruthlessly.** Build only `calibrate.py` + the SILVER path + M-VEC first (the proposal's own "do this first" is right). Defer reconciler/triage/regression until the keystone has survived 10 real bids. Don't land 7 modules at once into a one-person shop.
- **Each module must degrade to a comprehensible fallback** the owner can read. If reconcile.py throws, the system should print the raw per-method numbers and stop — never wedge.
- **The regression harness must run automatically in the daemon** (which already runs scheduled jobs), reporting via the existing Telegram channel, not as a pre-commit hook that requires discipline. Make it nag, don't make it gated-on-the-human.
- **Cap the dependency surface.** OCR (Tesseract), PyMuPDF, ODA converter, a vision LLM — every new dependency is a thing that breaks on a Windows update and there's no second engineer. Prefer the fewest external binaries.
- Budget for the truth: a one-person shop will not maintain 7 modules well. **Design for benign neglect** — the system should still produce honest SILVER/REJECT verdicts even if nobody has touched the code in six months and the regression suite is stale.

---

## Does it deliver "no recurring failures"? — Honest verdict

**No. It delivers *no recurring SILENT failures of the types in the taxonomy* — which is a real and large win — but not "no recurring failures."** The distinction matters and you should say it out loud:

**What it genuinely eliminates (the actual win):**
- Single-source silent failure (C1), walls=0 pass-through (C2), and Togal-down sinking a bid — these are structurally killed. A wrong number can no longer ship *silently* as GOLD, because GOLD now requires two independent agreers. That is the whole ballgame and the design wins it.

**What it converts rather than eliminates:**
- Most failures become **loud stops (UNSCALED / REJECT / HUMAN)** instead of wrong numbers. That's strictly better, but it's a *different* recurring cost — recurring **work**, not recurring **error**. The volume of "it stopped, now I have to resolve it by hand" is the new steady-state burden, and on raster/no-dimension/DWG sets that volume is **high**.

**Honest residual risks that remain:**
1. **Correlated-method GOLD.** Two methods sharing a hidden upstream (same OCR'd tag, same transcribed program SF, same mis-rotated sheet) can agree and ship GOLD wrong. The ≤10% gate assumes independence it cannot verify. This is the deepest residual risk and the architecture has no detector for it.
2. **The gate gets bypassed under deadline.** The single most likely real-world failure: a correct HARD-STOP at 3:45 PM gets routed around by a fresh one-off script. The whack-a-mole returns through *human pressure*, not code — and the design doesn't defend this surface.
3. **DWG and severely-degraded scans** have no honest GOLD path and, for DWG, no entry point at all — these become new one-offs unless explicitly normalized or explicitly scoped out.
4. **Zero-engine state** (raster + LLM/Togal both unavailable) is unhandled in the design.
5. **Maintenance decay.** Higher per-module complexity + a regression suite that requires discipline a one-person shop won't sustain → the spine itself develops silent bugs that now affect *every* bid instead of one.
6. **Calibration is necessary but not sufficient.** Locking scale correctly still leaves *scope* errors (which surfaces are painted, walls-to-deck vs. to-ceiling, the GC scope-letter deltas your own memory flags) — the architecture measures geometry honestly and can still bid the wrong scope at the right scale.

**Bottom line:** It's a correct and well-targeted redesign — it turns a *silent-wrong-number* machine into a *loud-honest-stop* machine, which is exactly what was asked for. But "no recurring failures" is overclaiming. The honest claim is: **"no recurring silent mis-bids of the known types; in exchange, a recurring and sometimes heavy load of honest stops, plus a small set of new residual risks (correlated-method GOLD, deadline bypass, DWG/zero-engine gaps, and maintenance decay) that need their own explicit defenses."** Ship it as that, not as a guarantee — and build the deadline-pressure release valve (#5) before you build anything past the calibrate.py keystone, because that's the surface where the whack-a-mole actually comes back.

Verified against the codebase: the cited libs (`scripts/_lib/vector_takeoff.py`, `scripts/_lib/takeoff_scale.py`) exist, and the one-off graveyard the diagnosis blames is real and large (`_wssu_*`, `_clemson_*`, `_wedd_m1_refire.py`, `_togal_m1_refire.py`, plus ~12 `togal_*` scripts under `C:\Agent Carol\scripts\`) — so the root-cause diagnosis is grounded, not hypothetical.

## FAILURE ANALYSIS

Perfect. Now let me compile the comprehensive failure taxonomy based on everything I've read. Let me create a comprehensive report:

## FAILURE TAXONOMY: CCF Togal Takeoff Incident History

Based on systematic mining of C:\Agent Carol\scripts (togal_pipeline.py, auto_takeoff.py, togal_client.py, vector_takeoff.py, takeoff_scale.py, _togal_m1_refire.py, _wedd_m1_refire.py, _probe_togal_why.py) and data/memory/takeoff_benchmarks.json, here is the complete documented failure history:

---

### CATEGORY 1: AI SILENT FAILURE (Detection Workflow Not Fired)

**INCIDENT: WSSU (6/8)**
- **Root Cause**: POST /v1/page-processing/run (paint/full workflow) was never called. The scale endpoint alone returns zero rooms because Togal's AI detection never runs without the explicit workflow trigger.
- **Evidence**: togal_pipeline.py lines 1310-1312 ("CRITICAL: every extraction returned 0 rooms until POST /v1/page-processing/run was added (WSSU incident)")
- **Failure Mode**: walls[] arrays empty; extracted zero room SF despite valid page scaling
- **Fix Applied**: Added mandatory POST /v1/page-processing/run per page with view_id (lines 1313-1322) before extraction
- **Why It Recurred**: The fix became standard in togal_pipeline.py but auto_takeoff.py uses the ensure_detections() wrapper (auto_takeoff.py line 174) which MUST be called for every new takeoff; omitting --full flag skips the detection fire entirely
- **Current Status**: Mitigated but requires explicit --full flag; detection can still timeout silently if a page has raster/poor quality content

---

**INCIDENT: UMC + Old Navy (6/12)**
- **Root Cause**: Same as WSSU — detection POST /v1/page-processing/run was fired but with incorrect workflow_type parameter and no polling for feature population. Togal's API returns HTTP 200 immediately as a queue ACK and never signals errors; silent failures discovered only when examining the returned geojson (features=[]).
- **Evidence**: auto_takeoff.py lines 409-413 ("6/12 ROOT-CAUSE FIX (UMC/Old Navy zero-detection): Togal ACKs detection in milliseconds and fails SILENTLY — a fixed settle sleep proves nothing. Fire per page with the EXPLICIT view_id…")
- **Failure Mode**: Zero rooms detected; returned features[] on every page despite API HTTP 200 responses
- **Fix Applied**: (a) Fire with explicit view_id for EACH page (not batched); (b) Poll /v1/view/{view_id}/geojson until len(features) >= threshold (10) before declaring success; (c) Emit loud "NEVER POPULATED after Xmin" warning if timeout
- **Wrapper**: ensure_detections() function (auto_takeoff.py lines 174-208) — polls geojson for up to 12 minutes with 45-second intervals
- **Why Incomplete**: Detection can hang on raster pages or corrupted geometry; no way to distinguish "still processing" from "will never populate" — user must manually inspect page content and choose LABELED budget method as fallback (line 207)
- **Current Status**: Mitigated in auto_takeoff.py; togal_pipeline.py still lacks this polling (only calls once)

---

### CATEGORY 2: WALL DETECTION MISSING (Rooms Detected, Walls = Zero)

**INCIDENT: Weddington Road Apts (6/21)**
- **Root Cause**: Pipeline read room auto-detections from AI (56,267 noisy "rooms" with classifications like "Shafts"/"Hotel Room") but walls[] arrays were ALWAYS empty across all 206 pages. The paint workflow was never fired or fired with workflow_type="full" which detects rooms but not wall centerlines. Wall detection requires explicit workflow_type="paint" re-firing.
- **Evidence**: _wedd_m1_refire.py documents "the pipeline only read room auto-detections (walls=0)"; the script re-fires with workflow_type="paint" (line 59) to populate wall classifications
- **Failure Mode**: Extracted footprint SF correctly but wall_lf=0; geometry centerline data unpopulated despite page processing appearing complete
- **Fix Applied**: Manual re-fire script _wedd_m1_refire.py calls POST /v1/page-processing/run with workflow_type="paint" on floor-plan pages only, polls /v1/classification until wall classes appear (walls, partitions, GWB, drywall), then sums LineString features from view geojson to produce M1 wall LF
- **Why Incomplete**: Root cause was togal_pipeline.py calling workflow_type="full" (generic) instead of "paint" (wall-specific); auto_takeoff.py correctly uses "paint" (auto_takeoff.py line 186) but the combined-PDF set had orphan duplicate sets from prior failed retries, preventing the paint workflow from properly associating with the correct set_id
- **Current Status**: Requires manual re-fire + M2 vector fallback; togal_pipeline.py workflow_type not exposed as parameter (hardcoded to "full")

---

### CATEGORY 3: WRONG SCALE (Automatic Default or Unread Title Block)

**INCIDENT: WSSU Scale Mismatch (6/8)**
- **Root Cause**: Title block stated 3/16"=1'-0" but pages were processed at Togal's default 1/8" — all measurements came back 2.25x too large (96 scale factor at 1/8" vs 192 at 3/16")
- **Evidence**: _wssu_fix_scale_extract.py documents the issue; takeoff_scale.py lines 1-5 ("kills the silent-wrong-scale class of error (WSSU ran at the 1/8" default while title block said 3/16" → every area 2.25x off)")
- **Failure Mode**: Footprint SF inflated by (0.25/0.1875)^2 = 1.78x; rooms reported correct but in wrong real-world units
- **Fix Applied**: (a) detect_sheet_scale() function in takeoff_scale.py parses ALL scale notations from sheet text via regex, majority-votes, tie-breaks by proximity to "FLOOR PLAN" label; (b) detect_set_scale() scans every page for <=1/8" plan-scale notations + floor-plan keywords to exclude detail/section scales; (c) crosscheck_vs_program() compares measured footprint vs program SF to expose scale errors even when title block is unreadable
- **Why Incomplete**: detect_sheet_scale() only reads page 0 (cover page often has no plan scale → "scale NOT confirmed"); fixed in detect_set_scale() which scans all pages but multi-scale documents (permit sets mixing floor plans + elevations + details) can report conflicting scales; Weddington COMBINED 206-page permit set has non-standard naming and the scale-detection hit ambiguity (0.1875 vs 0.125) with no confidence threshold enforced
- **Current Status**: Implemented but auto_takeoff.py gates it as non-HARD requirement; gate still emits REVIEW even if scale_confirmed=false (auto_takeoff.py line 224-225)

---

### CATEGORY 4: PAGE SELECTION / SHEET FILTERING (Too Many or Too Few Pages)

**INCIDENT: WSSU Sheet Naming (Non-Standard)**
- **Root Cause**: The 206-page Weddington permit set uses plain numbering (page 0-205) and descriptive names, not standard AIA sheet prefixes (A201, A301, etc.). Tier 1 strict prefix matching returned zero pages; Tier 2 keyword matching ("floor plan", "rcp") also failed on many pages; Tier 3 fallback used ALL 206 pages, including site plans, details, title blocks, legends — massive over-detection and noise
- **Evidence**: togal_pipeline.py lines 303-347 (filter_painting_pages function with 3-tier fallback strategy); line 346 "Use ALL pages for takeoff" comment marks this as a known risk
- **Failure Mode**: 56,267 rooms detected (2.5x over-count); classifications included "Shafts", "Parking", "Elevator" — non-painting scope polluting results
- **Fix Applied**: (a) Filter non-painting classifications at extraction (togal_pipeline.py lines 626, NON_PAINTING_CLASSIFICATIONS set); (b) statistical outlier removal: Q3+4*IQR fence to discard building outlines (togal_pipeline.py lines 637-653); (c) deduplication: group pages with ±15% similar SF and pick primary (togal_pipeline.py lines 681-698)
- **Why Incomplete**: These filters are post-processing; the AI still fires on all 206 pages (expensive, slow, noisy); Tier 2 keyword matching still misses permit sets with generic page names; no way to exclude site plans / demo plans before sending to Togal
- **Current Status**: Page filtering happens AFTER extraction; user can manually specify --sheets to force exact matches (togal_pipeline.py line 438) but this requires knowing the correct sheet names in advance

---

### CATEGORY 5: COMBINED-PDF DUPLICATION (Orphan Sets from Failed Retries)

**INCIDENT: Weddington Road Apts (6/21)**
- **Root Cause**: The 164MB combined permit PDF was uploaded multiple times due to timeout / retry logic; Togal created 3 orphan duplicate sets associated with the same project, each with partial geometry data. The paint workflow was fired on orphan sets that had only room detections; the primary set that should have gotten the paint workflow fell back to reading the orphan sets' incomplete data
- **Evidence**: _wedd_m1_refire.py comment "project had 3 orphan duplicate sets from retries" (background context from user problem statement)
- **Failure Mode**: Classification data never populated on the correct set_id; Togal's /v1/page-processing/run found no matching pages to fire on; wall detection request went to a wrong/stale set copy
- **Fix Applied**: Manual identification of correct set_id by timestamp, explicit re-fire on that set only; togal_pipeline.py now names sets with unique timestamps (line 1408) so re-runs never collide with stale sets
- **Why Incomplete**: No API-level deduplication of sets; if upload times out, retry logic creates a new set silently; orphan sets remain in the account and confuse classification lookups (Togal's /v1/classification query is by organization_id + set_id but orphan sets have wrong names/data)
- **Current Status**: Mitigated by unique timestamp-based set naming (togal_pipeline.py line 1408); cleanup of orphan sets still manual

---

### CATEGORY 6: SCALE AMBIGUITY IN COMBINED PDFS (Non-Standard DPI / Raster Content)

**INCIDENT: Weddington Road Apts (6/21)**
- **Root Cause**: The combined permit set has non-standard scanning DPI and mixed raster/vector content. Vector extraction via PyMuPDF (M2 local engine) returned inconsistent footprint SF (694 SF vs 25,811 SF) depending on assumed scale (0.1875 vs 0.125). The scale-detect regex found both notations on different pages; no clear consensus; crosscheck_vs_program algorithm exposed the ambiguity but could not resolve it
- **Evidence**: vector_takeoff.py lines 1-18 ("No cloud, no AI, no silent failures — pure math… trust = two INDEPENDENT measurements agreeing"); auto_takeoff.py lines 422-444 (M2 vector engine cross-measure); takeoff_scale.py lines 86-127 (detect_set_scale with plan_max threshold)
- **Failure Mode**: M1 (Togal) and M2 (local) disagreed by >100%; could not achieve GOLD (<=10% agreement required); fell back to SILVER per-unit-type method
- **Fix Applied**: (a) Distinguish plan scales (<=1/8") from detail scales (>=1/4") in detect_set_scale() to exclude enlarged unit plans; (b) min_wall_run tuned to 4.0 ft for dense residential (hotel, apartment) to capture short partitions (auto_takeoff.py lines 435-439, "6/15: closed Marriott M1/M2 gap from Δ15% to Δ1% = GOLD"); (c) user supplies explicit --height and facility type to tighten confidence bounds
- **Why Incomplete**: Combined PDFs with raster pages or OCR-unfriendly title blocks can still refuse to reveal scale; no automated OCR fallback; crosscheck_vs_program requires knowing the program SF in advance (extracted from code sheet, not available for all bid types)
- **Current Status**: Mitigated by M2 tuning; still requires manual height specification and program SF lookup; scale ambiguity gates to REVIEW, never ships GOLD automatically

---

### CATEGORY 7: API INSTABILITY / HTTP ERRORS

**INCIDENT: 350 Hein HTTP 500 on /v1/page (6/16)**
- **Root Cause**: Re-fire attempt on 350 Hein called POST /v1/page-processing/run successfully but subsequent GET /v1/page returned HTTP 500, blocking page listing and view creation. The project had legitimate geometry but API was in a degraded state.
- **Evidence**: _togal_m1_refire.py documents the 350 Hein "silent failure"; togal_client.py line 104 comment "the 350 Hein 'Togal silent failure'" references the incident
- **Failure Mode**: Page-listing 500 error; downstream extract call received empty page list; classifications never populated
- **Fix Applied**: (a) Retry logic with fallback endpoints (togal_pipeline.py lines 365-369, try /v1/page then fallback to /v2/page for PUT scale); (b) catch-all exception handling in ensure_detections() to emit diagnostic and continue polling (auto_takeoff.py lines 188-189); (c) manual re-fire via _togal_m1_refire.py with raw requests + diagnostic headers to isolate the issue
- **Why Incomplete**: No automatic retry-with-backoff strategy; HTTP 500 on one page fails the entire extraction; session token expiry (7-day TTL) not distinguished from API errors (togal_client.py line 104 comment warns session-error handling is incomplete)
- **Current Status**: Users must manually retry or wait for Togal's API to recover; no circuit-breaker or graceful degradation

---

### CATEGORY 8: CLASSIFICATION POPULATION TIMEOUT

**INCIDENT: Weddington + All Multi-Page Projects**
- **Root Cause**: POST /v1/page-processing/run returns immediately with HTTP 200 (queue ACK) but classifications populate asynchronously. Togal's API has no job-status endpoint; the ONLY way to detect completion is polling /v1/classification until rows appear. Some large projects (206 pages) take >10 minutes to populate; default 12-minute timeout (auto_takeoff.py line 191) is barely sufficient.
- **Evidence**: ensure_detections() function (auto_takeoff.py lines 174-208) documents "discovered 6/12 after it silently produced nothing for two projects"; _wedd_m1_refire.py polls up to 8 minutes (line 65)
- **Failure Mode**: Timeout waiting for classifications; loop exits with pending pages; loud warning emitted ("NEVER POPULATED after Xmin") but no automatic fallback
- **Fix Applied**: (a) Polling loop with configurable max_min (default 12 minutes, lines 191-207); (b) per-page feature-count check >=10 as success threshold (line 200) to avoid false negatives; (c) loud diagnostic message listing pages that failed (lines 203-207)
- **Why Incomplete**: 12 minutes is a heuristic; very large drawing sets (200+ pages) can exceed this; no exponential backoff or early-exit on error (if a page errors once, it never recovers in that run); raster content pages never populate features and consume the entire timeout budget
- **Current Status**: Works for standard projects; large permit sets are timing-risk; user must manually extend max_wait parameter

---

### CATEGORY 9: OVER-DETECTION (Noisy AI Classifications)

**INCIDENT: Weddington + Old Navy + Banana Republic (6/16, 6/12)**
- **Root Cause**: Togal's AI auto-detection classifies every enclosed polygon as a "room"; permit sets and retail spaces trigger mass false positives. Old Navy returned 56,000+ detections including "Hotel Room", "Shaft", "Parking Lot" classifications that are non-painting scope. Banana Republic 3/16" scale version showed 2800 LF M1 vs 5318 LF M2 (89.9% delta, marked REVIEW, later found to be measurement error not detection error).
- **Evidence**: takeoff_benchmarks.json shows Old Navy delta_pct=529.7% (M1 1692 vs M2 10654 LF); Banana Republic delta_pct=89.9%; both marked REVIEW confidence
- **Failure Mode**: False room detections bloat footprint SF; non-painting classifications pollute totals; need aggressive filtering post-extraction
- **Fix Applied**: (a) NON_PAINTING_CLASSIFICATIONS set (togal_pipeline.py lines 108-112) to exclude "Parking", "Shafts", "Elevator", "Balcony", etc. in _filter_page_rooms(); (b) statistical outlier removal (IQR-based, lines 637-653) to discard building outlines and site boundaries; (c) M4 wall-floor-ratio sanity check (auto_takeoff.py lines 249-253) gates takeoffs where geometry is physically impossible; (d) M1 vs M2 independent measure agreement (auto_takeoff.py lines 243-247, <=25% for HIGH, <=10% for GOLD) as cross-validation
- **Why Incomplete**: Filters are heuristic-based; retail spaces with many small rooms legitimately have high wall/floor ratios; filtering can over-correct and hide real scope; M2 vector engine itself can fail to parse complex spaces (compact hotels, open-plan with few walls)
- **Current Status**: Mitigated by M1 vs M2 comparison gate; still falls back to REVIEW for 25-100% deltas; Banana Republic case shows M1 can have its own errors (later measurements showed the 2800 LF was wrong, not the 5318 M2)

---

### CATEGORY 10: MISSING HEIGHT SPECIFICATION

**INCIDENT: All Takeoffs (Ongoing)**
- **Root Cause**: Ceiling heights are a HARD requirement for valid wall face SF calculation (wall SF = perimeter LF × height). Togal measures perimeter LF but not height; sheets must be read manually for "CEILING HEIGHTS TO BE 9'-0"" notes. Many bid sets don't include reflected ceiling plans or interior elevations.
- **Evidence**: auto_takeoff.py lines 235-239 "HARD REQUIREMENT (6/12): no real height evidence = FAIL, never a quiet 9' default"; parse_ceiling_height() function (auto_takeoff.py lines 93-100) scans RCP/A-sheets text for height notations; gate check (lines 237-239) forces explicit --height supply when parsing fails
- **Failure Mode**: Gate emits REVIEW even if all other checks pass; wall SF cannot be derived without known height
- **Fix Applied**: (a) Automatic parsing of sheet text via regex patterns for "CEILING HEIGHTS TO BE 9'-0" A.F.F.", "9' - 0" A.F.F", etc.; (b) user supplies --height flag to override parsing if sheets are unreadable; (c) confidence gate flags any assumed height as non-GOLD (auto_takeoff.py line 283-284)
- **Why Incomplete**: Parsing is fragile; OCR-unfriendly raster sheets return zero matches; 9' is a reasonable default but the gate forces explicit specification, which is correct but requires user discipline
- **Current Status**: Working; requires user to read sections or pass --height; no automatic fallback to facility-type median (could be added)

---

### CATEGORY 11: EXTRACTION METHOD AMBIGUITY (GeoJSON vs Classifications)

**INCIDENT: All Projects (Ongoing Design Debt)**
- **Root Cause**: Togal has two measurement sources: (a) user-drawn classifications (authoritative, proper SF/LF from drawing API, zero noise) and (b) AI-detected GeoJSON features (automated, noisy, requires post-filtering). The system auto-tries (a) first and falls back to (b), but doesn't distinguish between "no classifications exist" and "classifications are incomplete/corrupt".
- **Evidence**: togal_pipeline.py lines 1326-1328 "Try user-drawn classifications first (authoritative)"; 1156-1201 try_classifications() function with fallback; but takeoff_benchmarks.json shows most projects using source="ai_detected_geojson" because users don't manually draw classifications in Togal UI
- **Failure Mode**: Most projects default to noisy AI measurements; high REVIEW rate due to post-filter uncertainty
- **Fix Applied**: (a) extract_from_classifications() function (togal_pipeline.py lines 150-200) to cleanly convert user-drawn data; (b) extract_measurements() function (lines 541-615) to convert GeoJSON with pixel-to-real-world math (scale_factor, DPI, 144-pixel-area quirk); (c) save_results() prefers classifications if present (lines 1221-1233)
- **Why Incomplete**: Users don't manually draw classifications (requires Togal UI work, slow, defeats "automated" goal); the system is designed to work with AI-detected geojson but that's inherently noisy; no way to force-promote to user-driven without manual Togal UI session
- **Current Status**: Working but low-confidence by design; GOLD requires M1 vs M2 agreement + real height + passing all gate checks; most projects land in REVIEW due to M1/M2 mismatch or missing height

---

### CATEGORY 12: TOGAL API RESPONSE SHAPE INSTABILITY

**INCIDENT: Various (Ongoing)**
- **Root Cause**: Togal's API responses vary by endpoint version, project age, and set state. GeoJSON feature keys inconsistently use "area" (pixel area / 144, undocumented), "perimeter", "measurement", "length", "value" (togal_pipeline.py lines 581-585); page data has both "scale_drawing" and "predicted_scale_drawing"; views return either dict or list format for classifications.
- **Evidence**: togal_pipeline.py defensive code: line 583 "Togal's area property = actual pixel area / 144 (consistent API quirk)" with comment about undocumented behavior; lines 553-555 fallback chain for scale ("scale_drawing" or "predicted_scale_drawing" or default 0.125)
- **Failure Mode**: Code must handle every shape variant; a breaking response change can silently corrupt data (e.g., missing the /144 adjustment)
- **Fix Applied**: (a) Defensive fallback chains with explicit comments documenting quirks (lines 553-556, 581-585, 593-596); (b) type-checking before accessing nested keys (lines 568-569, 594-596); (c) default values at every step to avoid KeyError crashes
- **Why Incomplete**: No schema validation or versioning from Togal; brittleness is inherent to the API design; new response shapes discovered by incident (Weddington encountered a response shape the code didn't expect)
- **Current Status**: Mitigated by defensive coding; new incidents require code patches + re-run

---

## SUMMARY BY CATEGORY (Incident Count)

| Category | Count | Status | Root Cause |
|----------|-------|--------|-----------|
| AI Silent Failure (Detection Not Fired) | 3 | MITIGATED | Togal returns HTTP 200 as queue ACK before AI completes; no job-status endpoint |
| Wall Detection Missing | 1 | MITIGATED | Wrong workflow_type (full vs paint); orphan sets from retries |
| Wrong Scale | 1 | MITIGATED | Title block unread; default 1/8" applied instead of 3/16" |
| Page Selection / Sheet Filtering | 1 | MITIGATED | Non-standard sheet naming; fallback to all pages generates noise |
| Combined-PDF Duplication | 1 | MITIGATED | Retry logic creates orphan sets; cleanup manual |
| Scale Ambiguity (Non-Standard DPI) | 1 | MITIGATED | Raster content + mixed scales; no clear consensus; M2 crosscheck fails |
| API Instability | 1 | MITIGATED | HTTP 500 on page-listing; no retry-with-backoff |
| Classification Timeout | 1+ | MITIGATED | 12-minute default insufficient for 200+ page sets |
| Over-Detection (Noisy Classifications) | 3+ | MITIGATED | AI flags non-painting scope; post-filter aggressive |
| Missing Height Specification | All | MITIGATED | Height notes require manual parsing; no automatic fallback |
| Extraction Method Ambiguity | All | ACCEPTED | Two sources (classifications vs geojson); geojson is noisy by design |
| API Response Shape Instability | 2+ | MITIGATED | Quirks documented; defensive code handles variants |

---

## ARCHITECTURAL DEBT (Takeoff v3 Design Issues)

1. **Single Point of Failure**: Togal is the only M1 source; if their AI fails, no measurement. M2 local vector engine exists but has its own scaling ambiguities. No tertiary fallback (facility wall-ratio M4 is a sanity check, not a measurement).

2. **Async Polling Without Job Status**: Detection fires asynchronously but Togal's API has no job-status endpoint. Only truth is polling geojson until features appear. Timeout tuning is heuristic (12 min); some projects exceed it silently.

3. **No Schema Validation**: Response shapes vary; defensive coding is fragile. A Togal API change breaks the pipeline without warning.

4. **Manual Scale Confirmation**: detect_set_scale() scans title blocks but raster PDFs fail OCR; user must supply --height flag. No automated fallback to facility median or program SF crosscheck alone.

5. **Orphan Set Accumulation**: Retry logic on upload creates orphan sets; no API-level deduplication or cleanup. Users must manually identify correct set_id.

6. **Noisy AI By Design**: GeoJSON feature detection includes all enclosed polygons; post-filtering is aggressive (IQR outlier removal, non-painting classification exclusion) but heuristic-based. Legitimate dense spaces (hotels) can be over-corrected.

---

## KEY FAILURE MODES AND FIXES

| Failure Mode | Root Cause | Mitigating Code | Still Incomplete? |
|---|---|---|---|
| 0 rooms extracted (walls/geojson empty) | POST /v1/page-processing/run never called OR fired with wrong workflow_type | ensure_detections() + explicit workflow_type="paint" re-fire | Yes: raster pages timeout; no job-status API |
| 2.25x scale error (wrong real-world units) | 1/8" default instead of 3/16" from title block | detect_set_scale() + crosscheck_vs_program() | Yes: raster scales unreadable; requires program SF |
| 56K false detections (non-painting scope) | AI classifies all enclosed polygons | NON_PAINTING_CLASSIFICATIONS filter + IQR outlier removal | Yes: heuristic-based; can over-correct |
| Scale ambiguity in combined PDFs (M1 vs M2 conflict) | Mixed raster/vector; non-standard DPI; multiple scale notations | M2 min_run_ft tuned to 4.0 for dense spaces; M1 vs M2 gate <=10% for GOLD | Yes: combined PDFs still fail; no OCR fallback |
| Wall SF = 0 (height missing) | Ceiling heights not noted on sheets | parse_ceiling_height() regex + --height flag | Yes: OCR fails on raster; user discipline required |
| Orphan sets from retries | Upload timeout + automatic retry | Unique timestamp-based set naming | Yes: orphans still accumulate; no cleanup API |

---

## WEDDINGTON ROAD APTS (Current Session Failure) — ROOT CAUSES

1. **Wall Detection Zero**: Pipeline fired workflow_type="full" but should have used "paint" for wall-specific AI. Orphan sets from prior uploads confused set_id resolution.
2. **Room Over-Detection**: 56,267 auto-detections on 206-page permit set; filtering removed "Shafts", "Parking", "Elevator" but still noisy.
3. **Scale Ambiguity**: Detected both 0.1875 and 0.125; M2 vector engine produced 694 vs 25,811 SF depending on assumed scale (400% delta).
4. **Classification Population Timeout**: 206 pages took >10 minutes to populate; polling loop nearly hit 12-minute default ceiling.
5. **HTTP 500 on /v1/page**: Re-fire attempt got 500 error on page-listing, blocking downstream extract.

**Outcome**: Could not produce GOLD dual-measure; fell back to per-unit-type measured takeoff (SILVER) by LLM agents reading dimensioned unit plans.

---

All file paths are absolute: C:\Agent Carol\scripts\{togal_pipeline.py, auto_takeoff.py, togal_client.py, _lib/vector_takeoff.py, _lib/takeoff_scale.py, _togal_m1_refire.py, _wedd_m1_refire.py, _probe_togal_why.py}, C:\Agent Carol\data\memory\takeoff_benchmarks.json.

---

I now have the complete picture across all three layers (togal_pipeline M1, vector_takeoff M2, the auto_takeoff gate + scale detector). I don't need to read more — the architecture's structural flaws are visible in the code I've read and proven by the benchmark ledger. Here is the first-principles analysis.

---

# Root-Cause Analysis: Why the Takeoff Architecture Keeps Failing in New Ways

## The core finding

The dual-measure design is sound as a *trust-gate* (never ship a number two independent methods don't confirm). But it was built on a false premise: **that you have two reliable measurements to compare.** In practice you have ~1.2 measurements — one cloud black-box that fails silently in a new way each project (M1), and one local engine whose answer is a *function of an unknown input* (the scale, M2). The gate then faithfully reports "they disagree" (REVIEW) on almost every run. The ledger proves it: **8 of 11 runs REVIEW, 3 with M1=0, only 2 HIGH — both the same Marriott run after a human hand-narrowed the sheets.** Zero GOLD, ever.

The whack-a-mole isn't bad luck. Five structural flaws *guarantee* a new failure per project. None is a bug; each is a design choice that assumed the world is more regular than construction documents are.

---

## Flaw 1 — Togal is a black-box oracle with no contract and no failure signal

`togal_pipeline.run_full` orchestrates a 6-stage remote state machine (upload → split → scale → view → vectorize → detect) where **every stage can fail silently and the only ground truth is "did features eventually appear on the view."** The code already documents this in `ensure_detections`: Togal ACKs detection in ~50ms as a queue receipt, surfaces no errors on any endpoint, and returns zero features. The pipeline is a pile of empirical workarounds — `/v1` then fall back to `/v2`; GET returns empty so POST for geojson; `$where` is "broken for views" so scan 500 recent rows; 409 means find-the-view-elsewhere; "GET /v1/page/{id} often returns 404." 

This is the literal definition of whack-a-mole: **the integration encodes a hundred observed behaviors of an API that has no stable contract.** Each new project exercises an untested path (a new page-state ordering, a new orphaned-set race, a 500 on page-listing as in Weddington), so a new workaround is needed. You cannot make a black-box oracle reliable by adding more special cases — the special-case surface is unbounded because the oracle's behavior is unbounded and not yours to fix.

**Structural fix direction:** Togal must be demoted from *the measurement* to *one optional, fully-quarantined corroborator.* The system must be able to produce a trustworthy, shippable takeoff with Togal entirely absent. Today it cannot — auto_takeoff hard-`return 4`s if `view_ids.json` is missing, i.e. no Togal = no takeoff.

## Flaw 2 — Scale is an unverified free parameter, and every quantity scales with its square

This is the deepest flaw because it poisons *both* measurers at once, defeating the entire point of independent measurement. `vector_takeoff` takes `scale_drawing` as a required input and trusts it blindly; SF scales with `scale²`. The Weddington symptom is the proof: 694 SF vs 25,811 SF "depending on assumed scale (0.1875 vs 0.125)" — a 1.5× scale error becomes a 2.25× area error, and a 37× swing is reported because the geometry that survived the wall-filter also changed.

`takeoff_scale.py` is the right idea — read the title block, majority-vote, drop detail scales — but it is **text-dependent and fails exactly when it matters most:**
- Vector PDFs with text-as-curves (the documented CLI-override case) → no readable notation → no scale.
- Combined 206-page permit sets → the cover/index has no plan scale; multiple disciplines at multiple scales; `detect_set_scale` keyword-gates on "FLOOR PLAN/KING/SUITE…" and silently misses anything worded differently.
- A single wrong majority vote sets the scale for the whole run with no independent check.

And critically: when scale auto-detection fails, the system **falls back to a default** (`extract_measurements` line 553: `or 0.125`; `auto_takeoff` line 383: `height = 9.0`; pipeline `_get_scale_values` returns `0.125`). The codebase's own stated law is "never silently default a scale" — yet three separate code paths default it. The gate's `scale_confirmed` check catches the *missing* case but **not the wrong-but-present case**, because a defaulted 0.125 looks confirmed.

**Structural fix direction:** Scale must become a *measured, self-verified quantity*, not an input — recovered from the drawing's own metric invariants (a dimensioned string "24'-0" whose endpoints span N points → pt/ft directly; door leaves cluster at 3'/3'-6"; the printed graphic scale bar; program SF on the code sheet). The `crosscheck_vs_program` function already knows the math (`ratio ≈ (true/used)²`) — that should be the *primary* scale solver run on every page, not an optional afterthought.

## Flaw 3 — The combined PDF destroys every per-page assumption

The architecture implicitly assumes "a PDF = a building at one scale." A 164MB combined permit set is the opposite: civil + structural + arch + MEP + details + schedules, multiple scales, multiple buildings/unit-types, rotated sheets, raster scans mixed with vector. Two failure modes cascade from this:

- **M1 over-detection:** 56,267 "rooms" with classifications like "Shafts"/"Hotel Room" — Togal detected across all 206 pages including details and schedules, and `postprocess_takeoff`'s statistical-outlier + dedup heuristics (IQR fences, ±15% SF grouping, "typical room density" scoring) are tuned for a clean floor-plan set and produce garbage on this input.
- **M2 scale-mixing:** `vector_takeoff` measures whatever pages it's handed at *one* scale; feed it detail sheets at 1/4" while assuming 1/8" and the LF/SF are silently wrong.

The mitigation that *worked* on Marriott was a human narrowing to the floor-plan sheets (`plan_pages`). That's the tell: **the system has no reliable, automatic "which pages are the paintable-area floor plans at scale S" stage** — the one stage that everything downstream depends on. `filter_painting_pages` is a name-prefix guess (A201…) with keyword and "use ALL pages" fallbacks; on a permit set with non-standard numbering it lands on "use ALL 206 pages," which is how you get 56k rooms.

**Structural fix direction:** A first-class **sheet-classification + page-segmentation stage** is the missing keystone. Split the combined PDF into discipline/scale-homogeneous groups; select the architectural floor-plan pages by *content* (room polygons + door arcs + a detected plan scale), not filename; measure each group at its own self-recovered scale; never sum across scales.

## Flaw 4 — The gate measures *agreement*, not *correctness*, and agreement is rare by construction

`run_gate` is well-built but it answers the wrong question. GOLD requires M1 and M2 within 10%. Given Flaws 1–3, M1 is frequently 0 or noise and M2 is scale-sensitive — so disagreement is the *default outcome*, and the gate dutifully stamps REVIEW. The ledger is the gate working perfectly on a broken pipeline: 529%, 83%, 90%, 16% deltas; null deltas when M1=0.

Two deeper problems:
- **Two correlated-failure measurers aren't independent.** Both consume the same (possibly wrong) scale via `eff_scale` (line 426: M2 even *borrows* Togal's page scale when the title block is unreadable). If both use a wrong scale they can *agree* and earn HIGH while both wrong — false confidence — or, when M1 dies, never agree. Independence is the entire premise of dual-measure and the code quietly violates it.
- **No anchor to reality.** The only check tying to ground truth is `program_sf_crosscheck`, and it's skipped (passes vacuously) when no program SF is supplied. A robust gate should be *anchored*, not just *internally consistent*: measured footprint vs the architect's printed gross SF, wall LF vs perimeter geometry, doors vs door schedule count. Agreement among methods that share a blind spot is not correctness.

**Structural fix direction:** Reframe the gate around **a documentary anchor** (program/finish-schedule SF, door-schedule counts, dimension strings) as the truth source, with M1/M2 as *redundancy* against it. Confidence = "two methods agree *and* both match the document's own stated quantities."

## Flaw 5 — The codebase itself is the whack-a-mole made visible

16 `togal_*.py` scripts plus `_wssu_*`, `_clemson_*`, `_togal_m1_refire`, `_overnight_retogal`, `_probe_togal_why`, `_fix_dotenv`, `_wssu_fix_scale_extract`. Each is a fossil of one project's failure handled as a one-off rather than folded into one engine. There is no single takeoff entry point with a documented contract; there are many overlapping scripts that each re-implement auth, paging, scale, extraction slightly differently. This *organizationally* guarantees recurrence: a fix made in `auto_takeoff` doesn't reach `togal_run_takeoff`; the next failure spawns the next `_probe_` script. Whack-a-mole at the code-architecture level mirrors whack-a-mole at the API level.

---

## The invariants a robust takeoff MUST guarantee

These are the contract the redesign should be held to — each directly answers one of the flaws above:

1. **No silent defaults, ever — fail loud or refuse.** Missing/unverified scale, height, or sheet-set must HALT to a named human question. A defaulted 0.125 or 9' is the most dangerous output because it looks confident. (Today: three default paths exist.)

2. **Scale is measured and self-verified per page, never an input.** Every page that contributes area must carry a scale recovered from the drawing's own invariants (dimension string, graphic bar, door-leaf cluster, program-SF back-solve) and cross-checked by at least two of those. A page whose scale can't be self-verified does not contribute SF.

3. **At least one measurement must be local, deterministic, and Togal-independent.** The system must produce a shippable, gated takeoff with the cloud oracle entirely offline. Togal is a *bonus corroborator*, never a prerequisite. (Today: no Togal → `return 4`.)

4. **Measurers must be genuinely independent** — not sharing the same scale, the same page selection, or the same blind spot. Agreement only earns confidence when the inputs that could be jointly wrong are themselves independently verified.

5. **Confidence is anchored to the documents, not to internal consensus.** The ground truth is the architect's printed numbers: finish-schedule areas, room schedule, door schedule, dimension strings, COMcheck/energy SF (per the existing "documents are ground truth" law). Method agreement is redundancy *around* that anchor, not a substitute for it.

6. **Page selection is by content, not by filename.** "This page is an architectural floor plan at scale S showing paintable rooms" must be decided from detected geometry (closed room polygons + door arcs + a recovered plan scale), so combined permit sets and non-standard sheet numbering can't route the engine into "measure all 206 pages."

7. **Heterogeneous input is segmented before measurement.** A combined PDF is split into scale-and-discipline-homogeneous groups; quantities are never summed across scales or across the same physical area shown on multiple sheets.

8. **Every quantity carries provenance and a falsifiable check.** Each number states its method tier, the scale and source page it came from, and the anchor it was validated against — so a failure is *localized and named* ("page 142 SF rejected: scale unverifiable"), never a global REVIEW with no actionable cause.

9. **One engine, one contract.** Collapse the 16 scripts into a single takeoff service with a documented interface; a fix lands once and protects every project. The proliferation of one-off scripts is itself a structural failure mode.

---

## Bottom line

The dual-measure gate is the right *philosophy* but is gating a pipeline whose two inputs are not actually two trustworthy measurements: M1 is a contract-less cloud oracle with unbounded silent-failure modes, and M2 is deterministic but parameterized by an unverified scale that both measurers share. The result is a system that is correct only when a human has already done the two hardest steps by hand (pick the floor-plan sheets, confirm the scale) — which is exactly what happened on the one HIGH run (Marriott) and didn't happen on Weddington.

The redesign that ends the whack-a-mole is not "patch Togal better." It is: **make scale and sheet-selection measured-and-self-verified local steps, anchor confidence to the documents' own printed quantities, demote Togal to an optional corroborator the system can run entirely without, and collapse the 16 scripts into one contract.** Invariant #1 (no silent defaults) and #2 (scale is measured, not input) are the two that, alone, would have caught both Weddington failures.

Relevant files: `C:\Agent Carol\scripts\togal_pipeline.py`, `C:\Agent Carol\scripts\auto_takeoff.py` (the gate, `run_gate` lines 211-289), `C:\Agent Carol\scripts\_lib\vector_takeoff.py`, `C:\Agent Carol\scripts\_lib\takeoff_scale.py` (scale solver + `crosscheck_vs_program` lines 129-153), `C:\Agent Carol\data\memory\takeoff_benchmarks.json` (the proof: 0 GOLD, 8/11 REVIEW).

---

## INDUSTRY RESEARCH

I have enough. The OST multi-scale concept is well-established (per-region scale calibration) and confirmed by the OST search result. Let me compile the findings.

---

# How professionals do construction takeoff reliably (2025-2026)

Research synthesis for the Carol takeoff redesign. The clearest takeaway across every source: pros do NOT trust a single automated number. Reliability comes from a **disciplined workflow** (scale verification + independent second measure), not from a better tool. The Weddington failures map exactly onto the known failure modes pros have built process guardrails against.

## 1. The standard professional takeoff workflow

The trusted workflow is the same across Bluebeam, Planmetry, OST/ConstructConnect, and the academic study:

1. **Get vector PDFs, not scans.** Always request drawings exported directly from CAD. Vector lets the tool snap to actual geometry; raster forces pixel-clicking that is "rarely 100% accurate" and degrades with resolution/warping. This is the #1 upstream control.
2. **Set/verify scale PER SHEET — never globally, never from the title block.** Each sheet gets its own scale; combined sets routinely mix scales.
3. **Calibrate to a known dimension** (a dimensioned wall, a gridline-to-gridline span). Click two points, type the real length.
4. **Double-calibrate (measure twice):** verify with a SECOND known dimension in another region, and ideally one horizontal + one vertical, because scanned/exported PDFs get stretched in one axis. Sanity-check against a known object (door ≈ 3 ft, parking stall ≈ 9 ft).
5. **Measure** lengths/areas/counts with disciplined naming ("Name – Location" + attributes), deduct openings, group by trade.
6. **QA / independent verification:** "one measures, one checks." A peer or senior estimator re-runs or validates before the number is trusted. No takeoff is final on one pass.
7. **Version control:** exports named by date + author; re-verify on every addendum (overlay tools flag what changed in red/blue).

## 2. The tools and what they're actually good at

| Tool | Type | Reality |
|---|---|---|
| **Bluebeam Revu** | Manual/semi-auto, vector | The industry calibration gold standard. Calibrate-to-known-dimension + per-page scale + saved scale presets. Trusted as the "ground truth measure." |
| **On-Screen Takeoff (OST)** | Desktop, commercial subs | Built for massive commercial sets offline. Has explicit **multi-scale-per-page** handling, Auto-Name, bitmap count, and **revision overlay** (red=removed/blue=added). |
| **PlanSwift / STACK / Kreo** | Digital/cloud | STACK tested within 3% of baseline after assemblies built. Kreo: ~95% automation but "opaque algorithm," heavy setup/training before trustworthy. |
| **Togal.AI** | AI auto-detect | 97% on **space/floor-plan detection**, ~70% time savings. BUT: architectural-only, "reads geometric shapes, does NOT interpret specs/notes," struggles with scanned/low-quality drawings, confuses hollow spaces/annotation lines, mis-splits doors vs windows. Requires human review/completion. |
| **Beam AI** | AI + human service | ±1% claims; **every output reviewed by a human expert** (2-3 day service model). |
| **Procore Estimating / Autodesk Assemble** | Integrated | Procore within ~4%, more manual clicks, less full CV automation. |

**Independent benchmark (200+ sheet complex project, QS triple-checked ground truth, Feb 2026):** InEight 1.8% error, STACK ~3%, Procore ~4%. Vendor accuracy claims (97-98%) are for *clean architectural floor plans only* and collapse on complex/scanned/multi-discipline sets.

## 3. Why PDF takeoff goes wrong (this is the Weddington failure list)

The literature names the exact failures Carol hit:

- **Combined/permit sets mix scales** — multiple views at different scales on one page; a single global scale (0.1875 vs 0.125) is wrong everywhere. → pro fix: per-region scale.
- **Rasterized/scanned content** distorts measurement; raster warping reduces accuracy across the page.
- **AI auto-detect over-counts and mis-classifies** — confuses hollow spaces, annotation lines, shafts (Togal's 56,267 "rooms" / "Hotel Room" / "Shafts" is the textbook pattern: it reads shapes, not meaning).
- **AI omits elements** in legends, upper/lower projections, and produces **empty walls** when the geometry confuses it (the empty `walls[]` arrays).
- **No verification** = the single biggest process failure cited. "Scale not calibrated/verified twice" and "no QA before export" top every mistake list.

## 4. The discipline that makes it reliable: measure twice, independently

This is the core principle — and it's already Carol's "dual-measure GOLD" law, validated by industry:

- **Scale:** calibrate against TWO known dimensions (different regions, both axes). If they disagree, the scale is wrong — stop, don't guess.
- **Quantity:** AI/auto measure + an independent second measure must reconcile. The academic study, Planmetry, Nomitech, and Datagrid all mandate "one measures, one checks" + senior sign-off. A tool is trusted only when it lands within 2-3% on counts and 4-6% on measurements *against a known-actual job*.
- **AI is a first pass, never the answer.** Universal consensus: "human oversight remains essential… human-AI collaboration, not full automation."

## 5. Concrete redesign implications for Carol

The research says the fix is NOT a better Togal call — it's adopting the pro guardrails so any tool's failure is *caught*, not *shipped*:

1. **Calibration-first, per-sheet, two-known-dimension gate.** Before any measurement, auto-detect dimensioned strings/gridlines and calibrate to a known length; require a second known dimension to agree (kills the 0.1875-vs-0.125 ambiguity that produced 694 vs 25,811 SF). If the two don't reconcile, the sheet is flagged UNSCALED, not measured. This is the missing piece in vector_takeoff.py.
2. **Reject/flag raster + combined sets up front.** Detect when a PDF page is scanned/raster or contains mixed scales and route it to a different (per-region) path or to manual — don't feed it a single global scale.
3. **Treat Togal as ONE noisy first-pass, gated by sanity rules** (count ceilings per SF, reject "Shaft/Hotel Room" garbage classes, require non-empty walls[] before trusting). Togal-alone is never GOLD — matches the "AI reads shapes not specs" finding.
4. **Independent second measure is mandatory, and the LLM per-unit-type dimensioned-plan read (the SILVER fallback that actually worked at Weddington) is a legitimate professional method** — it mirrors manual takeoff from dimension strings, which pros explicitly do. Promote it to a first-class M2, not just a fallback.
5. **Verify against a known-actual job** (a Cowork workbook with real measured SF) as the acceptance test for the whole pipeline — exactly how pros validate a new takeoff tool.

## Sources

- [Planmetry — PDF Quantity Takeoff Complete 2025 Workflow](https://www.planmetry.com/blog/pdf-quantity-takeoff-workflow) (double-calibrate, per-sheet scale, one-measures-one-checks, 10 failure modes)
- [Bluebeam — Construction Takeoffs Guide 2026](https://www.bluebeam.com/resources/construction-takeoffs-guide-2026/) and [Calibrate display scale](https://support.bluebeam.com/revu/how-to/calibrate-display-scale.html)
- [Bluebeam Built blog — Scaling Markups for Accuracy](https://blog.bluebeam.com/achieve-flawless-accuracy-scaling-markups-bluebeam-revu/)
- [On-Screen Takeoff — Handling More Than One Scale on the Same Plan](https://help.constructconnect.com/) (per-region multi-scale) and [Setting Scale & Takeoff guide](https://help.constructconnect.com/06-setting-scale-and-drawing-takeoff-including-takeoff-boost-73/on-screen-takeoff-06-00-scale-and-takeoff-overview-1032)
- [Robotics & Automation News — 6 AI estimating tools tested for complex-project accuracy (Feb 2026)](https://roboticsandautomationnews.com/2026/02/19/6-ai-construction-estimating-software-tested-on-complex-project-accuracy/98967/) (InEight 1.8%, STACK 3%, Procore 4%; AI fails on scanned/civil/hand-sketch)
- [Peer-reviewed study: Togal.AI vs On-Screen Takeoff (KU/UNCC/ASU, Mar 2025)](https://www.togal.ai/case-study/peer-reviewed-study-togal-ai-vs-on-screen-takeoff) (~71% time savings, ≤5% accuracy, but AI omits legends/projections, confuses hollow spaces & annotation lines, mis-splits doors/windows; human oversight essential)
- [iBeam — Togal.AI vs Beam AI comparison](https://www.ibeam.ai/blog/togal-vs-beamai-comparison) (Togal architectural-only, reads shapes not specs/notes)
- [TakeoffSoftware.com — Construction Drawing File Types for Estimators](https://www.takeoffsoftware.com/construction-drawing-file-types-what-do-they-mean-for-estimators-when-doing-a-takeoff/) (raster vs vector accuracy)
- [Nomitech — Quantity Takeoff Process Guide](https://www.nomitech.com/cost-estimating/quantity-takeoff-construction-guide) and [Datagrid — Automating Takeoff QA](https://datagrid.com/blog/ai-agents-automate-quantity-takeoff-qa) (peer review + senior sign-off discipline)

**Bottom line for the redesign:** the way to make takeoff "never have issues ever again" is not a more robust Togal integration — it's to stop trusting any single auto-number and codify the professional guardrails: vector-only input, per-sheet calibration to two known dimensions (reconcile or flag), reject raster/mixed-scale pages, treat AI as a sanity-gated first pass, and require an independent second measure (the LLM dimensioned-plan read) to agree before any number is GOLD. Whack-a-mole ends when the architecture assumes the tool WILL fail and the verification layer catches it.

---

I have enough to synthesize. The findings strongly converge and directly validate the failure pattern observed on Weddington. Here are the cited findings and the trust model real teams use.

---

# AI Takeoff Reliability — Research Findings (cited) + the trust model real teams use

## Bottom line
The user's experience is not a Carol bug or a one-off — it is the **documented, universal behavior of AI takeoff**. Every credible source says the same thing: AI auto-detect is a *speed tool that produces a draft*, never a number you ship. The Weddington failure (noisy room over-count, empty walls, scale ambiguity on a combined permit set) is the textbook failure mode of this entire product category. No vendor — Togal, Kreo, Beam, Bluebeam, STACK, Procore — claims hands-off accuracy. The fix is not "patch the next Togal failure"; it's adopting the industry trust model below.

## 1. How AI takeoff actually performs (measured, not vendor-claimed)

| Tool | Reported accuracy | Notes |
|---|---|---|
| Togal.AI | "97% detecting spaces" (vendor); peer study within **5%** *after human adjustment*; **85% residential / 60% retail podium** in the field | Excels on clean 2D architectural PDFs; degrades hard on civil/MEP/scanned |
| InEight Estimate | 1.8% total error | top of an independent 6-tool test |
| STACK | within 3% | |
| Procore Estimating | within 4% | needed more manual wall-run tracing |
| Kreo | "95% of the way" before manual tagging | "auto-measure can be messy"; still needs human QA |
| Beam AI | low miss rate | achieves it via a **human QA team checking every takeoff** before delivery |

Key independent data points:
- On **standard residential** work, AI lands **2–4%** of manual — acceptable for bids. On **complex commercial/industrial**, "the accuracy gap widens and individual elements can be significantly off in opposite directions." (eano)
- The Togal peer-reviewed study itself concludes: **"relying solely on the AI-automated results is not advisable… professional expertise and human oversight remain essential."** Discrepancies came from omitted projections, tracing errors from annotations/textures, door-vs-window confusion, and items only in legends/notes. (togal.ai case study)

## 2. The exact failure modes Carol hit are the known ones
- **Empty walls / missed walls:** "wall detection breaks down on complex intersections"; Reddit estimators report "the AI missed half my walls." Togal *can* auto-measure walls/base/trim — but only on clean drawings; on dense/combined sheets it returns garbage or nothing. (struvia/bidi, togal)
- **Room over-count / mis-classification:** "misidentifies spaces when room labels are missing or ambiguous" — produces phantom/duplicated spaces exactly like Weddington's "Shafts"/"Hotel Room" 2.5x over-count. (eano, struvia)
- **Combined/scanned permit sets:** "low-res scanned plans produce enough errors that manual correction takes longer than starting from scratch." A 206-page combined permit PDF is the worst-case input. (struvia, eano)
- **Scale ambiguity:** Universal hard rule across sources — **"Always verify the scale calibration on at least one known dimension."** AI does not reliably infer scale on non-standard/combined sheets; this must be human-anchored, never assumed. This is the root of Carol's 694 vs 25,811 SF swing. (eano, Bluebeam)
- **Scope blindness:** "AI takeoff reads what's drawn. It doesn't know what should be drawn" — walls-to-deck, alternates, finishes in schedules/legends are invisible to it. (eano) — matches Carol's existing "read the GC scope letter" law.

## 3. Accepted accuracy tolerance for a competitive sub bid
- **Bid-stage takeoff: ±5–8% is acceptable** — you're building a competitive number, not a contract.
- **Contract-stage / GMP / lump-sum: ±2–3%.**
- For a specialty sub like CCF painting, **a 5% measurement error can erase the entire profit margin** on a job — so tighter is better and the *direction* of error matters (under-measure = winner's curse). (bidicontracting/Bluebeam, inno-csol)

This means Carol's existing "<=10% to call it GOLD" dual-measure gate is actually *looser* than the industry bid tolerance. Worth tightening the GOLD band to ~5% and treating 5–8% as SILVER, >8% as needs-human.

## 4. Is "AI auto-detect then human-confirm" the norm? — Yes, universally.
Nobody ships raw AI output. Two dominant trust models in the field:

**Model A — Human-in-the-loop (Togal, Kreo, STACK, Procore):** AI generates the draft; estimator reviews/corrects in the tool before the number leaves. "Let the software handle the standard elements; reserve manual effort for what genuinely needs it." Verification is **mandatory, not optional.**

**Model B — Human QA gate (Beam AI):** AI runs, then a team of estimators checks **every** takeoff before it reaches the customer. They sell the *verified* number, not the raw one.

Both models share one principle: **the AI never produces the shippable number alone.** The accepted operating posture is "trust but verify, with the scale and a known dimension confirmed by a human every time."

## 5. The trust model to adopt for Carol (synthesized from the above)
This is the redesign direction the research supports — stop chasing Togal's per-project failures and instead build the verification scaffold every real team uses:

1. **AI is a draft engine, never the answer.** Treat Togal output as a *candidate* requiring confirmation — architecturally, never let a raw Togal number flow into an estimate.
2. **Scale is human-anchored, always.** Before any measurement, lock scale against one known dimension from the drawing (a dimensioned wall, a door = 3'-0", a known room dim, the title-block scale). Refuse to measure on an assumed/inferred scale. This alone would have caught Weddington.
3. **Triangulate, don't single-source.** The dual-measure (Togal M1 + vector M2) instinct is correct and matches how independent testers build "ground truth" (senior QS triple-check). Add a **third cheap anchor**: LLM reading dimensioned plans / area & finish schedules (the SILVER method that actually worked on Weddington). Agreement of 2-of-3 within tolerance = trust.
4. **Tolerance gating tied to bid stage.** GOLD <=5% (bid-ready), SILVER 5–8% (usable, flag), >8% = HARD STOP → human/LLM-anchored measured takeoff. Match the ±5–8% bid-stage industry band.
5. **Right tool per input type.** Route by drawing quality: clean single-discipline architectural PDF → Togal is great; combined/scanned/MEP-heavy permit set → skip Togal auto-detect entirely, go straight to LLM-reads-dimensioned-plans + schedules. Don't fight Togal on inputs the whole industry says it fails on.
6. **Schedules/legends are ground truth over detection.** When a finish/area schedule or COMcheck gives the number, that beats any pixel detection (already a Carol law — reinforce it as the tiebreaker).
7. **Scope letter + specs gate the takeoff**, since AI measures only what's drawn (already a Carol law).

The strategic reframe: **the goal isn't a Togal that never fails — that product doesn't exist for anyone. The goal is a verification process that catches Togal's failures before they reach the bid**, the same posture Beam/Kreo/Togal's own peer study prescribe.

## Sources
- [Togal.AI peer-reviewed study vs On-Screen Takeoff](https://www.togal.ai/case-study/peer-reviewed-study-togal-ai-vs-on-screen-takeoff)
- [Togal AI Review for GC Estimators (struvia/bidicontracting)](https://struvia.co/blog/togal-ai-review-2026)
- [AI Construction Takeoff: What it gets right and where it falls short (eano)](https://www.eano.com/blogs/ai-construction-takeoff-software-what-it-gets-right-and-where-it-still-falls-short)
- [6 AI estimating tools tested for complex-project accuracy (Robotics & Automation News)](https://roboticsandautomationnews.com/2026/02/19/6-ai-construction-estimating-software-tested-on-complex-project-accuracy/98967/)
- [Beam AI vs Kreo (ibeam.ai)](https://www.ibeam.ai/blog/beamai-vs-kreo)
- [Your Takeoff Is Wrong — Why That Matters (Bluebeam BUILT)](https://blog.bluebeam.com/your-takeoff-is-wrong-why-that-matters/)
- [Material Quantity Takeoff Accuracy (Innovative Construction Solutions)](https://inno-csol.com/material-quantity-takeoff-accuracy/)
- [How to Classify Walls in Togal.AI](https://www.togal.ai/blog/how-to-classify-walls-in-togal-ai)
- [Togal Painting & Wallpaper takeoff](https://www.togal.ai/trades/painting-wallpaper)

---

I have everything needed. Here is the research synthesis.

---

# Robust PDF/Vector Takeoff: Engineering Research

The root failure isn't Togal-specific — it's that the current stack has **no deterministic, first-principles measurement layer of its own**. Togal is a black-box AI that fails differently every project. The fix is to build a local engine where every number traces to a known scale and known geometry, with Togal demoted to one cross-check voice among several. Below are the concrete techniques and libraries per sub-problem.

## 1. Scale detection — the #1 cause of the Weddington failure (694 vs 25,811 SF)

Solve scale with a **cascade, highest-confidence-first, per page**, never one global factor:

**Tier A — Embedded PDF scale (deterministic, no guessing).** When a plan was scaled in Bluebeam/Acrobat, the real-world ratio is stored in the PDF itself and is exact:
- `/UserUnit` in the page dict (PDF 1.6+): points × UserUnit × 1/72 = inches. Optional, often absent. Read via `page.xref` / `doc.xref_get_key`.
- `/VP` (Viewport array) on the page → each viewport carries a `/Measure` dictionary (`/Subtype /RL`). The `/X` number-format array + `/R` ratio string (e.g. `"1 in = 10 ft"`) and `GPTS`/`LPTS`/`Bounds` give an exact points→feet conversion, scoped to a sub-region of the page. This is the gold source when present. Parse the raw PDF objects (PyMuPDF `xref` access, or `pikepdf` for clean dict traversal).
- **Per-page/per-viewport law:** a combined permit set has different scales per sheet and even multiple `/VP` regions per sheet (plan at 1/8", details at 1"). Store scale keyed to `(page_index, viewport_bounds)` — never one document-wide number. ([Apryse coordinates](https://apryse.com/blog/pdf-coordinates-and-pdf-processing), [Apryse measurement](https://apryse.com/blog/pdf-measurement-implementation-guide))

**Tier B — Title-block scale string (OCR/text).** Pull text from the title-block region (bottom-right strip) with `page.get_text("words")` (vector) or Tesseract (raster). Regex for `SCALE: 1/4" = 1'-0"`, `1:100`, `3/32"=1'`, `NTS`. Map the architectural ratio to points/foot. `NTS` ("not to scale") must HARD-disqualify that sheet from vector measure → route to dimension-reading instead.

**Tier C — Graphic scale bar.** Detect the labeled scale bar (a divided ruler graphic with `0  16'  32'` ticks). Find the horizontal segment group, OCR the end label, divide pixel/point length by labeled feet. Robust because it survives PDF rescaling (the bar scales with the drawing).

**Tier D — Calibrate to a labeled dimension (self-checking fallback).** Find a printed dimension string (`24'-6"`) and its associated dimension line (the geometry between two extension-line ticks). Real length ÷ measured point length = scale. Do this for *several* dimensions across the sheet and require they agree; disagreement means you grabbed the wrong line. Formula: `scale = real_length / measured_points`. ([Qoppa calibration](https://www.qoppa.com/files/pdfstudio/guide/calibrate-measurement-annotation.htm))

**Tier E — Sheet-size sanity (MediaBox).** Compare MediaBox (72 pt/in) to standard sheets — Arch D 24×36, Arch E 30×42. Gives a coarse scale and catches gross errors (the 0.1875-vs-0.125 ambiguity). Use only to validate Tiers A–D, never as primary. ([UserUnit/itext](https://kb.itextpdf.com/itext/how-to-get-the-userunit-from-a-pdf-file), [MediaBox](https://www.pdf2go.com/dictionary/mediabox))

**Gate:** require ≥2 independent tiers to agree within a tolerance, else mark scale UNTRUSTED and fall to the dimension-reading path. The Weddington disaster (assumed 0.1875 vs 0.125) is exactly what a 2-source agreement gate prevents.

## 2. Vector vs raster/scanned classification (per page)

No single test is failsafe ([PyMuPDF #1653](https://github.com/pymupdf/PyMuPDF/discussions/1653)); combine, per page:
- **Image coverage:** if `get_images()` yields image(s) whose combined bbox covers ≥95% of `page.rect` → raster/scanned. `abs(img_bbox & page.rect)/abs(page.rect) >= 0.95`.
- **Vector density:** `len(page.get_drawings())` — hundreds/thousands of paths → true vector. Near-zero → raster.
- **Real text:** `len(page.get_text("words"))` of selectable, non-`GlyphlessFont` text. `GlyphlessFont` in `get_fonts()` ⇒ Tesseract-OCR'd scan masquerading as digital.
- **Decision:** VECTOR → geometry engine (Section 3). RASTER → OpenCV/CV pipeline + Tesseract OCR; do not trust `get_drawings()` there.

## 3. Wall geometry from vector strokes (the measurement engine)

**Primitive extraction (PyMuPDF `get_drawings()`):** returns a list of path dicts. Keys: `items`, `rect` (bbox), `width` (stroke width — critical), `color`, `fill`, `closePath`. Each `items` entry is a typed tuple: `('l', Point, Point)` line, `('re', Rect)` rectangle, `('qu', Quad)`, `('c', p1, cp1, cp2, p2)` Bézier. Iterate to recover every segment's endpoints in PDF points. ([PyMuPDF drawings recipe](https://pymupdf.readthedocs.io/en/latest/recipes-drawing-and-graphics.html), [Artifex blog](https://artifex.com/blog/extracting-and-creating-vector-graphics-in-a-pdf-using-python-pymupdf))

**Walls = parallel-line-pair detection:**
1. Collect all `'l'` segments; normalize each by angle (snap near-axis to 0°/90° via dominant-angle histogram — sheets are often rotated a fraction of a degree).
2. Group by orientation; within a group find pairs of collinear-offset segments whose perpendicular gap = a plausible wall thickness (3.5", 4.875", 6", 8" at the page scale). Two close parallel lines + matching gap ⇒ a wall; the **centerline** is their midline. ([ResearchGate DXF walls](https://www.researchgate.net/post/How-to-hierarchically-extract-wall-information-geometry-from-floor-plans-in-DWG-or-DXF-files), [McNeel centerline](https://discourse.mcneel.com/t/automated-centerline-extraction-from-plan-walls/121783))
3. Merge colinear centerlines, snap endpoints to intersections → wall network graph (nodes/edges). Wall length × scale × ceiling height (Section 5) = paintable wall SF.
4. **Rooms/areas:** the closed regions of the wall graph are rooms; polygon area × scale² = floor/ceiling SF. Cross-check against `cluster_drawings()` (joins nearby path bboxes into room-sized rectangles — useful even though the API page omits it, it exists on `Page`).
5. **Bézier/curved walls:** flatten `'c'` items to polylines before pairing.

**Stroke-width filter** kills the noise that gave Togal 56k phantom rooms: discard hairline annotation/hatch/dimension lines by `width`, layer color, and dash pattern before pairing.

## 4. Combined multi-sheet set with mixed scales

- Split the 206-page PDF into pages; classify each (Section 2) and **tag sheet type** by title-block discipline code (A = architectural floor plans, S/M/E/P = skip for paint, schedules, COMcheck/energy). Only measure A-series plan sheets; reject egress/diagram sheets (already a v3 law).
- Run scale cascade **independently per page**; persist `scale_by_page`.
- For 85-unit apartments: detect the **unit-type plans** (Type A/B/C 1-page enlarged plans) and the **unit count schedule**, measure each unit type once at its (large) scale, multiply by counts. This is far more reliable than measuring 85 tiny stacked units on a key plan — and matches the SILVER fallback you already used, but now scale-anchored and deterministic.

## 5. Heights (HARD-required by v3)

Wall SF needs height. Pull from: room finish schedule / wall types legend (OCR the schedule table), `COMcheck`/energy sheets (ceiling heights), section sheets, and RCP ceiling notes. Never silent-9'. If no height found per area, flag that area's SF as height-UNKNOWN and block GOLD.

## 6. OCR / dimension cross-check (independent ground-truth voice)

This is the validator that makes the whole thing trustworthy — measured area must agree with the **printed** numbers:
- **Printed room-area tags:** OCR room labels like `LIVING 168 SF`, `BR-1 132 SF`. Vector text via `get_text("dict")` (with bbox to associate label↔room polygon); raster via Tesseract/EasyOCR/PaddleOCR. ([Nomic OCR](https://www.nomic.ai/glossary/construction-drawing-ocr), [ScienceDirect floor-plan OCR](https://www.sciencedirect.com/science/article/abs/pii/S0926580523004168))
- **Dimension strings:** `24'-6"` regex → sum perimeter dims to recompute room SF independently.
- **Finish/area schedule tables:** total GSF/unit SF as a document-level check.
- **Reconciliation gate:** measured polygon SF vs printed SF tag vs schedule SF — require ≤10% spread for GOLD, exactly mirroring the existing M1/M2 law but with *printed numbers* as a third, scale-independent voice. OCR errors are caught the same way ([dimension QC](https://www.sciencedirect.com/science/article/pii/S016636152100049X)).

## 7. DXF/DWG path (highest-fidelity input when available)

If the GC provides CAD (or you convert): walls become exact `LINE`/`LWPOLYLINE` entities on named layers (`A-WALL`), no detection guesswork.
- **`ezdxf`** reads DXF natively; filter by layer (`A-WALL`, `A-AREA`), read entity coords in real units (DXF carries true units → no scale ambiguity at all). ([ezdxf](https://pypi.org/project/ezdxf/))
- **PDF→DXF conversion:** `pyPDFtoDXF` chains `pdf2svg` → Inkscape → DXF (good for geometry, weak on text), or `PdfExtract` (QGIS plugin built on **PyMuPDF + ezdxf** directly). Prefer the PyMuPDF+ezdxf direct route so you stay in one stack. ([pyPDFtoDXF](https://github.com/mjecke/pyPDFtoDXF), [PdfExtract](https://plugins.qgis.org/plugins/PdfExtract/))

## Recommended architecture (replaces Togal as single point of failure)

```
ingest → split pages → classify vector/raster (PyMuPDF) → tag sheet discipline
     → scale cascade per page/viewport [embedded /Measure → title-block → scale-bar
        → dimension-calibrate → MediaBox sanity]  (≥2 agree or UNTRUSTED)
     → VECTOR: get_drawings() → stroke-width filter → parallel-pair walls + room polys
       RASTER: OpenCV (findContours + Douglas-Peucker + parallel-pair) + Tesseract
     → heights from schedules/sections (HARD)
     → OCR area tags + dimension strings + schedule totals
     → reconcile: measured vs printed vs (Togal M1 as one more voice)  ≤10% = GOLD
     → trust ledger
```

**Libraries:** PyMuPDF/fitz (primitives, text, images, drawings, xref/Measure), `pikepdf` (clean PDF-object/Measure-dict traversal), OpenCV (raster lines/contours, Hough, RANSAC), Tesseract via `pytesseract` + EasyOCR/PaddleOCR (dimension/area OCR), `ezdxf` (DXF when available), `shapely` (polygon area, centerline merging, room closure), `numpy` (angle histograms, line algebra). ([PyMuPDF](https://github.com/pymupdf/pymupdf))

**The core design principle:** Togal failed in a new way each project because it's an opaque AI with no fallback that *you* control. The redesign makes the local engine deterministic and self-checking — every SF traces to (a) a scale proven by ≥2 independent sources and (b) geometry cross-checked against the drawing's own printed numbers. Togal becomes one cross-check voice, so a Togal failure degrades to a warning, never a project-stopper.

Relevant existing files to refactor against: `C:/Agent Carol/scripts/_lib/vector_takeoff.py` (extend with the scale cascade + parallel-pair detector), `C:/Agent Carol/scripts/togal_pipeline.py` (demote to one voice), `C:/Agent Carol/data/memory/takeoff_benchmarks.json` (trust ledger gains the printed-number reconciliation column).

Sources:
- [PyMuPDF drawings/graphics recipe](https://pymupdf.readthedocs.io/en/latest/recipes-drawing-and-graphics.html)
- [Artifex: extracting vector graphics with PyMuPDF](https://artifex.com/blog/extracting-and-creating-vector-graphics-in-a-pdf-using-python-pymupdf)
- [PyMuPDF #1653: identify scanned PDF](https://github.com/pymupdf/PyMuPDF/discussions/1653)
- [Apryse: PDF coordinate systems](https://apryse.com/blog/pdf-coordinates-and-pdf-processing)
- [Apryse: PDF measurement implementation guide](https://apryse.com/blog/pdf-measurement-implementation-guide)
- [iText: get UserUnit](https://kb.itextpdf.com/itext/how-to-get-the-userunit-from-a-pdf-file)
- [PDF2Go: MediaBox](https://www.pdf2go.com/dictionary/mediabox)
- [Qoppa: calibrate measurement annotation](https://www.qoppa.com/files/pdfstudio/guide/calibrate-measurement-annotation.htm)
- [ResearchGate: extract wall geometry from DWG/DXF](https://www.researchgate.net/post/How-to-hierarchically-extract-wall-information-geometry-from-floor-plans-in-DWG-or-DXF-files)
- [McNeel: automated centerline extraction from plan walls](https://discourse.mcneel.com/t/automated-centerline-extraction-from-plan-walls/121783)
- [Nomic: construction drawing OCR](https://www.nomic.ai/glossary/construction-drawing-ocr)
- [ScienceDirect: text detection/recognition on architectural floor plans](https://www.sciencedirect.com/science/article/abs/pii/S0926580523004168)
- [ScienceDirect: dimension extraction for QC](https://www.sciencedirect.com/science/article/pii/S016636152100049X)
- [ezdxf (PyPI)](https://pypi.org/project/ezdxf/)
- [pyPDFtoDXF](https://github.com/mjecke/pyPDFtoDXF)
- [PdfExtract QGIS plugin (PyMuPDF+ezdxf)](https://plugins.qgis.org/plugins/PdfExtract/)
- [PyMuPDF (GitHub)](https://github.com/pymupdf/pymupdf)

---

