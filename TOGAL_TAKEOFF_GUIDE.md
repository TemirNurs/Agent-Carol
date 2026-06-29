# Togal AI Takeoff — Operating Guide

> **For Claude Cowork (or any Claude session) operating Togal AI on behalf of Carolina Commercial Finishes.**
>
> This is a standalone runbook. You do not need any other context — read this guide, run the commands. Everything you need is either in this file or accessible from the file paths it references.

---

## 1) What Togal is and why we use it

Togal AI (`app.togal.ai`) is a construction takeoff tool that:
1. Vectorizes architectural drawings (extracts walls, rooms, openings)
2. Calculates square footage of rooms, linear footage of walls, count of doors/windows
3. Returns measurements per architectural sheet

We use Togal to size painting/wallcovering scope before generating an estimate. **You can run a complete takeoff from the command line — no manual UI clicks required.** A Python pipeline already wraps every Togal API call.

---

## 2) Credentials & config

Credentials live in `data/config/togal_auth.json`. Read it programmatically — do **not** hardcode values:

```python
import json
from pathlib import Path
auth = json.loads(Path("data/config/togal_auth.json").read_text(encoding="utf-8"))
# Keys: email, password, api_key, organization_id, session_token
```

Confirmed values:
- **email:** `cs@carolinacommercialfinishes.com`
- **organization_id:** `<from data/config/togal_auth.json>`
- **base_url:** `https://app.togal.ai/api`
- **session_token:** stored, auto-refreshed when 401 occurs

If any Togal API call returns 401, the pipeline auto-re-authenticates by POSTing email+password to `/v1/session` and persisting a fresh `session_token`. You don't need to do this manually unless the password itself has changed.

---

## 3) The pipeline

A single Python script handles every step: `scripts/togal_pipeline.py`. Use it. Do not write a parallel implementation — the pipeline already handles auth refresh, multi-page PDF upload + processing wait, sheet auto-detection, scale setting, view creation (incl. 409 conflicts), vectorization + polling, measurement extraction, and post-processing.

### 3.1 — Run a complete takeoff (most common)

```bash
python scripts/togal_pipeline.py --project <slug> --scale "1/8"
```

Behavior:
1. Authenticates with Togal
2. Looks up the project's `togal_set_id` in `data/projects/<slug>/project.json`
3. If no Set exists yet: searches `data/projects/<slug>/drawings/` (or `bid_docs/`) for plan PDFs and uploads the largest plan-like one. Skips ESA reports, spec books, addenda by name.
4. Polls until pages are processed (up to 5 minutes)
5. Filters to painting-relevant sheets (3-tier fallback — see §6)
6. Sets scale on each page (default `1/8" = 1'-0"`)
7. Creates a "Paint Takeoff" view per page, runs vectorization
8. Polls until vectorization completes
9. Extracts measurements
10. Writes results to `data/projects/<slug>/togal_takeoff.json`

Total runtime: 3–8 minutes depending on PDF size.

### 3.2 — Status check (read-only)

```bash
python scripts/togal_pipeline.py --project <slug> --status
```

Returns JSON: `{set_id, takeoff_complete, pages_measured, timestamp}`.

### 3.3 — Re-extract from existing views (skip vectorization)

If pages are already vectorized but you want fresh measurements (e.g. after manually adding classifications in Togal's web UI):

```bash
python scripts/togal_pipeline.py --project <slug> --extract
```

### 3.4 — Manual upload (when auto-detect picks the wrong PDF)

```bash
python scripts/togal_pipeline.py --upload <slug> path/to/specific_plans.pdf
```

Creates a new Set under the existing Togal Project and uploads the PDF.

### 3.5 — Restrict to specific sheets

```bash
python scripts/togal_pipeline.py --project <slug> --sheets "A201,A301,A912"
```

Useful when auto-detection picks too many or wrong pages.

### 3.6 — Dry run

```bash
python scripts/togal_pipeline.py --project <slug> --dry-run
```

Shows what would happen without making API calls or modifying state.

---

## 4) Project folder layout

Each project lives at `data/projects/<slug>/`. Standard layout:

```
data/projects/sally-beauty-3622-cary-nc/
├── project.json          ← name, slug, togal_set_id, togal_project_id
├── drawings/             ← raw bid plan PDFs (preferred location)
├── bid_docs/             ← alternate location (some projects use this)
├── togal_takeoff.json    ← OUTPUT: final measurements
└── view_ids.json         ← page_id → view_id map
```

The pipeline's `find_project_dir()` handles slug variants (dashes vs underscores) so projects ingested by different scripts still resolve to the same folder. Don't worry about case or punctuation — pass any slug variant and it'll find the right folder.

### Bootstrapping a new project

```python
from pathlib import Path
import json
slug = "new-project-slug"
proj_dir = Path("data/projects") / slug
proj_dir.mkdir(parents=True, exist_ok=True)
(proj_dir / "drawings").mkdir(exist_ok=True)
(proj_dir / "project.json").write_text(json.dumps({
    "name": "New Project Name",
    "slug": slug,
}, indent=2))
# Drop the plan PDF into drawings/, then run the pipeline.
```

---

## 5) Reading the takeoff results

After the pipeline finishes, `togal_takeoff.json` contains:

```json
{
  "project": "sally-beauty-3622-cary-nc",
  "timestamp": "2026-05-07 14:30",
  "method": "togal_ai_pipeline",
  "scale": "1/8",
  "pages_measured": 4,
  "source": "ai_detected_geojson",
  "takeoff": {
    "floor_sf": 8421.5,
    "perimeter_lf": 1287.3,
    "room_count": 24,
    "primary_page": "A201_Floor_Plan",
    "best_pages_used": 1,
    "total_pages": 4,
    "page_groups": [...],
    "rooms": [...]
  },
  "raw_measurements": {...},
  "per_page_stats": {...}
}
```

### Fields you'll usually quote

- `takeoff.floor_sf` → room SF total (input to estimator)
- `takeoff.perimeter_lf` → wall painting LF
- `takeoff.room_count` → cross-check against your manual count

### `source` field — which is authoritative?

- `"classifications"` → user-drawn shapes in Togal's web UI. **Authoritative — quote these directly.**
- `"ai_detected_geojson"` → AI-detected. Good but should be sanity-checked. The pipeline's post-processing already filters non-painting classifications and removes building outlines, but spot-check totals against project size.

If the AI-detected number looks wrong (e.g. 50K SF on a small retail remodel), open the project in Togal web UI, draw the actual painting areas as classifications, then run `--extract` again — the pipeline will prefer classifications over AI detection.

---

## 6) Sheet detection rules (informational — pipeline handles automatically)

The pipeline filters drawing pages to painting-relevant sheets using a 3-tier fallback:

**Tier 1 — Standard architectural prefixes** (normal CD sets):
- A201/202/203/204 — Floor Plans
- A301/302/303/304 — Reflected Ceiling Plans
- A901/902/903/904 — Finish Plans
- A912/913 — Interior Elevations

**Tier 2 — Keyword match** (small projects / permit sets with non-standard names):
"floor plan", "finish plan", "finish schedule", "rcp", "interior elev", "paint", "color schedule", "wall finish", "enlarged plan", "demolition", "partition"

**Tier 3 — All pages** (small projects where everything is relevant)

To override auto-detection, pass `--sheets "<comma-list>"`.

---

## 7) Common scenarios

### Scenario A — Project exists in `data/projects/<slug>/drawings/`, never been to Togal

```bash
python scripts/togal_pipeline.py --project <slug> --scale "1/8"
```

The pipeline will auto-upload, wait for processing, vectorize, and extract. Just run it once — total ~5 minutes.

### Scenario B — User just dropped a new PDF, want to upload + verify before takeoff

```bash
python scripts/togal_pipeline.py --upload <slug> path/to/file.pdf
# Wait 2-3 minutes for Togal to split the PDF into pages
python scripts/togal_pipeline.py --project <slug> --status
```

### Scenario C — Pipeline finished but measurements look wrong

Try re-extract first:

```bash
python scripts/togal_pipeline.py --project <slug> --extract
```

If still wrong, open the project in Togal web UI and **draw classifications** for the painting areas only. Then re-run `--extract`.

### Scenario D — 401 errors from Togal

Session expired. The pipeline auto-refreshes via email+password. If that fails too, the password may have changed:

```bash
python -c "
import sys, json
sys.path.insert(0, 'scripts')
from togal_pipeline import authenticate
auth = json.load(open('data/config/togal_auth.json'))
token = authenticate(auth)
print('OK' if token else 'AUTH FAILED — check email/password in togal_auth.json')
"
```

---

## 8) Hard rules (do NOT violate)

1. **Never hardcode credentials in scripts you write.** Always read `data/config/togal_auth.json`.
2. **Never share the API token, password, or `session_token` in chat output.** If you need to debug, mask middle characters.
3. **Don't bypass the pipeline.** If you need a feature it doesn't have, extend `togal_pipeline.py` — don't write a parallel implementation.
4. **One Togal Set per project.** The pipeline writes `togal_set_id` to `project.json` so subsequent runs don't create duplicates. Don't manually delete or rewrite that field unless the Set is genuinely stuck (empty after >10 minutes of waiting, vectorization fails repeatedly).
5. **Wait for processing.** Multi-page PDFs take 2–5 minutes. The pipeline polls every 15 seconds and gives up after 5 minutes per phase. Don't kill it early.
6. **Output goes to `data/projects/<slug>/togal_takeoff.json` only.** Downstream estimating scripts read from that path.

---

## 9) Script reference

| Script | Use it for |
|---|---|
| `scripts/togal_pipeline.py` | **95% of the time** — the orchestrator |
| `scripts/togal_client.py` | Lower-level helpers (auth, projects, sets) — only if extending the pipeline |
| `scripts/togal_upload.py` | Standalone upload (the pipeline calls this internally) |
| `scripts/togal_check_status.py` | Quick status check |
| `scripts/togal_find_pages.py` | List pages in a Set |
| `scripts/togal_run_takeoff.py` | Older standalone runner — prefer `togal_pipeline.py` |
| `scripts/togal_takeoff_proper.py` | Reference implementation of the px-to-SF math |
| `scripts/togal_eusa_accept.py` | Click through Togal's EULA on first login |
| `scripts/togal_intercept.py` | Debug tool for inspecting Togal API traffic |

---

## 10) Hand-off after takeoff

Once `togal_takeoff.json` exists, the project is ready for estimating:

```bash
python scripts/build_estimate.py --project <slug>
# writes data/projects/<slug>/estimate.json
```

Then:

```bash
python scripts/export_estimate_xlsx.py --project <slug>
# creates the proposal Excel file
```

Full chain end-to-end:

```bash
python scripts/togal_pipeline.py --project <slug> --scale "1/8" \
  && python scripts/build_estimate.py --project <slug> \
  && python scripts/export_estimate_xlsx.py --project <slug>
```

---

## 11) When to ask the user before acting

- Picking the **scale** when the drawings don't say (1/8" vs 1/4" changes results 4×)
- Choosing **which sheets to take off** when auto-detection returns 0 or 50+ pages
- Whether to **delete and re-create** an existing Togal Set that's stuck or has the wrong scale
- Whether to use **AI-detected GeoJSON** vs **user-drawn classifications** when both exist (default: prefer classifications)

For everything else, run the pipeline and report results.

---

## Reference: Togal API spec

The full OpenAPI spec is at `data/config/togal-openapi-spec.json`. You shouldn't need it — the pipeline wraps every endpoint you'd call — but it's there for extending the pipeline.

---

_End of runbook._
