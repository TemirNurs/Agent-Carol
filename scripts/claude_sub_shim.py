#!/usr/bin/env python3
r"""claude_sub_shim.py — local OpenAI-compatible endpoint backed by the Claude
MAX SUBSCRIPTION (claude.exe -p), so the OpenClaw Telegram agent can use the
subscription for FREE — bypassing OpenClaw v2026.4.2's broken `claude-cli`
backend (which never registers its models -> "Unknown model" -> Gemini fallback).

HOW IT WORKS
  Exposes /v1/chat/completions + /v1/models on 127.0.0.1:PORT (OpenAI shape).
  Each chat request shells `claude -p --output-format json --model <m>` against
  Nursultan's Max subscription (the same ~/.claude OAuth claude.exe uses — $0),
  and returns an OpenAI ChatCompletion. Point OpenClaw at baseUrl
  http://127.0.0.1:PORT/v1 as a custom openai provider (like it talks to Ollama).
  Run as a background service.

  Model mapping: OpenClaw model id -> claude --model. 'claude-max' -> opus (=4.8,
  the latest the subscription serves). So this delivers Opus the CLI backend can't.
"""
import json
import os
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CLAUDE_EXE = r"C:\nodejs\node-v22.16.0-win-x64\node_modules\@anthropic-ai\claude-code\bin\claude.exe"
PORT = 8199
DEFAULT_MODEL = "opus"
MODEL_MAP = {"claude-max": "opus", "claude-max-opus": "opus", "claude-max-sonnet": "sonnet",
             "opus": "opus", "sonnet": "sonnet", "haiku": "haiku"}
TIMEOUT = 240

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def run_claude(messages, model):
    """Shell claude.exe -p on the Max subscription; return the reply text."""
    sys_parts, convo = [], []
    for m in messages or []:
        role = (m.get("role") or "user")
        c = m.get("content")
        if isinstance(c, list):  # OpenAI content-blocks
            c = " ".join(b.get("text", "") for b in c if isinstance(b, dict))
        c = (c or "").strip()
        if not c:
            continue
        if role == "system":
            sys_parts.append(c)
        else:
            convo.append(f"{role.upper()}: {c}")
    prompt = "\n\n".join(convo) or "(no message)"
    # Fold the system prompt into STDIN — NOT a command-line arg. OpenClaw's
    # system prompt is ~54KB; passing it via --append-system-prompt blows the
    # Windows ~32KB command-line limit (WinError 206). _lib/llm.py does the same.
    if sys_parts:
        prompt = "\n\n".join(sys_parts) + "\n\n" + prompt
    args = [CLAUDE_EXE, "-p", "--output-format", "json", "--model", model]
    # CRITICAL: strip any inherited ANTHROPIC_API_KEY / auth-token / base-url so
    # claude.exe uses the ~/.claude Max SUBSCRIPTION OAuth (free), NOT a paid API
    # key. A stray dead/no-credit ANTHROPIC_API_KEY in the machine env (which the
    # daemon-spawned shim inherits) otherwise hijacks claude.exe onto the paid API
    # and every reply becomes "Credit balance is too low".
    env = {k: v for k, v in os.environ.items()
           if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")}
    try:
        r = subprocess.run(args, input=prompt, capture_output=True, text=True,
                           timeout=TIMEOUT, encoding="utf-8", errors="replace", env=env)
    except subprocess.TimeoutExpired:
        return "(claude subscription timed out)"
    out = (r.stdout or "").strip()
    try:
        j = json.loads(out)
        return (j.get("result") or j.get("text") or "").strip() or out
    except Exception:
        return out or ((r.stderr or "claude error")[:400])


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        b = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            self._json(200, {"object": "list", "data": [
                {"id": "claude-max", "object": "model", "owned_by": "anthropic-subscription"}]})
        else:
            self._json(200, {"status": "ok", "backend": "claude-max-subscription"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(n).decode("utf-8", "replace") if n else "{}"
        try:
            req = json.loads(raw)
        except Exception:
            req = {}
        messages = req.get("messages", [])
        mid = str(req.get("model", "")).split("/")[-1].strip().lower()
        model = MODEL_MAP.get(mid, DEFAULT_MODEL)
        text = run_claude(messages, model)
        created = int(time.time())
        if req.get("stream"):
            # minimal SSE: one content chunk + stop + [DONE]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()

            def chunk(delta, finish=None):
                payload = {"id": f"chatcmpl-{created}", "object": "chat.completion.chunk",
                           "created": created, "model": "claude-max",
                           "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode("utf-8"))
            chunk({"role": "assistant", "content": text})
            chunk({}, "stop")
            self.wfile.write(b"data: [DONE]\n\n")
            return
        self._json(200, {
            "id": f"chatcmpl-{created}", "object": "chat.completion", "created": created,
            "model": "claude-max",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        })


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"claude_sub_shim listening on http://127.0.0.1:{PORT}/v1  (model->{DEFAULT_MODEL}, Max subscription)")
    srv.serve_forever()


if __name__ == "__main__":
    main()
