"""Meta-test: AGENTS_LESSONS.md rules must continue to pass.

This wraps the existing lessons audit in pytest so CI catches regressions.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent


def test_lessons_audit_passes():
    """Run scripts/_lessons_audit.py — must exit 0 (no rule violations)."""
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "_lessons_audit.py")],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(ROOT),
    )
    assert r.returncode == 0, (
        f"Lessons audit FAILED — see AGENTS_LESSONS.md.\n"
        f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    )
