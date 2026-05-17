#!/usr/bin/env bash
# 起 FastAPI（:8000）+ Vite dev（:5173）双 server，开两个终端各跑一个最稳。
# 本脚本提供：① 一键启动指引 ② 检查端口占用 ③ 停止帮助

set -uo pipefail
cd "$(dirname "$0")/.."

cmd="${1:-}"

case "$cmd" in
  start)
    echo "▎检查端口"
    if lsof -i:8000 >/dev/null 2>&1; then echo "  ⚠️  8000 被占用"; else echo "  ✅ 8000 空闲"; fi
    if lsof -i:5173 >/dev/null 2>&1; then echo "  ⚠️  5173 被占用"; else echo "  ✅ 5173 空闲"; fi

    echo
    echo "▎启动后端（FastAPI :8000）"
    PYTHONPATH=src nohup python3 -m uvicorn ai_ops.api.main:app --host 127.0.0.1 --port 8000 \
      > /tmp/ai-ops-backend.log 2>&1 &
    echo "  PID: $!  日志: /tmp/ai-ops-backend.log"

    echo
    echo "▎启动前端（Vite :5173）"
    cd frontend
    nohup npm run dev > /tmp/ai-ops-frontend.log 2>&1 &
    echo "  PID: $!  日志: /tmp/ai-ops-frontend.log"

    sleep 3
    echo
    echo "=========================================="
    echo "  访问地址："
    echo "  ▶ React Dashboard: http://127.0.0.1:5173"
    echo "  ▶ FastAPI 文档   : http://127.0.0.1:8000/docs"
    echo "  ▶ HTML 简版 UI    : http://127.0.0.1:8000/ui"
    echo
    echo "  停止: bash scripts/dev_servers.sh stop"
    echo "=========================================="
    ;;
  stop)
    echo "▎停止 dev servers"
    pkill -f "uvicorn ai_ops.api.main" && echo "  ✅ FastAPI 已停" || echo "  ⚠️  没有 FastAPI 在跑"
    pkill -f "vite.*frontend" && echo "  ✅ Vite 已停" || echo "  ⚠️  没有 Vite 在跑"
    ;;
  status)
    echo "▎当前进程"
    ps aux | grep -E "uvicorn ai_ops|vite.*frontend" | grep -v grep | head -10
    echo
    echo "▎端口占用"
    ss -tlnp 2>/dev/null | grep -E ":8000|:5173" | head -5
    ;;
  logs)
    echo "▎后端日志（/tmp/ai-ops-backend.log）"
    tail -20 /tmp/ai-ops-backend.log 2>/dev/null
    echo
    echo "▎前端日志（/tmp/ai-ops-frontend.log）"
    tail -20 /tmp/ai-ops-frontend.log 2>/dev/null
    ;;
  *)
    cat <<EOF
用法:
  bash scripts/dev_servers.sh start    # 启动 FastAPI + Vite
  bash scripts/dev_servers.sh stop     # 停止两个
  bash scripts/dev_servers.sh status   # 查进程 + 端口
  bash scripts/dev_servers.sh logs     # 看日志
EOF
    ;;
esac
