#!/usr/bin/env bash
# 手动测试面板启停。用法: bash scripts/manual-panel.sh start|stop|status
set -euo pipefail
APP=/root/market-deploy/agent/tech-digest
PIDFILE=/tmp/tech-digest-panel.pid
case "${1:-}" in
  start)
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "面板已在运行 pid=$(cat "$PIDFILE")"
      exit 0
    fi
    nohup "$APP/.venv/bin/python" "$APP/manual_server.py" >> "$APP/log/manual-panel.log" 2>&1 &
    echo $! > "$PIDFILE"
    sleep 1
    echo "面板已启动 pid=$(cat "$PIDFILE")  http://127.0.0.1:19086"
    echo "（本机访问: 先运行 manual-panel.ps1 建立隧道，或 ssh -L 19086:127.0.0.1:19086 market-server）"
    ;;
  stop)
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      kill "$(cat "$PIDFILE")" && rm -f "$PIDFILE"
      echo "面板已停止"
    else
      echo "面板未在运行"
    fi
    ;;
  status)
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "运行中 pid=$(cat "$PIDFILE")"
    else
      echo "未运行"
    fi
    ;;
  *) echo "用法: $0 start|stop|status"; exit 1;;
esac
