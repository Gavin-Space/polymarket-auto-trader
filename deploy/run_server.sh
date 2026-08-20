#!/usr/bin/env bash
# ============================================================
# Polymarket Auto-Trader — simple native launcher (no systemd)
# Usage:  ./run_server.sh            (start in background)
#         ./run_server.sh stop       (stop it)
#         ./run_server.sh logs       (tail the log)
# ============================================================
set -euo pipefail
cd "$(dirname "$0")"

DATA_DIR="${POLY_WORKSPACE:-$(pwd)/data}"
mkdir -p "$DATA_DIR"

if [ "${1:-}" = "stop" ]; then
  pkill -f "auto_trader.py" || true
  echo "PolyAuto stopped."
  exit 0
fi

if [ "${1:-}" = "logs" ]; then
  tail -f "$DATA_DIR/server_output.log" 2>/dev/null || echo "no log yet"
  exit 0
fi

if pgrep -f "auto_trader.py" >/dev/null; then
  echo "PolyAuto is already running. Use: ./run_server.sh stop"
  exit 1
fi

export PYTHONUNBUFFERED=1
export POLY_WORKSPACE="$DATA_DIR"

if [ -x "./venv/bin/python" ]; then
  PY=./venv/bin/python
else
  PY=python3
fi

export PORT="${PORT:-5001}"
nohup "$PY" auto_trader.py >> "$DATA_DIR/server_output.log" 2>&1 &
echo "PolyAuto started (PID $!)."
echo "Dashboard: http://localhost:${PORT}"
echo "Logs:      tail -f $DATA_DIR/server_output.log"
