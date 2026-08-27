#!/bin/bash
# 见字如面 - 开发服务后台驻留管理脚本
# 用法: bash scripts/dev-daemon.sh [start|stop|status]
# 与 start.sh 的区别：进程通过 nohup 脱离终端常驻，关闭终端/会话回收后服务不退出。
# 日志与 PID 文件位于 .dev-logs/ 目录。

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="$PROJECT_DIR/.dev-logs"
mkdir -p "$LOG_DIR"

# 确保 node/npx 可用（无 nvm 环境时自动加载默认版本，避免 nohup 子进程找不到 npx）
if ! command -v npx >/dev/null 2>&1; then
  NVM_NODE_BIN="$HOME/.nvm/versions/node/$(ls "$HOME/.nvm/versions/node" 2>/dev/null | tail -1)/bin"
  [ -d "$NVM_NODE_BIN" ] && export PATH="$NVM_NODE_BIN:$PATH"
fi

BACKEND_PID_FILE="$LOG_DIR/backend.pid"
FRONTEND_PID_FILE="$LOG_DIR/frontend.pid"

is_running() { [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }

stop() {
  for f in "$BACKEND_PID_FILE" "$FRONTEND_PID_FILE"; do
    if is_running "$f"; then
      kill "$(cat "$f")" 2>/dev/null
      echo "  已停止进程 $(cat "$f")"
    fi
    rm -f "$f"
  done
  # 兜底：按端口清理残留监听进程
  for port in 8000 5173; do
    pids=$(lsof -t -i:"$port" -sTCP:LISTEN 2>/dev/null)
    if [ -n "$pids" ]; then
      kill $pids 2>/dev/null
      echo "  已清理端口 $port 残留进程: $pids"
    fi
  done
}

start() {
  stop
  sleep 1
  echo "[1/2] 启动后端 (http://localhost:8000)..."
  cd "$PROJECT_DIR/backend"
  nohup .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 \
    > "$LOG_DIR/backend.log" 2>&1 &
  echo $! > "$BACKEND_PID_FILE"

  echo "[2/2] 启动前端 (http://localhost:5173)..."
  cd "$PROJECT_DIR/frontend"
  nohup npx vite --host 0.0.0.0 --port 5173 > "$LOG_DIR/frontend.log" 2>&1 &
  echo $! > "$FRONTEND_PID_FILE"

  sleep 4
  status
  echo ""
  echo "日志: $LOG_DIR/{backend,frontend}.log"
}

status() {
  if is_running "$BACKEND_PID_FILE"; then
    b="running(pid $(cat "$BACKEND_PID_FILE"))"
  else
    b="stopped"
  fi
  if is_running "$FRONTEND_PID_FILE"; then
    f="running(pid $(cat "$FRONTEND_PID_FILE"))"
  else
    f="stopped"
  fi
  bh=$(curl -s -o /dev/null -m 3 -w '%{http_code}' http://localhost:8000/api/health)
  fh=$(curl -s -o /dev/null -m 3 -w '%{http_code}' http://localhost:5173/)
  echo "  backend : $b | /api/health -> $bh"
  echo "  frontend: $f | /           -> $fh"
}

case "${1:-start}" in
  start) start ;;
  stop) stop ;;
  status) status ;;
  *) echo "用法: $0 [start|stop|status]" ;;
esac
