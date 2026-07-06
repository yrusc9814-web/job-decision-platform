#!/bin/bash

BASE_DIR="/Users/vantawork/Documents/Ai/Claude code"
BACKEND_DIR="$BASE_DIR/job_decision_backend"
DATA_DIR="$BASE_DIR/data"
LOG_DIR="$BASE_DIR/logs"
DB_PATH="$DATA_DIR/job_decision.db"
LOG_FILE="$LOG_DIR/job_decision_server.log"
PORT=8787
HOST=127.0.0.1

mkdir -p "$DATA_DIR" "$LOG_DIR"
cd "$BASE_DIR"

# ── 1. Check port occupancy ──
OCCUPANT=$(lsof -nP -iTCP:$PORT -sTCP:LISTEN -t 2>/dev/null || true)
if [ -n "$OCCUPANT" ]; then
  if ps -p "$OCCUPANT" -o command= 2>/dev/null | grep -q "server.py"; then
    echo "✅ 后端已在运行 (PID $OCCUPANT)"
  else
    echo "❌ 端口 $PORT 被其他进程占用 (PID $OCCUPANT)，无法启动"
    echo "   请先关闭占用进程: kill $OCCUPANT"
    osascript -e "display notification \"端口 $PORT 被占用\" with title \"求职决策台\""
    exit 1
  fi
fi

# ── 2. Check python3 ──
PYTHON=""
if [ -f "$BACKEND_DIR/.venv/bin/python3" ]; then
  PYTHON="$BACKEND_DIR/.venv/bin/python3"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON="python3"
else
  echo "❌ 未找到 python3"
  osascript -e "display notification \"未找到 python3\" with title \"求职决策台\""
  exit 1
fi

# ── 3. Start if not already healthy ──
if curl -fsS "http://$HOST:$PORT/api/health" >/dev/null 2>&1; then
  echo "✅ 后端已就绪"
else
  echo "🚀 启动后端..."
  nohup "$PYTHON" "$BACKEND_DIR/server.py" >> "$LOG_FILE" 2>&1 &
  BACKEND_PID=$!
  echo "   后端 PID: $BACKEND_PID"

  # ── 4. Wait for health (max 15s) ──
  HEALTHY=false
  for i in $(seq 1 15); do
    if curl -fsS "http://$HOST:$PORT/api/health" >/dev/null 2>&1; then
      HEALTHY=true
      echo "✅ 后端就绪 (${i}s)"
      break
    fi
    sleep 1
  done

  if [ "$HEALTHY" = false ]; then
    echo "❌ 后端启动超时 (15s)，请检查日志: $LOG_FILE"
    osascript -e "display notification \"后端启动失败，请查看日志\" with title \"求职决策台\""
    exit 1
  fi
fi

# ── 5. Open browser ──
open "http://$HOST:$PORT/"
echo "✅ 已打开求职决策台"
