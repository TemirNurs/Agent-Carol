"""Structured logging — JSON-line audit log + colorized console.

Replaces the ad-hoc `print() + open(LOG_FILE, "a")` pattern in every script.

Usage:
    from scripts._lib import log
    L = log.get("backfill_contacts")
    L.info("scanning %d bids", 12)
    L.warn("Sheets API slow")
    L.error("send failed: %s", err)
    with L.timed("Gmail search"):
        ... # logs duration
"""

from __future__ import annotations

import json
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
LOGS_DIR = ROOT / "data" / "logs"
LEVEL_COLOR = {
    "DEBUG": "\033[2m",
    "INFO":  "\033[36m",
    "WARN":  "\033[33m",
    "ERROR": "\033[31m",
}
RESET = "\033[0m"


def _stamp() -> str:
    return datetime.now().isoformat(timespec="seconds")


class _Logger:
    """Per-script logger. Writes JSON lines to its own log file + colored console."""

    def __init__(self, name: str, console: bool = True):
        self.name = name
        self.console = console
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        self.path = LOGS_DIR / f"{name}.jsonl"

    def _emit(self, level: str, msg: str, **fields):
        rec = {
            "ts": _stamp(),
            "level": level,
            "logger": self.name,
            "msg": msg,
            **fields,
        }
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass
        if self.console and not _is_quiet():
            color = LEVEL_COLOR.get(level, "")
            line = f"{_stamp()} {color}{level:<5}{RESET} [{self.name}] {msg}"
            try:
                print(line, flush=True)
            except UnicodeEncodeError:
                print(line.encode("ascii", "replace").decode("ascii"), flush=True)

    def info(self, msg, *args, **fields):
        self._emit("INFO", msg % args if args else msg, **fields)

    def warn(self, msg, *args, **fields):
        self._emit("WARN", msg % args if args else msg, **fields)

    def error(self, msg, *args, **fields):
        self._emit("ERROR", msg % args if args else msg, **fields)

    def debug(self, msg, *args, **fields):
        self._emit("DEBUG", msg % args if args else msg, **fields)

    @contextmanager
    def timed(self, label: str, **fields):
        """Context manager that logs duration of a block."""
        t0 = time.time()
        try:
            yield
            self.info(f"{label} took %.2fs", time.time() - t0, label=label, **fields)
        except Exception as e:
            self.error(f"{label} FAILED after %.2fs: %s",
                       time.time() - t0, e, label=label, **fields)
            raise


_quiet = False


def _is_quiet() -> bool:
    return _quiet or any(a in sys.argv for a in ("--quiet", "-q"))


def set_quiet(q: bool):
    global _quiet
    _quiet = q


_loggers: dict[str, _Logger] = {}


def get(name: str) -> _Logger:
    """Get or create a logger for a script."""
    if name not in _loggers:
        _loggers[name] = _Logger(name)
    return _loggers[name]
