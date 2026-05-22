#!/usr/bin/env python3
"""
Cost watchdog — monitor daily Gemini spend; alert and kill-switch on overrun.

Reads OpenClaw session jsonls, sums today's google input + output tokens,
computes spend at official rates. Sends Telegram alert at WARN threshold,
flips primary to Groq at KILL threshold (preventing further Gemini burn).

Designed to run every hour via daemon.

Usage:
  python scripts/cost_watchdog.py                # check + alert + kill if needed
  python scripts/cost_watchdog.py --report       # just print breakdown
  python scripts/cost_watchdog.py --reset        # reset kill-switch (re-enable Gemini)
"""

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = Path(__file__).resolve().parent.parent
SESSIONS_DIR = Path("C:/Users/Nursm/.openclaw/agents/main/sessions")
OPENCLAW_CFG = Path("C:/Users/Nursm/.openclaw/openclaw.json")
WATCHDOG_LOG = BASE / "data" / "memory" / "cost_watchdog.json"

# Gemini 2.5 Flash rates (April 2026)
RATE_INPUT_PER_M  = 0.30
RATE_OUTPUT_PER_M = 2.50

# Thresholds (per day)
WARN_USD = 0.50
KILL_USD = 1.50

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.environ.get("USER_TELEGRAM_CHAT_ID", "")


def todays_provider_breakdown():
    """Sum tokens per provider used today across all sessions."""
    today = date.today().isoformat()
    by_provider = {}
    for jf in SESSIONS_DIR.glob("*.jsonl"):
        try:
            with open(jf, encoding="utf-8") as f:
                for line in f:
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    if e.get("type") != "message":
                        continue
                    ts = e.get("timestamp", "")
                    if not ts.startswith(today):
                        continue
                    m = e.get("message", {})
                    prov = m.get("provider")
                    if not prov:
                        continue
                    u = m.get("usage", {})
                    tin  = u.get("input", 0) or 0
                    tout = u.get("output", 0) or 0
                    if prov not in by_provider:
                        by_provider[prov] = {"calls": 0, "in": 0, "out": 0}
                    by_provider[prov]["calls"] += 1
                    by_provider[prov]["in"]  += tin
                    by_provider[prov]["out"] += tout
        except Exception:
            pass
    return by_provider


def todays_spend():
    """Sum google-provider tokens (cost basis) — used for kill-switch threshold."""
    bp = todays_provider_breakdown()
    g = bp.get("google", {})
    total_in  = g.get("in", 0)
    total_out = g.get("out", 0)
    cost_in  = total_in  * RATE_INPUT_PER_M  / 1_000_000
    cost_out = total_out * RATE_OUTPUT_PER_M / 1_000_000
    return {
        "calls": g.get("calls", 0),
        "input_tokens": total_in,
        "output_tokens": total_out,
        "cost_input": cost_in,
        "cost_output": cost_out,
        "cost_total": cost_in + cost_out,
        "all_providers": bp,
    }


def tg_send(text):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"
        }).encode("utf-8")
        urllib.request.urlopen(
            urllib.request.Request(url, data=data, method="POST"), timeout=10)
        return True
    except Exception as e:
        print(f"[wd] tg fail: {e}")
        return False


def flip_primary_to_groq():
    """Edit openclaw.json so primary = groq, gemini moves to fallback."""
    cfg = json.load(open(OPENCLAW_CFG, encoding="utf-8"))
    mdl = cfg["agents"]["defaults"]["model"]
    if mdl.get("primary") == "groq/llama-3.3-70b-versatile":
        return False  # already flipped
    mdl["primary"] = "groq/llama-3.3-70b-versatile"
    mdl["fallbacks"] = ["google/gemini-2.5-flash", "cerebras/llama3.1-8b"]
    json.dump(cfg, open(OPENCLAW_CFG, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    return True


def flip_primary_to_gemini():
    """Restore Gemini as primary (--reset path)."""
    cfg = json.load(open(OPENCLAW_CFG, encoding="utf-8"))
    mdl = cfg["agents"]["defaults"]["model"]
    if mdl.get("primary") == "google/gemini-2.5-flash":
        return False
    mdl["primary"] = "google/gemini-2.5-flash"
    mdl["fallbacks"] = ["groq/llama-3.3-70b-versatile", "cerebras/llama3.1-8b"]
    json.dump(cfg, open(OPENCLAW_CFG, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    return True


def load_state():
    if WATCHDOG_LOG.exists():
        try: return json.load(open(WATCHDOG_LOG, encoding="utf-8"))
        except: pass
    return {"warned_today": "", "killed_today": "", "history": []}


def save_state(s):
    WATCHDOG_LOG.parent.mkdir(parents=True, exist_ok=True)
    json.dump(s, open(WATCHDOG_LOG, "w", encoding="utf-8"), indent=2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--reset",  action="store_true")
    ap.add_argument("--quiet",  action="store_true")
    args = ap.parse_args()

    if args.reset:
        flipped = flip_primary_to_gemini()
        state = load_state()
        state["killed_today"] = ""
        save_state(state)
        msg = "Gemini re-enabled as primary" if flipped else "Already on Gemini"
        print(f"[wd] {msg}")
        return

    s = todays_spend()
    today = date.today().isoformat()
    state = load_state()

    if args.report:
        print(f"=== Carol provider breakdown for {today} ===")
        print(f"{'Provider':<12} {'Calls':>6} {'Input':>14} {'Output':>10} {'Cost':>10}")
        print("-" * 60)
        for prov, st in sorted(s.get("all_providers", {}).items()):
            cost = ""
            if prov == "google":
                ci = st["in"]  * RATE_INPUT_PER_M  / 1_000_000
                co = st["out"] * RATE_OUTPUT_PER_M / 1_000_000
                cost = f"${ci+co:.4f}"
            else:
                cost = "$0 (free)"
            print(f"  {prov:<10} {st['calls']:>6,} {st['in']:>14,} {st['out']:>10,} {cost:>10}")
        print()
        print(f"  Gemini paid spend today: ${s['cost_total']:.4f}")
        print(f"  Warn threshold: ${WARN_USD:.2f}  |  Kill threshold: ${KILL_USD:.2f}")
        print(f"  warned_today: {state.get('warned_today', '')}")
        print(f"  killed_today: {state.get('killed_today', '')}")
        return

    cost = s["cost_total"]
    summary = f"cost_watchdog {datetime.now().strftime('%H:%M:%S')}: today=${cost:.3f} ({s['calls']} calls, {s['input_tokens']:,} in / {s['output_tokens']:,} out)"

    # Kill switch: spending more than $1.50 in one day
    if cost >= KILL_USD and state.get("killed_today") != today:
        flipped = flip_primary_to_groq()
        if flipped:
            tg_send(
                f"🚨 *Cost watchdog tripped*\n"
                f"  • Today's Gemini spend: *${cost:.2f}*\n"
                f"  • Threshold: ${KILL_USD:.2f}\n"
                f"  • Action: Switched primary to Groq (free tier)\n"
                f"  • Re-enable: `python scripts/cost_watchdog.py --reset`"
            )
        state["killed_today"] = today
        state["history"].append({"at": datetime.now().isoformat(), "event": "kill", "cost": cost})
        save_state(state)
        if args.quiet: print(summary + " | KILL_SWITCH_TRIPPED")
        else: print(summary + "\n[wd] KILL: switched primary to Groq")
        return

    # Warn at $0.50 (once per day)
    if cost >= WARN_USD and state.get("warned_today") != today:
        tg_send(
            f"⚠️ *Cost watchdog warning*\n"
            f"  • Today's Gemini spend: *${cost:.2f}*\n"
            f"  • At ${KILL_USD:.2f}/day, Carol auto-falls back to Groq\n"
            f"  • Calls today: {s['calls']}, input tokens: {s['input_tokens']:,}"
        )
        state["warned_today"] = today
        state["history"].append({"at": datetime.now().isoformat(), "event": "warn", "cost": cost})
        save_state(state)
        if args.quiet: print(summary + " | WARN")
        else: print(summary + "\n[wd] WARN sent to Telegram")
        return

    if args.quiet: print(summary)
    else: print(summary)


if __name__ == "__main__":
    main()
