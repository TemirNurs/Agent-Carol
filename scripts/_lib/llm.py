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

# Provider chain — user-set order: paid Gemini first, free APIs as fallback
DEFAULT_CHAIN = [
    "gemini/gemini-2.5-flash",
    "groq/llama-3.3-70b-versatile",
    "cerebras/llama3.1-8b",
    "ollama/gemma4:latest",
]


def _api_keys_available() -> dict[str, bool]:
    return {
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
    if not candidates:
        return {"text": "", "model": "", "ok": False, "error": "no API keys configured"}

    last_err = None
    for m in candidates:
        for attempt in range(retries + 1):
            try:
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
