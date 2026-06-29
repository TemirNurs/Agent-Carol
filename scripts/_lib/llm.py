"""LLM — fallback chain wrapper with structured-output enforcement.

Replaces ad-hoc litellm calls scattered across:
  - llm_extract_due_dates.py
  - process_followup_replies.py
  - draft_email.py

Default chain (per user 5/5/2026): Gemini Flash primary, then free APIs.
Cost watchdog can kill the Gemini path; we fall back automatically.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

# 5/30 fix — load .env so GMAIL_APP_PASSWORD / API keys are present when
# Carol (OpenClaw/Telegram) shells out to this script. A shelled child does
# NOT inherit the daemon's env, so without this the credential reads below
# return '' and the script fails (e.g. IMAP login). Absolute path → cwd-safe.
try:
    from pathlib import Path as _CCF_P
    from dotenv import load_dotenv as _ccf_load_dotenv
    # repo root is THREE levels up from scripts/_lib/llm.py — parent.parent
    # pointed at scripts/.env (nonexistent), silently disabling API keys.
    _ccf_load_dotenv(_CCF_P(__file__).resolve().parent.parent.parent / ".env")
except Exception:
    pass

import glob as _glob
import shutil as _shutil
import subprocess as _subprocess

# Provider chain (user 2026-06-19: "Claude Code paramount; Gemini ONLY when Claude
# can't — Claude Code runs on the subscription = free, Gemini costs money").
# claude-code shells out to the local Claude Code CLI (subscription); paid Gemini
# is the fallback ONLY when the CLI is unavailable/not-logged-in.
DEFAULT_CHAIN = [
    "claude-code",
    "gemini/gemini-2.5-flash",
    "groq/llama-3.3-70b-versatile",
    "cerebras/llama3.1-8b",
    "ollama/gemma4:latest",
]


def _find_claude_exe():
    p = _shutil.which("claude")
    if p:
        return p
    base = os.path.join(os.environ.get("APPDATA", ""), "Claude", "claude-code")
    cands = sorted(_glob.glob(os.path.join(base, "*", "claude.exe")))
    return cands[-1] if cands else None


CLAUDE_EXE = _find_claude_exe()


def _claude_code_chat(messages, max_tokens, timeout=150):
    """Run the prompt through the local Claude Code CLI (subscription = free).
    Raises on any failure (not-logged-in, empty, non-zero) so chat() falls through
    to the paid Gemini fallback."""
    if not CLAUDE_EXE:
        raise RuntimeError("claude.exe not found")
    sys_txt = "\n\n".join(m["content"] for m in messages if m.get("role") == "system")
    usr_txt = "\n\n".join(m["content"] for m in messages if m.get("role") != "system")
    prompt = (sys_txt + "\n\n" + usr_txt).strip() if sys_txt else usr_txt
    # Force SUBSCRIPTION auth (free), never the metered API key: strip Anthropic
    # key/token from the child env so claude -p uses the logged-in subscription.
    _env = dict(os.environ)
    for _k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_API_KEY"):
        _env.pop(_k, None)
    # Pass the prompt on STDIN, not argv — large/multiline thread transcripts blow
    # the Windows command-line length/escaping limit when passed as an argument.
    r = _subprocess.run([CLAUDE_EXE, "-p"], input=prompt, capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=timeout, env=_env)
    out = (r.stdout or "").strip()
    low = (out + " " + (r.stderr or "")).lower()
    if r.returncode != 0 or not out or any(s in low for s in
            ("please run /login", "not logged in", "invalid api key", "usage limit", "rate limit")):
        raise RuntimeError("claude-code unavailable: " + (out or r.stderr or "empty")[:140])
    return out


def _api_keys_available() -> dict[str, bool]:
    return {
        "claude-code": bool(CLAUDE_EXE),   # CLI present; login checked at call time → falls back if not
        "gemini":   bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")),
        "groq":     bool(os.environ.get("GROQ_API_KEY")),
        "cerebras": bool(os.environ.get("CEREBRAS_API_KEY")),
        "ollama":   True,  # local — assume available
    }


def _provider(model: str) -> str:
    return model.split("/", 1)[0] if "/" in model else model


def chat(messages: list[dict], *, model: str | None = None,
         max_tokens: int = 800, temperature: float = 0.4,
         json_mode: bool = False, retries: int = 1,
         chain: list[str] | None = None) -> dict:
    """Run a chat completion through the fallback chain. Returns:
        {"text": str, "model": str, "ok": bool, "error": str|None}

    `messages` is a list of {"role": "system|user|assistant", "content": str}.
    `json_mode=True` requires JSON output (uses response_format when supported).
    """
    try:
        import litellm
    except ImportError:
        return {"text": "", "model": "", "ok": False, "error": "litellm not installed"}

    candidates = [model] if model else (chain or list(DEFAULT_CHAIN))
    keys = _api_keys_available()
    candidates = [m for m in candidates if keys.get(_provider(m), False)]
    # Honor the cost watchdog: when it tripped today, drop paid gemini/* from
    # the chain (it previously only edited openclaw.json, so daemon scripts
    # kept burning paid tokens after the kill).
    try:
        import json as _json
        from datetime import date as _date
        _wd = _CCF_P(__file__).resolve().parent.parent.parent / "data" / "memory" / "cost_watchdog.json"
        if _wd.exists():
            _st = _json.loads(_wd.read_text(encoding="utf-8"))
            _killed = str(_st.get("killed_today") or _st.get("killed_date") or "")
            if _killed == _date.today().isoformat():
                _filtered = [m for m in candidates if not m.startswith("gemini")]
                if _filtered:
                    candidates = _filtered
    except Exception:
        pass
    if not candidates:
        return {"text": "", "model": "", "ok": False, "error": "no API keys configured"}

    last_err = None
    for m in candidates:
        for attempt in range(retries + 1):
            try:
                if m == "claude-code":
                    text = _claude_code_chat(messages, max_tokens)
                    return {"text": text, "model": "claude-code", "ok": True, "error": None}
                kwargs = dict(model=m, max_tokens=max_tokens,
                              temperature=temperature, messages=messages)
                if json_mode:
                    kwargs["response_format"] = {"type": "json_object"}
                r = litellm.completion(**kwargs)
                text = r.choices[0].message.content.strip()
                return {"text": text, "model": m, "ok": True, "error": None}
            except Exception as e:
                last_err = str(e)
                err = last_err.lower()
                # Retry same model on transient errors, fall through on quota
                if any(t in err for t in ("rate limit", "429", "quota",
                                            "too many", "overloaded", "capacity")):
                    break  # try next model
                if attempt < retries:
                    time.sleep(1)  # transient — retry once
                    continue
                break  # try next model
    return {"text": "", "model": "", "ok": False,
            "error": f"all models failed: {last_err}"}


def chat_json(messages: list[dict], **kw) -> dict:
    """Convenience: chat() + parse JSON output. Returns parsed dict or {"error": ...}."""
    kw.setdefault("json_mode", True)
    r = chat(messages, **kw)
    if not r["ok"]:
        return {"error": r["error"]}
    text = r["text"]
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        out = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            return {"error": "no JSON in response", "raw": text[:300]}
        try:
            out = json.loads(m.group(0))
        except json.JSONDecodeError as e:
            return {"error": f"JSON parse: {e}", "raw": text[:300]}
    out["_model"] = r["model"]
    return out
