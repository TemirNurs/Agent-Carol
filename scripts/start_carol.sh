#!/bin/bash
# start_carol.sh — Launch Carol's full stack
# 1. Carol daemon (Python async loop — bid scraping, briefings, pipeline)
# 2. OpenClaw gateway (Node.js — WhatsApp, Telegram, web)
#
# Usage:
#   bash scripts/start_carol.sh          # Start everything
#   bash scripts/start_carol.sh --daemon  # Daemon only
#   bash scripts/start_carol.sh --gateway # Gateway only
#   bash scripts/start_carol.sh --stop    # Stop all

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"
cd "$ROOT"

DAEMON_PID_FILE="data/carol.pid"
GATEWAY_PID_FILE="data/openclaw.pid"
LOG_DIR="data/logs"
mkdir -p "$LOG_DIR"

start_daemon() {
    if [ -f "$DAEMON_PID_FILE" ] && kill -0 "$(cat "$DAEMON_PID_FILE")" 2>/dev/null; then
        echo "[DAEMON] Already running (PID $(cat "$DAEMON_PID_FILE"))"
        return
    fi
    echo "[DAEMON] Starting carol_daemon.py..."
    python carol_daemon.py >> "$LOG_DIR/carol_daemon.log" 2>&1 &
    echo $! > "$DAEMON_PID_FILE"
    echo "[DAEMON] Started (PID $!)"
}

start_gateway() {
    if [ -f "$GATEWAY_PID_FILE" ] && kill -0 "$(cat "$GATEWAY_PID_FILE")" 2>/dev/null; then
        echo "[GATEWAY] Already running (PID $(cat "$GATEWAY_PID_FILE"))"
        return
    fi

    # Check for API keys — prefer Gemini (cheaper), fall back to Anthropic
    if [ -z "$GEMINI_API_KEY" ] && [ -z "$ANTHROPIC_API_KEY" ]; then
        echo "[GATEWAY] WARNING: No GEMINI_API_KEY or ANTHROPIC_API_KEY set. Falling back to ollama."
    elif [ -n "$GEMINI_API_KEY" ]; then
        echo "[GATEWAY] Using Gemini 2.5 Flash (GEMINI_API_KEY set)"
    else
        echo "[GATEWAY] Using Anthropic Sonnet (ANTHROPIC_API_KEY set)"
    fi

    echo "[GATEWAY] Starting OpenClaw on port 18789..."
    npx openclaw start >> "$LOG_DIR/openclaw.log" 2>&1 &
    echo $! > "$GATEWAY_PID_FILE"
    echo "[GATEWAY] Started (PID $!)"
}

stop_all() {
    for pidfile in "$DAEMON_PID_FILE" "$GATEWAY_PID_FILE"; do
        if [ -f "$pidfile" ]; then
            pid=$(cat "$pidfile")
            if kill -0 "$pid" 2>/dev/null; then
                echo "Stopping PID $pid..."
                kill "$pid"
            fi
            rm -f "$pidfile"
        fi
    done
    echo "All services stopped."
}

health_check() {
    echo "=== CAROL HEALTH CHECK ==="
    # Daemon
    if [ -f "$DAEMON_PID_FILE" ] && kill -0 "$(cat "$DAEMON_PID_FILE")" 2>/dev/null; then
        echo "[DAEMON] Running (PID $(cat "$DAEMON_PID_FILE"))"
    else
        echo "[DAEMON] NOT running"
    fi
    # Gateway
    if [ -f "$GATEWAY_PID_FILE" ] && kill -0 "$(cat "$GATEWAY_PID_FILE")" 2>/dev/null; then
        echo "[GATEWAY] Running (PID $(cat "$GATEWAY_PID_FILE"))"
    else
        echo "[GATEWAY] NOT running"
    fi
    # Quick port check
    if command -v curl &>/dev/null; then
        if curl -s -o /dev/null -w "%{http_code}" http://localhost:18789/health 2>/dev/null | grep -q "200"; then
            echo "[GATEWAY] HTTP OK on port 18789"
        fi
    fi
}

case "${1:-all}" in
    --daemon)  start_daemon ;;
    --gateway) start_gateway ;;
    --stop)    stop_all ;;
    --health)  health_check ;;
    all)
        start_daemon
        start_gateway
        echo ""
        echo "Carol is online."
        echo "  Daemon log:  $LOG_DIR/carol_daemon.log"
        echo "  Gateway log: $LOG_DIR/openclaw.log"
        echo "  Stop:        bash scripts/start_carol.sh --stop"
        ;;
    *)
        echo "Usage: start_carol.sh [--daemon|--gateway|--stop|--health]"
        ;;
esac
