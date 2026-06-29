#!/usr/bin/env python3
"""
CCF Project Store — File-based project data management.
Usage:
  python project_store.py create --name "Food Lion 1513" --gc "WED Construction" --tier target --due "2026-03-18"
  python project_store.py list
  python project_store.py get --id food-lion-1513
  python project_store.py update --id food-lion-1513 --status estimate
  python project_store.py save-phase --id food-lion-1513 --phase sow --file sow.md
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data" / "projects"


def slugify(name):
    """Convert project name to URL-safe slug."""
    s = name.lower().strip()
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"[\s]+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-")


def create_project(name, gc="", tier="target", due_date="", files=None):
    """Create a new project."""
    slug = slugify(name)
    project_dir = DATA_DIR / slug
    project_dir.mkdir(parents=True, exist_ok=True)

    project = {
        "id": slug,
        "name": name,
        "gc": gc,
        "pricing_tier": tier,
        "due_date": due_date,
        "status": "created",
        "files": files or [],
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
    }

    project_file = project_dir / "project.json"
    with open(project_file, "w") as f:
        json.dump(project, f, indent=2)

    return project


def get_project(project_id):
    """Get a project by ID."""
    project_file = DATA_DIR / project_id / "project.json"
    if not project_file.exists():
        return None
    with open(project_file) as f:
        return json.load(f)


def update_project(project_id, **updates):
    """Update project fields."""
    project = get_project(project_id)
    if not project:
        return None

    for key, value in updates.items():
        if value is not None:
            project[key] = value
    project["updated_at"] = datetime.now().isoformat()

    project_file = DATA_DIR / project_id / "project.json"
    with open(project_file, "w") as f:
        json.dump(project, f, indent=2)

    return project


def list_projects():
    """List all projects."""
    if not DATA_DIR.exists():
        return []

    projects = []
    for d in sorted(DATA_DIR.iterdir()):
        if d.is_dir():
            pf = d / "project.json"
            if pf.exists():
                with open(pf) as f:
                    projects.append(json.load(f))
    return projects


def save_phase_output(project_id, phase, content=None, filepath=None):
    """Save phase output to project directory.
    phase: sow, takeoff, estimate, proposal, email
    """
    project_dir = DATA_DIR / project_id
    if not project_dir.exists():
        return {"error": f"Project {project_id} not found"}

    ext_map = {
        "sow": ".md",
        "takeoff_plan": ".md",
        "takeoff": ".csv",
        "estimate": ".json",
        "proposal": ".md",
        "email": ".json",
    }

    ext = ext_map.get(phase, ".txt")
    output_file = project_dir / f"{phase}{ext}"

    if filepath:
        import shutil
        shutil.copy2(filepath, output_file)
    elif content:
        with open(output_file, "w", encoding="utf-8") as f:
            if isinstance(content, (dict, list)):
                json.dump(content, f, indent=2)
            else:
                f.write(content)

    # Update project status
    update_project(project_id, status=phase)

    return {"saved": str(output_file), "phase": phase}


def get_phase_output(project_id, phase):
    """Read a phase output file."""
    project_dir = DATA_DIR / project_id
    ext_map = {
        "sow": ".md",
        "takeoff_plan": ".md",
        "takeoff": ".csv",
        "estimate": ".json",
        "proposal": ".md",
        "email": ".json",
    }
    ext = ext_map.get(phase, ".txt")
    output_file = project_dir / f"{phase}{ext}"

    if not output_file.exists():
        return None

    with open(output_file, encoding="utf-8") as f:
        content = f.read()

    if ext == ".json":
        return json.loads(content)
    return content


def main():
    parser = argparse.ArgumentParser(description="CCF Project Store")
    sub = parser.add_subparsers(dest="command")

    # create
    p_create = sub.add_parser("create")
    p_create.add_argument("--name", required=True)
    p_create.add_argument("--gc", default="")
    p_create.add_argument("--tier", default="target", choices=["floor", "target", "premium"])
    p_create.add_argument("--due", default="")

    # list
    sub.add_parser("list")

    # get
    p_get = sub.add_parser("get")
    p_get.add_argument("--id", required=True)

    # update
    p_update = sub.add_parser("update")
    p_update.add_argument("--id", required=True)
    p_update.add_argument("--status", default=None)
    p_update.add_argument("--tier", default=None)
    p_update.add_argument("--gc", default=None)

    # save-phase
    p_save = sub.add_parser("save-phase")
    p_save.add_argument("--id", required=True)
    p_save.add_argument("--phase", required=True)
    p_save.add_argument("--file", default=None)
    p_save.add_argument("--content", default=None)

    # get-phase
    p_gphase = sub.add_parser("get-phase")
    p_gphase.add_argument("--id", required=True)
    p_gphase.add_argument("--phase", required=True)

    args = parser.parse_args()

    if args.command == "create":
        result = create_project(args.name, args.gc, args.tier, args.due)
    elif args.command == "list":
        result = list_projects()
    elif args.command == "get":
        result = get_project(args.id)
        if result is None:
            result = {"error": f"Project {args.id} not found"}
    elif args.command == "update":
        result = update_project(args.id, status=args.status, pricing_tier=args.tier, gc=args.gc)
    elif args.command == "save-phase":
        result = save_phase_output(args.id, args.phase, content=args.content, filepath=args.file)
    elif args.command == "get-phase":
        result = get_phase_output(args.id, args.phase)
        if result is None:
            result = {"error": f"Phase {args.phase} not found for project {args.id}"}
    else:
        parser.print_help()
        sys.exit(1)

    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
