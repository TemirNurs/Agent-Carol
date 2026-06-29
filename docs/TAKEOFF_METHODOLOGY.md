# CCF Takeoff Methodology — automated (Carol) edition

Adapted June 2026 from the Cowork "How I Do a Takeoff & Estimate — A to Z" doc.
That document's honesty about error sources is the DESIGN SPEC for Carol's
auto-takeoff: every insight became a method tier, a check, or a rule below.

## Method tiers (every quantity is labeled)

| Tier | Source | Trust | Typical error |
|---|---|---|---|
| **M1 measured** | Togal geometry: wall centerlines, footprint outline, door counts | Highest | ±5-10% |
| **M2 anchors** | Architect's printed data: program GSF, area matrix, unit counts, RCP height notes | High | as-drawn |
| **M3 visual** | Rendered-sheet reading (labels/schedules/counts by eye) | Medium | ±10-30% |
| **M4 parametric** | Facility wall-ratio heuristics (floor SF × 2.8 etc.) | Lowest | ±20-50% per line |

Rules:
- **M1 replaces everything** when available. M4 exists only to CROSS-CHECK M1
  and to carry SD-quality sets where Togal is structurally blind (Clemson).
- A good TOTAL from M4 does not mean good LINES (Cowork: -11% / -36% / +213%
  lines offsetting to an accurate total). Never quote M4 line items as measured.
- What Togal is actually good at (validated on WSSU): **wall centerlines with
  class labels, the building outline polygon, door/fixture counts, page text.**
  What it is NOT good at: room polygons + their classifications (noise), and
  anything on schematic-design linework (zero or garbage).

## The pipeline (scripts/auto_takeoff.py)

1. **Sheet selection** — discipline-aware picker (togal_pipeline._plan_score).
2. **Scale** — read the title block of THE SHEET USED (\_lib/takeoff_scale.py);
   verify it equals the scale set on the Togal pages. NEVER default a scale.
3. **Detect** — upload → /v1/page-processing/run (the detection AI) → extract.
4. **Quantities (M1)** — centerline LF by class × ceiling height (M2, parsed
   from RCP text) = wall face SF; footprint from outline polygon; door counts.
5. **Cross-validate** — M1 wall SF vs M4 (footprint × facility ratio) ≤25% Δ.
6. **THE GATE** — all checks must pass for confidence HIGH:
   scale_confirmed · scale_matches_togal_pages · ceiling_height_parsed ·
   wall_floor_ratio_in_band · method_agreement_M1_vs_M4 · perimeter_geometry ·
   program_sf_crosscheck (±15%) · doors_density_sane.
   HIGH → auto-proceed to estimate. REVIEW → human sees the failing checks.
7. **Scope is judgment, not measurement** — finish-schedule/spec reading
   (deductions: tile walls, WC walls, ACT ceilings, openings) ALWAYS gets
   human eyes before a proposal goes out. The gate covers quantities only.

## Known failure modes (encoded, not just remembered)

- Wrong default scale (1/8 vs 3/16 → 2.25× area) → checks 2+3, eval cases.
- Detection AI never triggered (scale-only endpoint) → baked into run_full.
- Togal list endpoints cap at 10 rows → paginated client.
- Room polygons trusted → only centerlines/outline/counts are used.
- Tag-scan caps dropping a unit type (the 6x6) → scan full range, verify
  counts against the area matrix total.
- SD sets → auto-route to M2/M4 budget method, labeled as budget, never
  presented as measured.

## Division of labor (the optimal workflow, automated)

Togal measures (its strength) → Carol reads all docs, assembles scope, applies
CCF rates/materials, builds deliverables (her strength) → the human reviews
scope + the top 3-5 cost lines + anything the gate flagged. Days of takeoff →
minutes of review. Never zero review on scope.
