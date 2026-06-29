#!/usr/bin/env python3
"""
Estimator Agent — SOW + Takeoff + Pricing
==========================================
Runs the full estimation pipeline from "let's bid this" to "here is the estimate."
Activated by Carol Core when owner says "bid [project]".

Pipeline stages managed:
  scouted → docs_downloading → docs_ready → sow_building → sow_ready →
  takeoff_planning → takeoff_uploading → takeoff_done →
  estimating → estimate_ready
"""

import json
import math
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
PROJECTS_DIR = DATA_DIR / "projects"
BIDS_FILE = DATA_DIR / "memory" / "active_bids.json"
PRICING_FILE = DATA_DIR / "pricing" / "ccf-pricing-config.json"
SCRIPTS_DIR = BASE_DIR / "scripts"
SKILL_SCRIPTS = BASE_DIR / "skills" / "ccf-estimator" / "scripts"
DIAGNOSIS_FILE = DATA_DIR / "pricing" / "pricing-diagnosis.json"

# Document reading priority — most scope-dense first
DOCUMENT_PRIORITY = [
    "scope_letter", "invitation", "bid_invite",
    "spec_09900", "spec_09720", "painting_spec",
    "finish_schedule",
    "floor_plan",
    "rcp", "reflected_ceiling",
    "interior_elevation",
    "addendum",
    "walk_notes", "site_visit",
]

# ---------------------------------------------------------------------------
#  Task-code → production-rate mapping
#  Keys match task names in ccf-pricing-config.json production_rates.painting[]
# ---------------------------------------------------------------------------
TASK_CODE_MAP = {
    # Interior walls
    "walls_new_drywall_prime_2coat":      "New drywall — prime + 2 coats",
    "walls_repaint_2coat":                "Repaint — 2 coats",
    "walls_repaint_1coat":                "Repaint — 1 coat touch-up",
    "walls_cutin":                        "Cut-in / edging only",
    "walls_spray_prime":                  "New drywall — spray prime",
    "walls_spray_2coat":                  "New drywall — spray 2 coats",
    "walls_spray_large":                  "Large open walls — spray",
    "walls_spray_backroll":               "Spray + back-roll",
    # Ceilings
    "ceiling_act_spray":                  "ACT / drop ceiling grid — spray",
    "ceiling_drywall_roll":               "Drywall ceiling — roll",
    "ceiling_drywall_spray":              "Drywall ceiling — spray",
    # Trim & doors
    "trim_base":                          "Base/shoe/chair rail",
    "door_paint":                         "Door — paint (per side)",
    "door_frame":                         "Door frame — paint",
    "window_frame":                       "Window frame",
    # Exterior
    "exterior_spray":                     "Exterior walls — spray",
    "exterior_roll":                      "Exterior walls — roll",
    "exterior_pressure_wash":             "Exterior — pressure wash (prep)",
    # Specialty
    "epoxy_floor":                        "Epoxy floor coating",
    "stain_wood":                         "Stain — interior wood",
    "texture_knockdown":                  "Texture — knockdown/orange peel",
    "tape_float":                         "Tape & float (drywall finish)",
}

# Prep task codes
PREP_CODE_MAP = {
    "prep_masking":         "Masking / protection",
    "prep_sanding_light":   "Sanding — light",
    "prep_sanding_heavy":   "Sanding — heavy / patching",
    "prep_caulking":        "Caulking",
    "prep_cleaning":        "Surface cleaning",
    "prep_wallpaper_remove": "Demolition — wallpaper removal",
    "prep_spot_prime":      "Primer — spot prime patches",
}

# Unit-price task matching (for quick-estimate mode)
UNIT_PRICE_MAP = {
    "walls_brush_roll":     "Walls — 2 coats, brush & roll",
    "walls_spray":          "Walls — 2 coats, spray",
    "walls_spray_backroll": "Walls — spray + back-roll",
    "ceiling_roll":         "Ceilings — roll (2 coats)",
    "ceiling_spray":        "Ceilings — spray (2 coats)",
    "trim":                 "Trim / base / chair rail",
    "door_complete":        "Doors — complete (2 sides + frame)",
    "ext_spray":            "Walls — spray (2 coats)",
    "ext_roll":             "Walls — roll (2 coats)",
    "ext_pressure_wash":    "Pressure wash prep",
    "epoxy":                "Epoxy floor (2 coats)",
    "stain":                "Stain — wood (2 coats)",
    "texture":              "Texture application",
}

# Material cost reference ($/SF per coat)
MATERIAL_COSTS = {
    "latex_standard":       0.12,   # ProMar 200 Eg-Shel
    "precat_epoxy":         0.18,   # K45/K46
    "epoxy_macropoxy":      0.25,   # Macropoxy 646
    "semi_gloss_trim":      0.15,   # Semi-gloss for trim
    "dtm_acrylic":          0.15,   # DTM exterior
    "primer_standard":      0.10,   # Standard primer
    "stain":                0.14,   # Interior stain
    "texture":              0.08,   # Texture material
    "wallcovering_adhesive": 0.05,  # Wallcovering adhesive only
}

# Per-unit material costs
MATERIAL_PER_UNIT = {
    "door_single_hm":      8.50,   # Single HM door + frame
    "door_double_hm":      14.00,  # Double HM door + frame
    "door_sliding_frame":  7.00,   # Sliding door frame
}


class EstimatorAgent:
    """Runs the full estimation pipeline for a single project."""

    def __init__(self, pipeline_state=None):
        self.pipeline = pipeline_state
        self._pricing = None   # lazy-loaded
        self._diagnosis = None # lazy-loaded

    # ------------------------------------------------------------------
    #  Pricing config (lazy-loaded)
    # ------------------------------------------------------------------
    @property
    def pricing(self) -> dict:
        if self._pricing is None:
            if PRICING_FILE.exists():
                self._pricing = json.loads(PRICING_FILE.read_text(encoding="utf-8"))
            else:
                self._pricing = self._default_pricing()
        return self._pricing

    @property
    def diagnosis(self) -> dict:
        """Pricing diagnosis: win/loss data, GC feedback, strategy rules."""
        if self._diagnosis is None:
            if DIAGNOSIS_FILE.exists():
                self._diagnosis = json.loads(DIAGNOSIS_FILE.read_text(encoding="utf-8"))
            else:
                self._diagnosis = {}
        return self._diagnosis

    def get_pricing_intelligence(self, gc_name: str = None,
                                 facility_type: str = None,
                                 bid_amount: float = None) -> list:
        """
        Return pricing warnings/tips based on diagnosis data.
        Called during estimate to flag issues before submitting.
        """
        warnings = []
        rules = self.diagnosis.get("pricing_rules_for_carol", {})
        diag = self.diagnosis

        # Check if bid is in our sweet spot
        if bid_amount and rules.get("sweet_spot_range"):
            low, high = rules["sweet_spot_range"]
            if bid_amount > high:
                warnings.append(
                    f"Bid ${bid_amount:,.0f} is above our sweet spot ($25K-$100K). "
                    f"Consider tighter pricing — large bids are our weakest area."
                )

        # Check GC feedback history
        if gc_name:
            gc_lower = gc_name.lower()
            for fb in diag.get("gc_feedback", []):
                if fb.get("gc", "").lower() in gc_lower or gc_lower in fb.get("gc", "").lower():
                    gap = fb.get("gap", "")
                    warnings.append(
                        f"GC HISTORY: {fb['gc']} on {fb['project']} — "
                        f"\"{fb['feedback']}\" (gap: {gap})"
                    )

        # Check facility type strengths
        if facility_type:
            strong = rules.get("strong_facility_types", [])
            ft_lower = facility_type.lower()
            if any(s in ft_lower for s in strong):
                warnings.append(
                    f"STRENGTH: {facility_type} is one of our strongest types. "
                    f"Price confidently at TARGET or above."
                )

        # General pricing check
        win_loss = diag.get("win_loss_summary", {})
        if win_loss.get("wins_from_last_20_estimating_bids", 0) == 0:
            warnings.append(
                "REMINDER: 0 wins from last 20 estimating bids. "
                "Default to spray-first rates. Keep blended under $1.00/SF for new construction."
            )

        return warnings

    @staticmethod
    def _default_pricing() -> dict:
        return {
            "production_rates": {"painting": [], "prep": [], "non_painting": []},
            "pricing_policy": {
                "labor_rates": [],
                "unit_prices": [],
                "markup_scenarios": [],
                "overhead": [],
                "bid_formula": [],
            },
        }

    def _get_prod_rate(self, task_name: str, speed: str = "avg") -> float:
        """Look up a production rate by task name. speed = slow/avg/fast."""
        key = f"{speed}_rate"
        for task in self.pricing.get("production_rates", {}).get("painting", []):
            if task["task"] == task_name:
                return task.get(key, task.get("avg_rate", 150))
        for task in self.pricing.get("production_rates", {}).get("prep", []):
            if task["task"] == task_name:
                return task.get(key, task.get("avg_rate", 150))
        return 150  # safe fallback

    def _get_unit_price(self, task_name: str, tier: str = "TARGET") -> tuple:
        """Return (low, high) unit price for a task at the given tier."""
        tier_key = {
            "FLOOR": "floor_price",
            "TARGET": "target_price",
            "PREMIUM": "premium_price",
        }.get(tier, "target_price")
        for up in self.pricing.get("pricing_policy", {}).get("unit_prices", []):
            if up["task"] == task_name:
                prices = up.get(tier_key, [0, 0])
                if isinstance(prices, list) and len(prices) == 2:
                    return (prices[0], prices[1])
                return (0, 0)
        return (0, 0)

    def _get_tier_percentages(self, tier: str = "TARGET") -> dict:
        """Get overhead% and profit% for a pricing tier."""
        tiers = {
            "FLOOR":   {"overhead_pct": 0.10, "profit_pct": 0.08},
            "TARGET":  {"overhead_pct": 0.12, "profit_pct": 0.18},
            "PREMIUM": {"overhead_pct": 0.15, "profit_pct": 0.25},
        }
        return tiers.get(tier, tiers["TARGET"])

    def _get_labor_rate(self, role: str = "experienced") -> float:
        """Burdened labor rate. Default $28/hr."""
        rates = {
            "entry": 22.50,       # midpoint $21-24
            "experienced": 28.00, # midpoint $25-31
            "lead": 34.50,        # midpoint $31-38
        }
        return rates.get(role, 28.00)

    def _speed_for_tier(self, tier: str) -> str:
        """Map pricing tier to production rate speed."""
        return {"FLOOR": "slow", "TARGET": "avg", "PREMIUM": "fast"}.get(tier, "avg")

    # ==================================================================
    #  MASTER PIPELINE
    # ==================================================================

    def start_pipeline(self, slug: str, pricing_tier: str = "TARGET") -> str:
        """
        Kick off the estimation pipeline. Downloads docs, returns status.
        Owner says 'continue [project]' to advance through remaining stages.
        """
        proj = self.pipeline.get_project(slug)
        if not proj:
            return f"Project {slug} not found in pipeline."

        # Step 1: Download documents
        self.pipeline.update_stage(slug, "docs_downloading")
        docs_result = self.download_documents(slug, proj)

        if docs_result.get("error"):
            return f"Document download failed: {docs_result['error']}"

        self.pipeline.update_stage(slug, "docs_ready")

        doc_count = docs_result.get("total_files", 0)
        return (
            f"Documents downloaded for {proj['name']}.\n"
            f"Files: {doc_count}\n"
            f"Stage: docs_ready\n\n"
            f"Next: I'll read the docs and build the Scope of Work.\n"
            f"Say \"continue {proj['name']}\" to proceed."
        )

    def continue_pipeline(self, slug: str, pricing_tier: str = "TARGET") -> str:
        """
        Continue the pipeline from wherever it left off.
        Advances through: docs_ready → sow → takeoff → estimate.
        """
        proj = self.pipeline.get_project(slug)
        if not proj:
            return f"Project {slug} not found in pipeline."

        stage = proj.get("stage", "scouted")

        # Route based on current stage
        if stage in ("scouted", "docs_downloading"):
            return self.start_pipeline(slug, pricing_tier)

        elif stage == "docs_ready":
            self.pipeline.update_stage(slug, "sow_building")
            sow = self.build_sow(slug)
            if sow.get("error"):
                return f"SOW build failed: {sow['error']}"

            sow_summary = self._format_sow_summary(sow)
            return (
                f"SOW built for {proj['name']}.\n\n"
                f"{sow_summary}\n\n"
                f"Stage: sow_ready\n"
                f"Say \"continue {proj['name']}\" to build the takeoff plan."
            )

        elif stage in ("sow_ready", "sow_building"):
            plan = self.build_takeoff_plan(slug)
            if plan.get("error"):
                return f"Takeoff plan failed: {plan['error']}"

            sheets = len(plan.get("sheets_for_togal", []))
            items = len(plan.get("measurement_matrix", []))
            return (
                f"Takeoff plan built for {proj['name']}.\n"
                f"Sheets identified: {sheets}\n"
                f"Measurement items: {items}\n\n"
                f"Stage: takeoff_planning\n"
                f"You can upload a CSV/XLSX from STACK, or text me the quantities.\n"
                f"Or say \"continue {proj['name']}\" to auto-estimate from SOW data."
            )

        elif stage in ("takeoff_planning", "takeoff_uploading", "takeoff_done"):
            estimate = self.build_estimate(slug, pricing_tier=pricing_tier)
            if estimate.get("error"):
                return f"Estimate failed: {estimate['error']}"

            summary_text = self._generate_estimate_summary(slug, estimate)
            return summary_text

        elif stage == "estimating":
            # Already estimating, rebuild
            estimate = self.build_estimate(slug, pricing_tier=pricing_tier)
            if estimate.get("error"):
                return f"Estimate failed: {estimate['error']}"
            return self._generate_estimate_summary(slug, estimate)

        elif stage == "estimate_ready":
            return self.get_estimate(slug)

        else:
            return (
                f"{proj['name']} is at stage \"{stage}\".\n"
                f"Estimation phase is complete. "
                f"Next: \"draft proposal {proj['name']}\""
            )

    # ==================================================================
    #  DOCUMENT DOWNLOAD
    # ==================================================================

    def download_documents(self, slug: str, proj: dict) -> dict:
        """Run fetch_project_docs.py to download bid documents."""
        proj_dir = PROJECTS_DIR / slug
        proj_dir.mkdir(parents=True, exist_ok=True)
        (proj_dir / "documents").mkdir(exist_ok=True)

        script = SCRIPTS_DIR / "fetch_project_docs.py"
        if not script.exists():
            return {"error": "fetch_project_docs.py not found"}

        try:
            result = subprocess.run(
                [sys.executable, str(script), "--force", proj["name"]],
                capture_output=True, text=True, timeout=300,
                cwd=str(BASE_DIR),
            )
            for line in result.stdout.splitlines():
                if line.startswith("__RESULT__:"):
                    return json.loads(line[len("__RESULT__:"):])
            return {
                "status": "completed" if result.returncode == 0 else "failed",
                "total_files": self._count_docs(proj_dir / "documents"),
                "stdout": result.stdout[-500:],
                "stderr": result.stderr[-500:],
            }
        except subprocess.TimeoutExpired:
            return {"error": "Document download timed out (5 min)"}
        except Exception as e:
            return {"error": str(e)}

    @staticmethod
    def _count_docs(docs_dir: Path) -> int:
        if not docs_dir.exists():
            return 0
        return len([f for f in docs_dir.rglob("*") if f.is_file()])

    # ==================================================================
    #  BUILD SOW (Scope of Work)
    # ==================================================================

    def build_sow(self, slug: str) -> dict:
        """
        Read all documents using Claude API and build structured SOW.
        Returns SOW dict: interior_painting, exterior_painting, wallcovering,
        prep_work, exclusions, special_conditions, summary.
        """
        proj_dir = PROJECTS_DIR / slug
        docs_dir = proj_dir / "documents"
        bid_docs_dir = proj_dir / "bid_docs"

        # Check both directories for PDFs
        if not docs_dir.exists() and not bid_docs_dir.exists():
            return {"error": "No documents directory. Run download first."}
        if not docs_dir.exists() and bid_docs_dir.exists():
            docs_dir = bid_docs_dir

        # Check for existing scope_extract from fetch_project_docs.py
        scope_extract = proj_dir / "scope_extract.json"
        if scope_extract.exists():
            existing = json.loads(scope_extract.read_text(encoding="utf-8"))
            if existing and not existing.get("error"):
                # Enhance with Claude API analysis
                sow = self._enhance_scope_extract(slug, existing, docs_dir)
                if sow and not sow.get("error"):
                    sow_path = proj_dir / "sow.json"
                    sow_path.write_text(json.dumps(sow, indent=2, ensure_ascii=False), encoding="utf-8")
                    self.pipeline.update_stage(slug, "sow_ready")
                    return sow

        # No existing scope extract — read PDFs directly
        pdfs = sorted(docs_dir.rglob("*.pdf"))
        if not pdfs:
            return {"error": "No PDF documents found."}

        doc_texts = self._extract_pdf_texts(pdfs)
        if not doc_texts:
            return {"error": "Could not extract text from any PDFs."}

        sow = self._call_llm_for_sow(slug, doc_texts)

        sow_path = proj_dir / "sow.json"
        sow_path.write_text(json.dumps(sow, indent=2, ensure_ascii=False), encoding="utf-8")
        self.pipeline.update_stage(slug, "sow_ready")
        return sow

    def _extract_pdf_texts(self, pdfs: list) -> list:
        """Extract text from PDFs using pdfplumber. Sorted by DOCUMENT_PRIORITY."""
        try:
            import pdfplumber
        except ImportError:
            return []

        # Sort by priority
        def priority_key(pdf_path):
            name = pdf_path.stem.lower()
            for i, keyword in enumerate(DOCUMENT_PRIORITY):
                if keyword in name:
                    return i
            return 999

        pdfs = sorted(pdfs, key=priority_key)

        doc_texts = []
        for pdf_path in pdfs:
            try:
                with pdfplumber.open(pdf_path) as pdf:
                    text = ""
                    for page in pdf.pages[:50]:
                        page_text = page.extract_text() or ""
                        text += page_text + "\n"
                    if text.strip():
                        doc_texts.append({
                            "filename": pdf_path.name,
                            "text": text[:12000],
                        })
            except Exception:
                continue

        return doc_texts

    def _enhance_scope_extract(self, slug: str, scope_data: dict, docs_dir: Path) -> dict:
        """If we already have a scope_extract, enhance it with Claude analysis."""
        # If the scope_extract already has structured painting data, use it
        if isinstance(scope_data, dict) and scope_data.get("interior_painting"):
            return scope_data

        # Otherwise, combine with PDF text and call Claude
        pdfs = sorted(docs_dir.rglob("*.pdf"))
        doc_texts = self._extract_pdf_texts(pdfs)

        # Add scope extract as additional context
        if doc_texts:
            return self._call_llm_for_sow(slug, doc_texts, extra_context=scope_data)
        return scope_data

    def _call_llm_for_sow(self, slug: str, doc_texts: list,
                           extra_context: dict = None) -> dict:
        """Call LLM (Gemini/Anthropic/Ollama via litellm) to analyze documents and build structured SOW."""
        try:
            import litellm
        except ImportError:
            return {"error": "litellm package not installed. Run: pip install litellm"}

        # Model priority (built-in failover chain):
        #   1. Groq llama-3.3-70b (free tier, 70B, ~500 tok/s, 128k ctx)
        #   2. Google Gemini 2.5 Flash (1M ctx for huge doc sets, free tier available)
        #   3. Cerebras llama3.1-8b (free tier, ultra-fast, smaller)
        #   4. Anthropic Sonnet (paid emergency)
        #   5. Ollama local (offline last resort)
        fallbacks = []
        if os.environ.get("GROQ_API_KEY"):
            fallbacks.append("groq/llama-3.3-70b-versatile")
        if os.environ.get("GEMINI_API_KEY"):
            fallbacks.append("gemini/gemini-2.5-flash")
        if os.environ.get("CEREBRAS_API_KEY"):
            fallbacks.append("cerebras/llama3.1-8b")
        if os.environ.get("ANTHROPIC_API_KEY"):
            fallbacks.append("anthropic/claude-sonnet-4-20250514")
        fallbacks.append("ollama/gemma4:latest")
        model = fallbacks[0] if fallbacks else "ollama/gemma4:latest"

        # Build document context
        doc_context = ""
        for doc in doc_texts:
            doc_context += f"\n--- {doc['filename']} ---\n{doc['text']}\n"

        if extra_context:
            doc_context += f"\n--- PREVIOUSLY EXTRACTED SCOPE ---\n{json.dumps(extra_context, indent=2)}\n"

        system_prompt = """You are a commercial painting estimator for Carolina Commercial Finishes (CCF).
Analyze the bid documents and extract the PAINTING AND WALLCOVERING scope only.
Return a JSON object with exactly these keys:

{
  "interior_painting": {
    "walls": [{"area": "name", "description": "...", "finish": "...", "method": "spray|brush_roll|spray_backroll", "coats": "1P+2F", "estimated_sf": 0}],
    "ceilings": [{"area": "name", "type": "GWB|ACT|exposed", "method": "spray|roll", "coats": "2", "estimated_sf": 0}],
    "doors": {"count": 0, "type": "hollow_core|solid_core", "includes_frames": true, "coats": "2"},
    "frames": {"count": 0, "type": "HM|wood", "coats": "2"},
    "trim": [{"item": "base/chair_rail/crown", "lf": 0, "coats": "2"}],
    "misc": [{"item": "...", "quantity": 0, "unit": "SF|EA|LF"}]
  },
  "exterior_painting": {
    "surfaces": [{"area": "name", "substrate": "...", "method": "spray|roll", "coats": "2", "estimated_sf": 0}],
    "prep": [{"item": "pressure_wash|scraping|caulking", "estimated_sf": 0}]
  },
  "wallcovering": {
    "locations": [{"area": "name", "type": "Type_II_vinyl|sisal|custom", "estimated_sf": 0}]
  },
  "prep_work": {
    "items": [{"task": "masking|sanding|caulking|cleaning|priming|patching", "estimated_sf_or_lf": 0, "notes": ""}]
  },
  "exclusions": ["items CCF should explicitly exclude"],
  "special_conditions": {
    "prevailing_wage": false,
    "night_work": false,
    "phased": false,
    "occupied": false,
    "high_work": false,
    "containment": false,
    "notes": ""
  },
  "summary": {
    "total_sf_walls": 0,
    "total_sf_ceilings": 0,
    "total_doors": 0,
    "total_lf_trim": 0,
    "total_sf_exterior": 0,
    "total_sf_wallcovering": 0,
    "complexity": "simple|moderate|complex",
    "key_scope_items": ["brief list of main work items"],
    "estimated_duration_days": 0
  }
}

IMPORTANT:
- Extract ONLY painting, wallcovering, and related prep work
- If you can estimate SF from document descriptions, do so
- If quantities aren't specified, set to 0 and note "TBD - needs takeoff"
- Flag any prevailing wage, Davis-Bacon, or special labor requirements
- Return ONLY valid JSON, no markdown or explanation"""

        # Try each model in the fallback chain until one succeeds
        last_err = None
        for candidate in fallbacks:
            try:
                response = litellm.completion(
                    model=candidate,
                    max_tokens=8096,
                    timeout=60,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": f"Analyze these bid documents for painting scope:\n{doc_context[:50000]}"},
                    ],
                )
                text = response.choices[0].message.content

                json_match = re.search(r'\{[\s\S]*\}', text)
                if json_match:
                    sow = json.loads(json_match.group())
                    bad = self._validate_sow_numerics(sow)
                    if bad:
                        print(f"  SOW numeric validation failed ({candidate}): {bad}")
                        return {"raw_analysis": text,
                                "error": f"SOW failed numeric validation: {bad}",
                                "_model": candidate}
                    sow["_generated_at"] = datetime.now().isoformat()
                    sow["_source_docs"] = [d["filename"] for d in doc_texts]
                    sow["_model"] = candidate
                    return sow
                return {"raw_analysis": text, "error": "Could not parse structured SOW", "_model": candidate}
            except Exception as e:
                last_err = e
                # Retry on rate limits / 429 / quota errors; otherwise surface immediately
                msg = str(e).lower()
                if any(t in msg for t in ("429", "rate limit", "quota", "too many", "overloaded", "capacity")):
                    continue
                # For non-rate-limit errors, also try next fallback — could be model-specific issue
                continue
        return {"error": f"All LLM providers failed. Last error: {last_err}"}

    # Sane bounds for LLM-extracted SOW quantities
    SOW_MAX_SF = 2_000_000
    SOW_MIN_TOTAL_SF = 100

    def _validate_sow_numerics(self, sow: dict) -> str:
        """Validate/coerce numeric SOW fields used downstream (estimated_sf etc).

        Coerces numeric strings to numbers in place. Returns "" when valid,
        else an error message. Zero means "TBD - needs takeoff" and is
        allowed; non-numeric values or quantities outside 0..2,000,000 SF
        (or a nonzero total below 100 SF) are rejected as garbage.
        """
        problems = []

        def coerce(d, key, label):
            val = d.get(key, 0)
            if val in (None, ""):
                d[key] = 0
                return 0.0
            try:
                if isinstance(val, bool):
                    raise ValueError
                num = float(str(val).replace(",", "").strip())
            except (ValueError, TypeError):
                problems.append(f"{label}={val!r} not numeric")
                d[key] = 0
                return 0.0
            if num < 0 or num > self.SOW_MAX_SF:
                problems.append(f"{label}={num:.0f} out of bounds")
                d[key] = 0
                return 0.0
            d[key] = num
            return num

        ip = sow.get("interior_painting") or {}
        ep = sow.get("exterior_painting") or {}
        wc = sow.get("wallcovering") or {}
        pw = sow.get("prep_work") or {}

        def rows(parent, key):
            return (parent.get(key) or []) if isinstance(parent, dict) else []

        total_sf = 0.0
        groups = [
            (rows(ip, "walls"), "estimated_sf", "interior.walls", True),
            (rows(ip, "ceilings"), "estimated_sf", "interior.ceilings", True),
            (rows(ip, "trim"), "lf", "interior.trim", False),
            (rows(ip, "misc"), "quantity", "interior.misc", False),
            (rows(ep, "surfaces"), "estimated_sf", "exterior.surfaces", True),
            (rows(ep, "prep"), "estimated_sf", "exterior.prep", False),
            (rows(wc, "locations"), "estimated_sf", "wallcovering", True),
            (rows(pw, "items"), "estimated_sf_or_lf", "prep_work", False),
        ]
        for row_list, key, label, in_total in groups:
            for i, row in enumerate(row_list):
                if isinstance(row, dict):
                    num = coerce(row, key, f"{label}[{i}].{key}")
                    if in_total:
                        total_sf += num
        if isinstance(ip, dict):
            for k in ("doors", "frames"):
                if isinstance(ip.get(k), dict):
                    coerce(ip[k], "count", f"interior.{k}.count")

        summary = sow.get("summary")
        if isinstance(summary, dict):
            sum_sf = 0.0
            for k in ("total_sf_walls", "total_sf_ceilings",
                      "total_sf_exterior", "total_sf_wallcovering"):
                sum_sf += coerce(summary, k, f"summary.{k}")
            for k in ("total_doors", "total_lf_trim", "estimated_duration_days"):
                coerce(summary, k, f"summary.{k}")
            total_sf = max(total_sf, sum_sf)

        if problems:
            return "; ".join(problems[:5])
        if 0 < total_sf < self.SOW_MIN_TOTAL_SF:
            return f"total estimated SF {total_sf:.0f} below sane minimum ({self.SOW_MIN_TOTAL_SF})"
        if total_sf > self.SOW_MAX_SF:
            return f"total estimated SF {total_sf:.0f} above sane maximum ({self.SOW_MAX_SF:,})"
        return ""

    def _format_sow_summary(self, sow: dict) -> str:
        """Format SOW into readable text for the owner."""
        if sow.get("error"):
            return f"Error: {sow['error']}"

        lines = ["SCOPE OF WORK", "=" * 40]

        summary = sow.get("summary", {})
        if isinstance(summary, dict):
            if summary.get("key_scope_items"):
                lines.append("Key scope: " + ", ".join(summary["key_scope_items"]))
            lines.append(f"Complexity: {summary.get('complexity', '?')}")
            if summary.get("total_sf_walls"):
                lines.append(f"Walls: ~{summary['total_sf_walls']:,} SF")
            if summary.get("total_sf_ceilings"):
                lines.append(f"Ceilings: ~{summary['total_sf_ceilings']:,} SF")
            if summary.get("total_doors"):
                lines.append(f"Doors: {summary['total_doors']}")
            if summary.get("total_lf_trim"):
                lines.append(f"Trim: ~{summary['total_lf_trim']:,} LF")
            if summary.get("total_sf_exterior"):
                lines.append(f"Exterior: ~{summary['total_sf_exterior']:,} SF")
            if summary.get("total_sf_wallcovering"):
                lines.append(f"Wallcovering: ~{summary['total_sf_wallcovering']:,} SF")

        # Interior painting
        ip = sow.get("interior_painting", {})
        if isinstance(ip, dict):
            walls = ip.get("walls", [])
            if walls:
                lines.append(f"\nINTERIOR WALLS ({len(walls)} areas):")
                for w in walls[:10]:
                    sf = w.get("estimated_sf", 0)
                    sf_str = f" ~{sf:,} SF" if sf else ""
                    lines.append(f"  - {w.get('area', '?')}: {w.get('method', '?')} {w.get('coats', '')}{sf_str}")

            doors = ip.get("doors", {})
            if isinstance(doors, dict) and doors.get("count"):
                lines.append(f"\nDOORS: {doors['count']} ({doors.get('type', '?')})")

        # Exterior
        ep = sow.get("exterior_painting", {})
        if isinstance(ep, dict) and ep.get("surfaces"):
            lines.append(f"\nEXTERIOR ({len(ep['surfaces'])} surfaces):")
            for s in ep["surfaces"][:5]:
                lines.append(f"  - {s.get('area', '?')}: {s.get('method', '?')}")

        # Wallcovering
        wc = sow.get("wallcovering", {})
        if isinstance(wc, dict) and wc.get("locations"):
            lines.append(f"\nWALLCOVERING ({len(wc['locations'])} locations):")
            for loc in wc["locations"][:5]:
                lines.append(f"  - {loc.get('area', '?')}: {loc.get('type', '?')}")

        # Special conditions
        sc = sow.get("special_conditions", {})
        if isinstance(sc, dict):
            flags = []
            if sc.get("prevailing_wage"): flags.append("PREVAILING WAGE")
            if sc.get("night_work"):      flags.append("NIGHT WORK")
            if sc.get("phased"):          flags.append("PHASED")
            if sc.get("occupied"):        flags.append("OCCUPIED BUILDING")
            if sc.get("high_work"):       flags.append("HIGH WORK (lifts)")
            if sc.get("containment"):     flags.append("CONTAINMENT REQ")
            if flags:
                lines.append(f"\nSPECIAL CONDITIONS: {', '.join(flags)}")

        # Exclusions
        excl = sow.get("exclusions", [])
        if excl:
            lines.append(f"\nEXCLUSIONS: {', '.join(excl[:8])}")

        return "\n".join(lines)

    # ==================================================================
    #  BUILD TAKEOFF PLAN
    # ==================================================================

    def build_takeoff_plan(self, slug: str) -> dict:
        """
        Build takeoff plan from SOW and available drawings.
        Classifies drawing sheets, builds measurement matrix.
        """
        proj_dir = PROJECTS_DIR / slug
        sow_path = proj_dir / "sow.json"

        if not sow_path.exists():
            return {"error": "No SOW found. Run build_sow first."}

        sow = json.loads(sow_path.read_text(encoding="utf-8"))

        plan = {
            "project": slug,
            "created_at": datetime.now().isoformat(),
            "sheets_for_togal": [],
            "measurement_matrix": [],
            "notes": [],
        }

        # Classify drawing files
        docs_dir = proj_dir / "documents"
        if docs_dir.exists():
            for d in sorted(docs_dir.rglob("*.pdf")):
                sheet_type = self._classify_sheet(d.name)
                if sheet_type:
                    measures = self._measures_for_sheet(sheet_type)
                    plan["sheets_for_togal"].append({
                        "file": d.name,
                        "type": sheet_type,
                        "measure": measures,
                    })

        # Build measurement matrix from SOW
        plan["measurement_matrix"] = self._sow_to_measurement_matrix(sow)

        # Add notes based on special conditions
        sc = sow.get("special_conditions", {})
        if isinstance(sc, dict):
            if sc.get("high_work"):
                plan["notes"].append("Include lift rental in equipment costs")
            if sc.get("prevailing_wage"):
                plan["notes"].append("PREVAILING WAGE — use certified payroll rates, not standard $28/hr")
            if sc.get("night_work"):
                plan["notes"].append("Night work — add 15-20% labor premium")
            if sc.get("phased"):
                plan["notes"].append("Phased work — add mobilization for each phase")

        plan_path = proj_dir / "takeoff_plan.json"
        plan_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")

        self.pipeline.update_stage(slug, "takeoff_planning")
        return plan

    @staticmethod
    def _classify_sheet(filename: str) -> str | None:
        """Classify a drawing sheet by filename."""
        name = filename.lower()
        patterns = {
            "floor_plan":       ["floor plan", "floor_plan", "fp", "a1.", "a2.", " plan", "architectural plan"],
            "rcp":              ["rcp", "reflected ceiling", "ceiling plan", "a5."],
            "interior_elevation": ["interior elev", "int elev", "a3.", "elevation"],
            "exterior_elevation": ["exterior elev", "ext elev"],
            "finish_schedule":  ["finish schedule", "finish_schedule", "room finish"],
            "door_schedule":    ["door schedule", "door_schedule", "hw schedule"],
            "site_plan":        ["site plan", "site_plan", "c1."],
            "roof_plan":        ["roof plan", "roof_plan"],
            "detail":           ["detail", "dtl"],
            "spec":             ["spec", "specification", "section 09"],
        }
        for sheet_type, keywords in patterns.items():
            if any(k in name for k in keywords):
                return sheet_type
        # Check for common architectural sheet numbering
        if re.match(r'a[0-9]', name):
            return "floor_plan"
        return None

    @staticmethod
    def _measures_for_sheet(sheet_type: str) -> list:
        """What to measure on each sheet type."""
        measures = {
            "floor_plan":         ["net_wall_sf", "wall_perimeter_lf", "room_count", "door_count"],
            "rcp":                ["ceiling_sf_by_type", "ceiling_height"],
            "interior_elevation": ["accent_wall_sf", "specialty_finish_sf"],
            "exterior_elevation": ["exterior_wall_sf", "window_count"],
            "finish_schedule":    ["finish_types_by_room", "paint_colors"],
            "door_schedule":      ["door_count_by_type", "frame_types"],
            "site_plan":          ["building_perimeter", "exterior_sf"],
        }
        return measures.get(sheet_type, [])

    def _sow_to_measurement_matrix(self, sow: dict) -> list:
        """Convert SOW sections into measurement checklist items."""
        matrix = []

        ip = sow.get("interior_painting", {})
        if isinstance(ip, dict):
            # Walls
            walls = ip.get("walls", [])
            if walls:
                for w in walls:
                    matrix.append({
                        "scope": f"Interior Walls — {w.get('area', 'General')}",
                        "measure": "Net SF (deduct openings >4 SF), LF perimeter",
                        "method": w.get("method", "TBD"),
                        "coats": w.get("coats", "2"),
                        "estimated_sf": w.get("estimated_sf", 0),
                        "source": "floor plans",
                        "task_code": self._pick_wall_task_code(w),
                    })
            elif sow.get("summary", {}).get("total_sf_walls"):
                matrix.append({
                    "scope": "Interior Walls — All Areas",
                    "measure": "Net SF (deduct openings >4 SF)",
                    "method": "TBD",
                    "coats": "1P+2F",
                    "estimated_sf": sow["summary"]["total_sf_walls"],
                    "source": "floor plans",
                    "task_code": "walls_new_drywall_prime_2coat",
                })

            # Ceilings
            ceilings = ip.get("ceilings", [])
            if ceilings:
                for c in ceilings:
                    code = "ceiling_drywall_spray" if c.get("type") == "GWB" else "ceiling_act_spray"
                    matrix.append({
                        "scope": f"Ceiling — {c.get('area', 'General')} ({c.get('type', '?')})",
                        "measure": "SF by ceiling type",
                        "method": c.get("method", "spray"),
                        "coats": c.get("coats", "2"),
                        "estimated_sf": c.get("estimated_sf", 0),
                        "source": "RCP / floor plans",
                        "task_code": code,
                    })

            # Doors
            doors = ip.get("doors", {})
            if isinstance(doors, dict) and doors.get("count"):
                matrix.append({
                    "scope": f"Doors — {doors.get('type', 'standard')}",
                    "measure": "Count by type (single/double, HM/wood)",
                    "method": "brush",
                    "coats": doors.get("coats", "2"),
                    "estimated_qty": doors["count"],
                    "unit": "EA",
                    "source": "door schedule / floor plans",
                    "task_code": "door_paint",
                })

            # Frames
            frames = ip.get("frames", {})
            if isinstance(frames, dict) and frames.get("count"):
                matrix.append({
                    "scope": f"Door Frames — {frames.get('type', 'HM')}",
                    "measure": "Count",
                    "method": "brush",
                    "coats": frames.get("coats", "2"),
                    "estimated_qty": frames["count"],
                    "unit": "EA",
                    "source": "door schedule",
                    "task_code": "door_frame",
                })

            # Trim
            trim = ip.get("trim", [])
            for t in trim:
                matrix.append({
                    "scope": f"Trim — {t.get('item', 'base')}",
                    "measure": "LF",
                    "method": "brush",
                    "coats": t.get("coats", "2"),
                    "estimated_lf": t.get("lf", 0),
                    "source": "floor plans (perimeter)",
                    "task_code": "trim_base",
                })

        # Exterior
        ep = sow.get("exterior_painting", {})
        if isinstance(ep, dict):
            for s in ep.get("surfaces", []):
                matrix.append({
                    "scope": f"Exterior — {s.get('area', 'walls')}",
                    "measure": "SF from elevations",
                    "method": s.get("method", "spray"),
                    "coats": s.get("coats", "2"),
                    "estimated_sf": s.get("estimated_sf", 0),
                    "source": "exterior elevations / site plan",
                    "task_code": "exterior_spray" if s.get("method") == "spray" else "exterior_roll",
                })

        # Wallcovering
        wc = sow.get("wallcovering", {})
        if isinstance(wc, dict):
            for loc in wc.get("locations", []):
                matrix.append({
                    "scope": f"Wallcovering — {loc.get('area', 'TBD')}",
                    "measure": "SF by location",
                    "method": "install",
                    "type": loc.get("type", "Type II vinyl"),
                    "estimated_sf": loc.get("estimated_sf", 0),
                    "source": "floor plans + finish schedule",
                    "task_code": "wallcovering",
                })

        # Prep
        prep = sow.get("prep_work", {})
        if isinstance(prep, dict):
            for p in prep.get("items", []):
                task = p.get("task", "prep")
                code = f"prep_{task}" if f"prep_{task}" in PREP_CODE_MAP else "prep_masking"
                matrix.append({
                    "scope": f"Prep — {task}",
                    "measure": "SF or LF",
                    "estimated_qty": p.get("estimated_sf_or_lf", 0),
                    "notes": p.get("notes", ""),
                    "source": "specs / SOW",
                    "task_code": code,
                })

        return matrix

    @staticmethod
    def _pick_wall_task_code(wall_item: dict) -> str:
        """Pick the right task code based on wall description."""
        method = (wall_item.get("method", "") or "").lower()
        coats = (wall_item.get("coats", "") or "").lower()
        desc = (wall_item.get("description", "") or "").lower()

        if "spray" in method and "back" in method:
            return "walls_spray_backroll"
        if "spray" in method:
            if "prime" in coats or "prime" in desc:
                return "walls_spray_prime"
            return "walls_spray_2coat"
        if "repaint" in desc or "existing" in desc:
            if "touch" in desc or "1 coat" in coats:
                return "walls_repaint_1coat"
            return "walls_repaint_2coat"
        if "new" in desc or "prime" in coats:
            return "walls_new_drywall_prime_2coat"
        return "walls_new_drywall_prime_2coat"

    # ==================================================================
    #  TOGAL TAKEOFF (stub with fallback)
    # ==================================================================

    def run_togal_takeoff(self, slug: str) -> dict:
        """
        Run Togal.AI takeoff via generalized pipeline. Falls back to SOW-based estimation.

        Uses togal_pipeline.py (generalized for any project):
          1. Authenticate with Togal API
          2. Find painting sheets in set
          3. Set scale, create views, run vectorization
          4. Extract room/wall/region measurements
          5. Convert to Carol's takeoff item format
          6. Save takeoff.json
        """
        proj_dir = PROJECTS_DIR / slug

        # Try new generalized Togal pipeline first
        try:
            sys.path.insert(0, str(SCRIPTS_DIR))
            from togal_pipeline import TogalPipeline

            proj = self.pipeline.get_project(slug) if self.pipeline else None

            # Check if project has a Togal set ID
            togal_set = (proj or {}).get("togal_set_id")
            if not togal_set:
                print("  No togal_set_id — checking for existing takeoff or falling back")
                # Check if takeoff already exists from manual run
                takeoff_file = proj_dir / "togal_takeoff.json"
                if takeoff_file.exists():
                    print("  Found existing togal_takeoff.json — extracting")
                else:
                    return self._sow_based_quantities(slug)

            pipeline = TogalPipeline(slug)

            # Check if takeoff already completed
            status = pipeline.get_status()
            if status.get("takeoff_complete"):
                print("  Togal takeoff already complete — re-extracting measurements")
                result = pipeline.run_extract_only()
            else:
                print(f"  Running Togal AI takeoff for {(proj or {}).get('name', slug)}...")
                result = pipeline.run_full()

            if result.get("status") == "complete":
                # Read saved takeoff and convert to estimator format
                takeoff_file = proj_dir / "togal_takeoff.json"
                if takeoff_file.exists():
                    togal_data = json.loads(takeoff_file.read_text(encoding="utf-8"))
                    takeoff_items = self._togal_measurements_to_items(togal_data)

                    takeoff = {
                        "source": "togal_ai_pipeline",
                        "pages_processed": togal_data.get("pages_measured", 0),
                        "grand_totals": togal_data.get("grand_totals", {}),
                        "items": takeoff_items,
                    }

                    takeoff_path = proj_dir / "takeoff.json"
                    takeoff_path.write_text(json.dumps(takeoff, indent=2, ensure_ascii=False),
                                            encoding="utf-8")

                    self.pipeline.update_stage(slug, "takeoff_done")
                    return takeoff

            if result.get("error"):
                print(f"  Togal pipeline error: {result['error']}")

        except ImportError:
            print("  togal_pipeline not available — falling back to SOW-based takeoff")
        except Exception as e:
            print(f"  Togal takeoff error: {e} — falling back to SOW-based takeoff")

        # Fallback: use SOW quantities
        return self._sow_based_quantities(slug)

    def _togal_measurements_to_items(self, togal_data: dict) -> list:
        """Convert togal_takeoff.json measurements into estimator line items.

        togal_pipeline.save_results() writes:
          "takeoff": {floor_sf, room_count, rooms, and either
                      wall_sf/wall_lf (user classifications) or
                      perimeter_lf (AI-detected geojson)}
          "raw_grand_totals": {rooms, room_sf, walls, room_perim_lf}
                      (AI-detected source only — pre-filter fallback)

        Raises ValueError if no usable quantities are found — callers must
        NOT proceed to an estimate from an empty takeoff.
        """
        items = []
        takeoff = togal_data.get("takeoff") or {}
        source = togal_data.get("source", "togal")

        floor_sf = takeoff.get("floor_sf", 0) or 0
        wall_sf = takeoff.get("wall_sf", 0) or 0
        perim_lf = (takeoff.get("perimeter_lf", 0) or takeoff.get("wall_lf", 0) or 0)
        room_count = takeoff.get("room_count", 0) or 0

        # Fallback: cleaned takeoff empty — use raw grand totals (pre-filter)
        if floor_sf <= 0 and wall_sf <= 0 and perim_lf <= 0:
            raw = togal_data.get("raw_grand_totals") or {}
            floor_sf = raw.get("room_sf", 0) or 0
            perim_lf = raw.get("room_perim_lf", 0) or 0
            room_count = raw.get("rooms", 0) or room_count
            if floor_sf > 0 or perim_lf > 0:
                source = f"{source} (raw_grand_totals fallback)"

        # Walls: prefer measured wall SF, else perimeter x 10ft default height
        if wall_sf > 0:
            items.append({
                "area": "Interior Walls (Togal measured)",
                "task_code": "walls_new_drywall_prime_2coat",
                "quantity": round(wall_sf, 0),
                "unit": "SF",
                "method": "B&R",
                "coats": "1P+2F",
                "notes": f"Togal {source}: measured wall SF, {room_count} rooms",
            })
        elif perim_lf > 0:
            wall_calc_sf = round(perim_lf * 10, 0)  # 10ft default height
            items.append({
                "area": "Interior Walls (perimeter-derived)",
                "task_code": "walls_new_drywall_prime_2coat",
                "quantity": wall_calc_sf,
                "unit": "SF",
                "method": "B&R",
                "coats": "1P+2F",
                "notes": f"Togal {source}: {perim_lf:.0f} LF perim x 10ft, {room_count} rooms",
            })

        # Ceilings: floor-plan SF approximates ceiling SF
        if floor_sf > 0:
            items.append({
                "area": "Ceilings (floor-plan SF)",
                "task_code": "ceiling_drywall_spray",
                "quantity": round(floor_sf, 0),
                "unit": "SF",
                "method": "spray",
                "coats": "1P+2F",
                "notes": f"Togal {source}: {room_count} rooms floor SF — verify ceiling scope",
            })

        total_qty = sum(i.get("quantity", 0) or 0 for i in items)
        if not items or total_qty <= 0:
            raise ValueError(
                "Togal takeoff produced no usable quantities "
                f"(source: {togal_data.get('source', '?')}, "
                f"takeoff keys: {sorted(takeoff.keys())})"
            )

        return items

    def _sow_based_quantities(self, slug: str) -> dict:
        """Generate takeoff quantities from SOW estimates (no Togal)."""
        proj_dir = PROJECTS_DIR / slug
        sow_path = proj_dir / "sow.json"
        plan_path = proj_dir / "takeoff_plan.json"

        sow = {}
        if sow_path.exists():
            sow = json.loads(sow_path.read_text(encoding="utf-8"))

        plan = {}
        if plan_path.exists():
            plan = json.loads(plan_path.read_text(encoding="utf-8"))

        # Build takeoff items from measurement matrix
        takeoff_items = []
        for item in plan.get("measurement_matrix", []):
            qty = (item.get("estimated_sf") or item.get("estimated_qty")
                   or item.get("estimated_lf") or 0)

            unit = "SF"
            if "door" in item.get("scope", "").lower() or "frame" in item.get("scope", "").lower():
                unit = "EA"
            elif "trim" in item.get("scope", "").lower() or "caulk" in item.get("scope", "").lower():
                unit = "LF"

            task_code = item.get("task_code", "")
            takeoff_items.append({
                "area": item.get("scope", "Unknown"),
                "task_code": task_code,
                "quantity": qty,
                "unit": unit,
                "method": item.get("method", "TBD"),
                "coats": item.get("coats", "2"),
                "notes": item.get("notes", "SOW estimate — verify with takeoff"),
            })

        # Also check if bid had size_sf for a sanity check
        bid = self.find_bid_for_slug(slug)
        plan_sf = bid.get("size_sf", 0) if bid else 0

        result = {
            "source": "sow_estimate",
            "items": takeoff_items,
            "plan_sf": plan_sf,
            "note": "Quantities estimated from SOW — verify with actual takeoff for accuracy",
        }

        takeoff_path = proj_dir / "takeoff.json"
        takeoff_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

        return result

    # ==================================================================
    #  FACILITY TYPE DETECTION
    # ==================================================================

    def _detect_facility_type(self, slug: str, sow: dict = None) -> str | None:
        """Classify project into one of the 9 facility types using keyword matching
        against project name, SOW scope text, and bid description."""
        # Keywords mapped to facility type keys (order matters — more specific first)
        FACILITY_KEYWORDS = {
            "k12_gym": [
                "gymnasium", "gym ", " gym", "gymnas", "athletic", "fieldhouse",
                "field house", "recreation center", "rec center",
            ],
            "k12_school": [
                "school", "elementary", "middle school", "high school",
                "academy", "charter", "k-12", "k12", "classroom",
                "kindercare", "kinder care", "childcare", "child care",
                "daycare", "day care", "montessori", "preschool", "pre-school",
                "education", "learning center",
            ],
            "medical": [
                "medical", "hospital", "clinic", "dental", "urgent care",
                "healthcare", "health care", "outpatient", "surgery center",
                "veterinary", "vet clinic", "optometry", "ophthalmol",
                "orthop", "pediatric", "oncology", "pharmacy",
            ],
            "restaurant": [
                "restaurant", "kitchen", "food service", "cafeteria",
                "cafe", "bistro", "grill", "eatery", "dining",
                "chick-fil-a", "chickfila", "mcdonald", "wendy",
                "starbucks", "chipotle", "panera", "subway",
            ],
            "grocery": [
                "grocery", "supermarket", "market", "food lion",
                "harris teeter", "publix", "aldi", "lidl", "trader joe",
                "whole foods", "kroger", "piggly", "ingles",
            ],
            "warehouse": [
                "warehouse", "distribution", "fulfillment", "industrial",
                "manufacturing", "factory", "plant", "logistics",
                "storage facility", "cold storage",
            ],
            "multifamily": [
                "apartment", "multifamily", "multi-family", "condo",
                "townhome", "townhouse", "residential", "senior living",
                "assisted living", "housing",
            ],
            "retail": [
                "retail", "store", "shop", "mall", "plaza",
                "boutique", "showroom", "salon", "barbershop",
                "dollar general", "dollar tree", "target", "walmart",
                "autozone", "o'reilly", "advance auto",
            ],
            "office": [
                "office", "professional", "corporate", "coworking",
                "co-working", "business center", "bank", "credit union",
                "insurance", "law firm", "accounting",
            ],
        }

        # Build a text blob from all available project info
        text_parts = [slug.replace("_", " ").replace("-", " ")]

        # From SOW
        if sow:
            text_parts.append(sow.get("project_name", ""))
            text_parts.append(sow.get("scope_summary", ""))
            text_parts.append(sow.get("building_type", ""))
            text_parts.append(sow.get("facility_type", ""))
            for area in sow.get("areas", []):
                text_parts.append(area.get("name", ""))

        # From bid data in pipeline
        bid = self.find_bid_for_slug(slug)
        if bid:
            text_parts.append(bid.get("name", ""))
            text_parts.append(bid.get("description", ""))
            text_parts.append(bid.get("gc", ""))

        search_text = " ".join(str(p) for p in text_parts if p).lower()

        if not search_text.strip():
            return None

        # Score each type by keyword hits
        scores = {}
        for ftype, keywords in FACILITY_KEYWORDS.items():
            score = sum(1 for kw in keywords if kw.lower() in search_text)
            if score > 0:
                scores[ftype] = score

        if not scores:
            return None

        # Return highest scoring type
        return max(scores, key=scores.get)

    # ==================================================================
    #  BUILD ESTIMATE
    # ==================================================================

    def build_estimate(self, slug: str, measurements: dict = None,
                       pricing_tier: str = "TARGET") -> dict:
        """
        Apply CCF production rates to measurements/SOW.
        Full 5-step bid formula:
          1. Labor cost = qty / prod_rate * burdened_rate
          2. Material cost = qty * material_cost_per_unit * coats
          3. Direct cost = labor + material + equipment
          4. Overhead = direct * overhead_pct
          5. Bid price = (direct + overhead) * (1 + markup)
        """
        proj_dir = PROJECTS_DIR / slug
        self.pipeline.update_stage(slug, "estimating")

        # Load SOW (optional if takeoff already exists)
        sow_path = proj_dir / "sow.json"
        sow = {}
        if sow_path.exists():
            sow = json.loads(sow_path.read_text(encoding="utf-8"))

        # Load or generate takeoff
        takeoff_path = proj_dir / "takeoff.json"
        if measurements:
            takeoff_data = {"source": "user_provided", "items": measurements.get("items", [])}
        elif takeoff_path.exists():
            takeoff_data = json.loads(takeoff_path.read_text(encoding="utf-8"))
        elif sow:
            takeoff_data = self._sow_based_quantities(slug)
        else:
            return {"error": "No SOW or takeoff found. Run build_sow or accept_takeoff first."}

        # Determine rates based on tier
        speed = self._speed_for_tier(pricing_tier)
        tier_pcts = self._get_tier_percentages(pricing_tier)
        labor_rate = self._get_labor_rate("experienced")

        # Build line items
        line_items = []
        total_labor_hours = 0.0
        total_material = 0.0
        total_equipment = 0.0

        for item in takeoff_data.get("items", []):
            qty = item.get("quantity", 0)
            if qty <= 0:
                continue

            task_code = item.get("task_code", "")
            unit = item.get("unit", "SF")
            coats_str = item.get("coats", "2")
            coats = self._parse_coat_count(coats_str)
            method = item.get("method", "")

            # Calculate labor hours
            labor_hrs = self._calc_labor_hours(task_code, qty, unit, coats, speed)

            # Calculate material cost
            mat_cost = self._calc_material_cost(task_code, qty, unit, coats)

            # Equipment cost (for specialty items)
            equip_cost = item.get("equipment_cost", 0)

            labor_cost = round(labor_hrs * labor_rate, 2)

            line_item = {
                "area": item.get("area", ""),
                "task_code": task_code,
                "quantity": qty,
                "unit": unit,
                "method": method,
                "coats": coats_str,
                "prod_rate": self._get_effective_rate(task_code, speed),
                "labor_hours": round(labor_hrs, 2),
                "labor_cost": labor_cost,
                "material_cost": round(mat_cost, 2),
                "equipment_cost": round(equip_cost, 2),
                "line_total": round(labor_cost + mat_cost + equip_cost, 2),
            }
            line_items.append(line_item)

            total_labor_hours += labor_hrs
            total_material += mat_cost
            total_equipment += equip_cost

        # Handle wallcovering separately (priced per SF installed, not production rate)
        # Only add if not already in takeoff items
        has_wc = any(i.get("task_code") == "wallcovering" for i in takeoff_data.get("items", []))
        if not has_wc:
            wc_items = self._calc_wallcovering(sow, pricing_tier)
            for wc in wc_items:
                line_items.append(wc)
                total_labor_hours += wc.get("labor_hours", 0)
                total_material += wc.get("material_cost", 0)

        # Calculate totals using bid formula
        total_labor_cost = round(total_labor_hours * labor_rate, 2)
        direct_cost = round(total_labor_cost + total_material + total_equipment, 2)
        overhead = round(direct_cost * tier_pcts["overhead_pct"], 2)
        subtotal = direct_cost + overhead
        profit = round(subtotal * tier_pcts["profit_pct"], 2)
        total_bid = round(subtotal + profit, 2)

        # Calculate total SF for blended rate
        total_sf = sum(
            i["quantity"] for i in line_items
            if i.get("unit") == "SF" and "prep" not in i.get("task_code", "")
        )

        # Also try from SOW summary
        summary = sow.get("summary", {})
        if isinstance(summary, dict):
            sow_sf = (
                (summary.get("total_sf_walls") or 0)
                + (summary.get("total_sf_ceilings") or 0)
                + (summary.get("total_sf_exterior") or 0)
                + (summary.get("total_sf_wallcovering") or 0)
            )
            if sow_sf > total_sf:
                total_sf = sow_sf

        # Check bid size_sf as fallback
        bid = self.find_bid_for_slug(slug)
        if bid and bid.get("size_sf"):
            try:
                bid_sf = float(str(bid["size_sf"]).replace(",", ""))
                if bid_sf > total_sf:
                    total_sf = bid_sf
            except (ValueError, TypeError):
                pass

        blended = round(total_bid / total_sf, 2) if total_sf > 0 else 0

        # Detect facility type and add calibration
        facility_type = self._detect_facility_type(slug, sow)
        calibration = None
        if facility_type:
            try:
                sys.path.insert(0, str(SKILL_SCRIPTS))
                from estimate_engine import calibrate_from_history
                proj_data = self.pipeline.get_project(slug) if self.pipeline else {}
                calibration = calibrate_from_history(
                    {"metrics": {"blended_rate_per_sf": blended}, "totals": {"bid_price": total_bid}},
                    facility_type=facility_type,
                    gc_name=(proj_data or {}).get("gc", ""),
                )
            except Exception:
                pass

        estimate = {
            "project": slug,
            "pricing_tier": pricing_tier,
            "labor_rate_burdened": labor_rate,
            "facility_type": facility_type,
            "line_items": line_items,
            "summary": {
                "total_sf": total_sf,
                "labor_hours": round(total_labor_hours, 1),
                "labor_cost": total_labor_cost,
                "material_cost": round(total_material, 2),
                "equipment_cost": round(total_equipment, 2),
                "direct_cost": direct_cost,
                "overhead_pct": tier_pcts["overhead_pct"],
                "overhead": overhead,
                "profit_pct": tier_pcts["profit_pct"],
                "profit": profit,
                "total_bid": total_bid,
                "blended_per_sf": blended,
                "pricing_tier": pricing_tier,
                "line_item_count": len(line_items),
            },
            "calibration": calibration,
            "created_at": datetime.now().isoformat(),
            "takeoff_source": takeoff_data.get("source", "unknown"),
        }

        # Save estimate.json
        est_path = proj_dir / "estimate.json"
        est_path.write_text(json.dumps(estimate, indent=2, ensure_ascii=False), encoding="utf-8")

        # Save estimate_summary.md
        self._save_estimate_summary_md(proj_dir, estimate, sow)

        self.pipeline.update_stage(slug, "estimate_ready")
        return estimate

    def _calc_labor_hours(self, task_code: str, qty: float, unit: str,
                          coats: int, speed: str) -> float:
        """Calculate labor hours for a line item."""
        # Map task_code to production rate task name
        task_name = TASK_CODE_MAP.get(task_code) or PREP_CODE_MAP.get(task_code)
        if not task_name:
            # Fallback: rough estimate at 150 SF/hr
            if unit == "SF":
                return (qty * coats) / 150.0
            elif unit == "LF":
                return (qty * coats) / 65.0
            elif unit == "EA":
                return qty * 0.5 * coats
            return 0

        rate = self._get_prod_rate(task_name, speed)

        # Handle per-unit rates (doors, frames, windows use hrs/unit)
        if task_code in ("door_paint", "door_frame", "window_frame"):
            # These rates are already hrs/unit — rate IS hrs per unit
            # For doors: slow=0.75, avg=0.5, fast=0.33 hrs/side
            # doors get 2 sides
            sides = 2 if task_code == "door_paint" else 1
            return qty * rate * sides

        # Standard SF/hr or LF/hr rates
        if rate <= 0:
            rate = 150
        if unit in ("SF", "LF"):
            return (qty * coats) / rate
        elif unit == "EA":
            return qty * coats / rate

        return 0

    def _get_effective_rate(self, task_code: str, speed: str) -> float:
        """Get the effective production rate for display."""
        task_name = TASK_CODE_MAP.get(task_code) or PREP_CODE_MAP.get(task_code)
        if task_name:
            return self._get_prod_rate(task_name, speed)
        return 150

    def _calc_material_cost(self, task_code: str, qty: float, unit: str,
                            coats: int) -> float:
        """Calculate material cost for a line item."""
        # Doors and frames: per-unit material cost
        if task_code in ("door_paint", "door_frame"):
            per_unit = MATERIAL_PER_UNIT.get("door_single_hm", 8.50)
            return qty * per_unit

        # Prep items: minimal material cost
        if task_code.startswith("prep_"):
            if task_code == "prep_caulking":
                # ~$0.10/LF for caulk
                return qty * 0.10
            elif task_code == "prep_spot_prime":
                return qty * MATERIAL_COSTS.get("primer_standard", 0.10)
            return 0  # masking, sanding, cleaning — negligible material

        # Wallcovering: handled separately
        if task_code == "wallcovering":
            return 0

        # Standard painting: $/SF/coat
        if unit == "SF":
            # Pick material cost by type
            if "epoxy" in task_code:
                mat_per_sf = MATERIAL_COSTS["epoxy_macropoxy"]
            elif "stain" in task_code:
                mat_per_sf = MATERIAL_COSTS["stain"]
            elif "texture" in task_code:
                mat_per_sf = MATERIAL_COSTS["texture"]
            elif "exterior" in task_code:
                mat_per_sf = MATERIAL_COSTS["dtm_acrylic"]
            elif "prime" in task_code:
                mat_per_sf = MATERIAL_COSTS["primer_standard"]
            elif "trim" in task_code or "ceiling" in task_code:
                mat_per_sf = MATERIAL_COSTS["semi_gloss_trim"]
            else:
                mat_per_sf = MATERIAL_COSTS["latex_standard"]

            return qty * mat_per_sf * coats

        elif unit == "LF":
            # Trim: minimal material per LF
            return qty * 0.05 * coats

        return 0

    def _calc_wallcovering(self, sow: dict, tier: str) -> list:
        """Calculate wallcovering costs from SOW."""
        wc = sow.get("wallcovering", {})
        if not isinstance(wc, dict):
            return []

        locations = wc.get("locations", [])
        if not locations:
            return []

        items = []
        for loc in locations:
            sf = loc.get("estimated_sf", 0)
            if sf <= 0:
                continue

            wc_type = (loc.get("type", "") or "").lower()

            # Labor rate per SF from pricing config
            if "sisal" in wc_type or "acoustical" in wc_type:
                labor_per_sf = 3.00  # market rate for specialty
                mat_per_sf = 4.00
            else:
                # Standard Type II vinyl
                labor_per_sf = 2.50  # market rate
                mat_per_sf = 2.00    # material cost

            # Tier adjustments
            if tier == "FLOOR":
                labor_per_sf *= 0.75
            elif tier == "PREMIUM":
                labor_per_sf *= 1.25

            labor_cost = sf * labor_per_sf
            labor_hours = labor_cost / self._get_labor_rate("experienced")
            material_cost = sf * mat_per_sf

            items.append({
                "area": f"Wallcovering — {loc.get('area', 'TBD')}",
                "task_code": "wallcovering",
                "quantity": sf,
                "unit": "SF",
                "method": "install",
                "coats": "N/A",
                "prod_rate": 0,
                "labor_hours": round(labor_hours, 2),
                "labor_cost": round(labor_cost, 2),
                "material_cost": round(material_cost, 2),
                "equipment_cost": 0,
                "line_total": round(labor_cost + material_cost, 2),
                "notes": f"Wallcovering {loc.get('type', 'Type II')} @ ${labor_per_sf:.2f}/SF labor + ${mat_per_sf:.2f}/SF material",
            })

        return items

    @staticmethod
    def _parse_coat_count(coats_str: str) -> int:
        """Parse coat string like '1P+2F', '2', 'N/A' into total coat count."""
        if not coats_str or coats_str == "N/A":
            return 1
        s = str(coats_str).lower().strip()

        # "1p+2f" → 3 total coats (but we use as multiplier for productivity)
        # For production rates, we typically price per-coat already
        # So "1P+2F" means the prod rate accounts for the full system
        if "p" in s and "f" in s:
            # Prime + finish system — prod rate already accounts for this
            return 1  # the rate is for the whole system

        # Simple number
        nums = re.findall(r'\d+', s)
        if nums:
            return int(nums[0])
        return 1

    # ==================================================================
    #  ESTIMATE SUMMARY (human readable)
    # ==================================================================

    def _generate_estimate_summary(self, slug: str, estimate: dict) -> str:
        """Generate formatted estimate text for WhatsApp/console."""
        s = estimate.get("summary", {})
        proj = self.pipeline.get_project(slug) if self.pipeline else {}
        proj_name = proj.get("name", slug) if proj else slug

        lines = [
            f"ESTIMATE: {proj_name}",
            "=" * 50,
            f"Pricing tier: {s.get('pricing_tier', '?')}",
            f"Labor rate: ${estimate.get('labor_rate_burdened', 28):.2f}/hr (burdened)",
            "",
            "LINE ITEMS:",
            "-" * 50,
        ]

        for item in estimate.get("line_items", []):
            qty = item.get("quantity", 0)
            unit = item.get("unit", "SF")
            hrs = item.get("labor_hours", 0)
            total = item.get("line_total", 0)
            lines.append(
                f"  {item.get('area', '?')}: "
                f"{qty:,.0f} {unit} | {hrs:.1f} hrs | ${total:,.0f}"
            )

        lines.extend([
            "",
            "-" * 50,
            f"Labor:     {s.get('labor_hours', 0):,.1f} hrs = ${s.get('labor_cost', 0):,.0f}",
            f"Material:  ${s.get('material_cost', 0):,.0f}",
        ])

        if s.get("equipment_cost", 0) > 0:
            lines.append(f"Equipment: ${s['equipment_cost']:,.0f}")

        lines.extend([
            f"Direct:    ${s.get('direct_cost', 0):,.0f}",
            f"OH ({s.get('overhead_pct', 0):.0%}):    ${s.get('overhead', 0):,.0f}",
            f"Profit ({s.get('profit_pct', 0):.0%}): ${s.get('profit', 0):,.0f}",
            "=" * 50,
            f"TOTAL BID: ${s.get('total_bid', 0):,.0f}",
            f"Blended:   ${s.get('blended_per_sf', 0):.2f}/SF",
            "",
            f"Takeoff source: {estimate.get('takeoff_source', '?')}",
        ])

        if estimate.get("takeoff_source") == "sow_estimate":
            lines.append("NOTE: Quantities from SOW estimate — verify with actual takeoff.")

        # Pricing intelligence warnings
        gc_name = proj.get("gc", "") if proj else ""
        facility_type = proj.get("facility_type", "") if proj else ""
        warnings = self.get_pricing_intelligence(gc_name, facility_type, s.get("total_bid", 0))
        if warnings:
            lines.append("\nPRICING INTELLIGENCE:")
            for w in warnings:
                lines.append(f"  * {w}")

        lines.append(f"\nStage: estimate_ready")
        lines.append(f"Next: \"draft proposal {proj_name}\" to generate proposal")

        return "\n".join(lines)

    def _save_estimate_summary_md(self, proj_dir: Path, estimate: dict, sow: dict):
        """Save estimate_summary.md for human review."""
        s = estimate.get("summary", {})
        proj_name = estimate.get("project", "Unknown")

        md = [
            f"# Estimate: {proj_name}",
            f"**Generated:** {estimate.get('created_at', '')}",
            f"**Pricing Tier:** {s.get('pricing_tier', '?')}",
            f"**Labor Rate:** ${estimate.get('labor_rate_burdened', 28):.2f}/hr (burdened)",
            "",
            "## Line Items",
            "",
            "| Area | Qty | Unit | Hrs | Labor | Material | Total |",
            "|------|-----|------|-----|-------|----------|-------|",
        ]

        for item in estimate.get("line_items", []):
            md.append(
                f"| {item.get('area', '')} "
                f"| {item.get('quantity', 0):,.0f} "
                f"| {item.get('unit', 'SF')} "
                f"| {item.get('labor_hours', 0):.1f} "
                f"| ${item.get('labor_cost', 0):,.0f} "
                f"| ${item.get('material_cost', 0):,.0f} "
                f"| ${item.get('line_total', 0):,.0f} |"
            )

        md.extend([
            "",
            "## Summary",
            "",
            f"| | |",
            f"|---|---|",
            f"| **Labor** | {s.get('labor_hours', 0):,.1f} hrs = ${s.get('labor_cost', 0):,.0f} |",
            f"| **Material** | ${s.get('material_cost', 0):,.0f} |",
            f"| **Direct Cost** | ${s.get('direct_cost', 0):,.0f} |",
            f"| **Overhead ({s.get('overhead_pct', 0):.0%})** | ${s.get('overhead', 0):,.0f} |",
            f"| **Profit ({s.get('profit_pct', 0):.0%})** | ${s.get('profit', 0):,.0f} |",
            f"| **TOTAL BID** | **${s.get('total_bid', 0):,.0f}** |",
            f"| **Blended Rate** | ${s.get('blended_per_sf', 0):.2f}/SF |",
        ])

        # Special conditions from SOW
        sc = sow.get("special_conditions", {})
        if isinstance(sc, dict):
            flags = []
            if sc.get("prevailing_wage"): flags.append("Prevailing Wage")
            if sc.get("night_work"): flags.append("Night Work")
            if sc.get("phased"): flags.append("Phased")
            if flags:
                md.extend(["", f"**Special Conditions:** {', '.join(flags)}"])

        if estimate.get("takeoff_source") == "sow_estimate":
            md.extend([
                "",
                "> **Note:** Quantities estimated from SOW. Verify with actual takeoff measurements.",
            ])

        md_path = proj_dir / "estimate_summary.md"
        md_path.write_text("\n".join(md), encoding="utf-8")

    # ==================================================================
    #  GET ESTIMATE (formatted output)
    # ==================================================================

    def get_estimate(self, slug: str) -> str:
        """Return formatted estimate summary for a project."""
        proj_dir = PROJECTS_DIR / slug
        est_path = proj_dir / "estimate.json"

        if not est_path.exists():
            return f"No estimate found for {slug}. Need to run the estimation pipeline first."

        estimate = json.loads(est_path.read_text(encoding="utf-8"))
        return self._generate_estimate_summary(slug, estimate)

    # ==================================================================
    #  ACCEPT TAKEOFF INPUT
    # ==================================================================

    def accept_takeoff(self, slug: str, takeoff_data: dict) -> str:
        """
        Accept takeoff quantities from user (CSV, pasted text, or structured dict).
        Maps each item to task codes and production rates.
        """
        proj_dir = PROJECTS_DIR / slug
        proj_dir.mkdir(parents=True, exist_ok=True)

        items = takeoff_data.get("items", [])
        mapped_items = []

        for item in items:
            task_code = item.get("task_code", "")
            if not task_code:
                task_code = self._auto_assign_task_code(item)
                item["task_code"] = task_code

            # Ensure material cost is set
            if "material_cost_per_sf" not in item and "material_cost_flat" not in item:
                item["material_cost_per_sf"] = self._default_material_cost(task_code)

            mapped_items.append(item)

        result = {
            "source": "user_provided",
            "items": mapped_items,
            "accepted_at": datetime.now().isoformat(),
        }

        takeoff_path = proj_dir / "takeoff.json"
        takeoff_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

        self.pipeline.update_stage(slug, "takeoff_done")

        return (
            f"Takeoff accepted: {len(mapped_items)} line items.\n"
            f"Say \"continue\" to build the estimate."
        )

    def _auto_assign_task_code(self, item: dict) -> str:
        """Auto-assign a task code based on item description."""
        area = (item.get("area", "") or item.get("task", "")).lower()
        method = (item.get("method", "")).lower()

        if "wallcovering" in area or "wallpaper" in area:
            return "wallcovering"
        if "door" in area and "frame" not in area:
            return "door_paint"
        if "frame" in area:
            return "door_frame"
        if "window" in area:
            return "window_frame"
        if "trim" in area or "base" in area or "chair" in area or "crown" in area:
            return "trim_base"
        if "ceiling" in area or "rcp" in area:
            if "act" in area or "grid" in area:
                return "ceiling_act_spray"
            if "spray" in method:
                return "ceiling_drywall_spray"
            return "ceiling_drywall_roll"
        if "exterior" in area or "outside" in area:
            if "wash" in area or "pressure" in area:
                return "exterior_pressure_wash"
            if "spray" in method:
                return "exterior_spray"
            return "exterior_roll"
        if "epoxy" in area:
            return "epoxy_floor"
        if "stain" in area:
            return "stain_wood"
        if "texture" in area:
            return "texture_knockdown"
        if "mask" in area or "protect" in area:
            return "prep_masking"
        if "caulk" in area:
            return "prep_caulking"
        if "sand" in area:
            return "prep_sanding_light"
        if "prime" in area or "primer" in area:
            return "prep_spot_prime"

        # Default: wall painting
        if "spray" in method and "back" in method:
            return "walls_spray_backroll"
        if "spray" in method:
            return "walls_spray_2coat"
        return "walls_new_drywall_prime_2coat"

    @staticmethod
    def _default_material_cost(task_code: str) -> float:
        """Default material cost per SF for a task code."""
        costs = {
            "walls_new_drywall_prime_2coat": 0.12,
            "walls_repaint_2coat":           0.12,
            "walls_repaint_1coat":           0.06,
            "walls_spray_prime":             0.10,
            "walls_spray_2coat":             0.12,
            "walls_spray_large":             0.12,
            "walls_spray_backroll":          0.12,
            "ceiling_act_spray":             0.10,
            "ceiling_drywall_roll":          0.12,
            "ceiling_drywall_spray":         0.12,
            "exterior_spray":                0.15,
            "exterior_roll":                 0.15,
            "epoxy_floor":                   0.25,
            "stain_wood":                    0.14,
            "texture_knockdown":             0.08,
        }
        return costs.get(task_code, 0.12)

    # ==================================================================
    #  QUICK ESTIMATE (unit-price based, no SOW needed)
    # ==================================================================

    def quick_estimate(self, slug: str, total_sf: float,
                       pricing_tier: str = "TARGET",
                       facility_type: str = "commercial") -> dict:
        """
        Quick estimate using unit prices (no SOW/takeoff needed).
        Good for initial budgeting before docs are available.
        """
        tier_pcts = self._get_tier_percentages(pricing_tier)

        # Typical commercial interior breakdown
        wall_sf = total_sf * 0.65
        ceiling_sf = total_sf * 0.25
        trim_lf = total_sf * 0.05  # rough LF estimate
        doors = max(1, int(total_sf / 500))  # ~1 door per 500 SF

        # Get unit prices
        wall_price = self._get_unit_price("Walls — 2 coats, brush & roll", pricing_tier)
        ceil_price = self._get_unit_price("Ceilings — spray (2 coats)", pricing_tier)
        trim_price = self._get_unit_price("Trim / base / chair rail", pricing_tier)
        door_price = self._get_unit_price("Doors — complete (2 sides + frame)", pricing_tier)

        # Use midpoint of range
        wall_mid = (wall_price[0] + wall_price[1]) / 2 if wall_price[1] > 0 else 0.65
        ceil_mid = (ceil_price[0] + ceil_price[1]) / 2 if ceil_price[1] > 0 else 0.48
        trim_mid = (trim_price[0] + trim_price[1]) / 2 if trim_price[1] > 0 else 1.25
        door_mid = (door_price[0] + door_price[1]) / 2 if door_price[1] > 0 else 75.0

        # Calculate
        wall_cost = wall_sf * wall_mid
        ceil_cost = ceiling_sf * ceil_mid
        trim_cost = trim_lf * trim_mid
        door_cost = doors * door_mid
        direct = wall_cost + ceil_cost + trim_cost + door_cost

        overhead = direct * tier_pcts["overhead_pct"]
        profit = (direct + overhead) * tier_pcts["profit_pct"]
        total = direct + overhead + profit

        return {
            "type": "quick_estimate",
            "total_sf": total_sf,
            "pricing_tier": pricing_tier,
            "breakdown": {
                "walls": {"sf": wall_sf, "unit_price": wall_mid, "cost": round(wall_cost)},
                "ceilings": {"sf": ceiling_sf, "unit_price": ceil_mid, "cost": round(ceil_cost)},
                "trim": {"lf": trim_lf, "unit_price": trim_mid, "cost": round(trim_cost)},
                "doors": {"count": doors, "unit_price": door_mid, "cost": round(door_cost)},
            },
            "direct_cost": round(direct),
            "overhead": round(overhead),
            "profit": round(profit),
            "total_bid": round(total),
            "blended_per_sf": round(total / total_sf, 2) if total_sf else 0,
            "note": "Quick budget estimate — run full pipeline for accurate bid",
        }

    # ==================================================================
    #  HELPERS
    # ==================================================================

    def find_bid(self, project_name: str) -> dict | None:
        """Fuzzy match project name against active_bids.json."""
        from difflib import get_close_matches
        if not BIDS_FILE.exists():
            return None
        bids = json.loads(BIDS_FILE.read_text(encoding="utf-8"))
        names = [b["project_name"] for b in bids]
        matches = get_close_matches(
            project_name.lower(),
            [n.lower() for n in names],
            n=1, cutoff=0.4,
        )
        if matches:
            for b in bids:
                if b["project_name"].lower() == matches[0]:
                    return b
        return None

    def find_bid_for_slug(self, slug: str) -> dict | None:
        """Find a bid whose slugified name matches."""
        if not BIDS_FILE.exists():
            return None
        bids = json.loads(BIDS_FILE.read_text(encoding="utf-8"))
        for b in bids:
            if self.make_slug(b["project_name"]) == slug:
                return b
        return None

    @staticmethod
    def make_slug(project_name: str) -> str:
        slug = project_name.lower()
        slug = re.sub(r'[^a-z0-9\s]', ' ', slug)
        slug = re.sub(r'\s+', '_', slug.strip())
        return slug[:80]
