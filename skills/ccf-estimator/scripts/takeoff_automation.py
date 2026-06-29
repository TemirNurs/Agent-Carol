#!/usr/bin/env python3
"""
CCF Takeoff Automation Provider Interface
Defines the interface for automated takeoff tools. Currently uses ManualProvider (user uploads).
Ready to plug in Kreo, Togal, or STACK APIs when activated.

Usage:
  python takeoff_automation.py --provider manual --status
  python takeoff_automation.py --provider kreo --upload --plans plan1.pdf plan2.pdf --project my-project
  python takeoff_automation.py --provider kreo --results --project my-project
"""

import argparse
import json
import sys
from pathlib import Path
from abc import ABC, abstractmethod

CONFIG_FILE = Path(__file__).resolve().parent.parent.parent.parent / "data" / "config" / "takeoff_config.json"


def _load_config():
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {"active_provider": "manual", "providers": {}}


class TakeoffProvider(ABC):
    """Base interface for takeoff automation providers."""

    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def status(self) -> dict: ...

    @abstractmethod
    def upload_plans(self, pdf_paths: list, project_id: str) -> dict: ...

    @abstractmethod
    def get_measurements(self, project_id: str) -> dict: ...

    @abstractmethod
    def get_progress(self, project_id: str) -> dict: ...


class ManualProvider(TakeoffProvider):
    """Current provider: user measures and uploads takeoff data."""

    def name(self): return "manual"

    def status(self):
        return {
            "provider": "manual",
            "active": True,
            "description": "User measures manually using STACK or on-site, then uploads CSV/Excel takeoff data.",
            "instructions": "Upload takeoff file (CSV or XLSX) with columns: area, task, quantity, unit, method, coats",
        }

    def upload_plans(self, pdf_paths, project_id):
        return {
            "provider": "manual",
            "message": "Plans saved. Please measure the following and upload a CSV/Excel takeoff file.",
            "plans_stored": [str(p) for p in pdf_paths],
        }

    def get_measurements(self, project_id):
        return {
            "provider": "manual",
            "status": "waiting_for_user",
            "message": "Waiting for user to upload takeoff data. Ask the user to measure and provide quantities.",
        }

    def get_progress(self, project_id):
        return {"provider": "manual", "status": "waiting_for_user", "progress": 0}


class KreoProvider(TakeoffProvider):
    """Kreo Core Platform API — AI-powered automated takeoff.
    Upload plans → get back measurements in JSON. ~98.5% accuracy.
    API docs: https://www.kreo.net/news-2d-takeoff/core-platform-api
    """

    def name(self): return "kreo"

    def status(self):
        config = _load_config()
        kreo_config = config.get("providers", {}).get("kreo", {})
        return {
            "provider": "kreo",
            "configured": bool(kreo_config.get("api_key")),
            "description": "Kreo AI takeoff — upload plans, get measurements in JSON automatically.",
            "accuracy": "~98.5%",
            "setup": "Add kreo.api_key to data/config/takeoff_config.json",
        }

    def upload_plans(self, pdf_paths, project_id):
        config = _load_config()
        api_key = config.get("providers", {}).get("kreo", {}).get("api_key")
        if not api_key:
            return {"error": "Kreo API key not configured", "setup": "Add to takeoff_config.json"}

        # TODO: Implement Kreo API upload
        # POST /upload with multipart form data
        # POST /project to create project with uploaded files
        return {"status": "not_implemented", "message": "Kreo integration ready for activation"}

    def get_measurements(self, project_id):
        return {"status": "not_implemented"}

    def get_progress(self, project_id):
        return {"status": "not_implemented"}


class TogalProvider(TakeoffProvider):
    """Togal.AI — Automated takeoff via REST API.
    Uses togal_pipeline.py for the actual API calls.
    Proven on Hopewell Elementary Phase 2 Gym.
    """

    def name(self): return "togal"

    def _get_pipeline(self, project_id: str):
        """Lazy import to avoid circular deps."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "togal_pipeline",
            Path(__file__).resolve().parent.parent.parent.parent / "scripts" / "togal_pipeline.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.TogalPipeline(project_id)

    def status(self):
        togal_auth = Path(__file__).resolve().parent.parent.parent.parent / "data" / "config" / "togal_auth.json"
        configured = togal_auth.exists()
        return {
            "provider": "togal",
            "configured": configured,
            "active": True,
            "description": "Togal AI takeoff — automated room detection and measurement from drawings.",
            "accuracy": "~98%",
            "workflow": "upload PDF → set scale → vectorize → extract rooms/walls/regions",
        }

    def upload_plans(self, pdf_paths, project_id):
        pipeline = self._get_pipeline(project_id)
        results = []
        for pdf in pdf_paths:
            result = pipeline.upload_plans(str(pdf))
            results.append(result)
        return {"provider": "togal", "uploads": results}

    def get_measurements(self, project_id):
        pipeline = self._get_pipeline(project_id)
        status = pipeline.get_status()
        if status.get("takeoff_complete"):
            # Read existing results
            takeoff_file = pipeline.project_dir / "togal_takeoff.json"
            import json
            data = json.loads(takeoff_file.read_text())
            return {
                "provider": "togal",
                "status": "complete",
                "grand_totals": data.get("grand_totals", {}),
                "pages": data.get("pages_measured", 0),
                "measurements": data.get("measurements", {}),
            }
        # Run extraction
        result = pipeline.run_extract_only()
        return {"provider": "togal", **result}

    def get_progress(self, project_id):
        pipeline = self._get_pipeline(project_id)
        status = pipeline.get_status()
        if status.get("takeoff_complete"):
            return {"provider": "togal", "status": "complete", "progress": 100}
        if status.get("views_created"):
            return {"provider": "togal", "status": "vectorizing", "progress": 50}
        return {"provider": "togal", "status": "pending", "progress": 0}


class StackProvider(TakeoffProvider):
    """STACK Construction Technologies API.
    Full developer API for creating projects, takeoff lists, retrieving quantities.
    Docs: https://www.stackct.com/developers-docs/
    """

    def name(self): return "stack"

    def status(self):
        config = _load_config()
        stack_config = config.get("providers", {}).get("stack", {})
        return {
            "provider": "stack",
            "configured": bool(stack_config.get("api_key")),
            "description": "STACK API — create projects, manage takeoff items, retrieve quantities and costs.",
            "setup": "Need API-Enabled STACK subscription. Add stack.api_key to takeoff_config.json",
        }

    def upload_plans(self, pdf_paths, project_id):
        return {"status": "not_implemented", "message": "STACK integration ready for activation"}

    def get_measurements(self, project_id):
        return {"status": "not_implemented"}

    def get_progress(self, project_id):
        return {"status": "not_implemented"}


PROVIDERS = {
    "manual": ManualProvider,
    "kreo": KreoProvider,
    "togal": TogalProvider,
    "stack": StackProvider,
}


def get_provider(name=None):
    if name is None:
        config = _load_config()
        name = config.get("active_provider", "manual")
    cls = PROVIDERS.get(name, ManualProvider)
    return cls()


def main():
    parser = argparse.ArgumentParser(description="CCF Takeoff Automation")
    parser.add_argument("--provider", default=None, help="Provider name")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--upload", action="store_true")
    parser.add_argument("--results", action="store_true")
    parser.add_argument("--plans", nargs="*", help="Plan PDF paths")
    parser.add_argument("--project", default=None, help="Project ID")
    parser.add_argument("--list-providers", action="store_true")
    args = parser.parse_args()

    if args.list_providers:
        result = {}
        for name, cls in PROVIDERS.items():
            p = cls()
            result[name] = p.status()
        print(json.dumps(result, indent=2))
        return

    provider = get_provider(args.provider)

    if args.status:
        result = provider.status()
    elif args.upload:
        result = provider.upload_plans(args.plans or [], args.project or "")
    elif args.results:
        result = provider.get_measurements(args.project or "")
    else:
        result = provider.status()

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
