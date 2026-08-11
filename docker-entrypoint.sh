#!/usr/bin/env bash
#
# docker-entrypoint.sh — ai-ops-auto 容器启动入口
#
# 底层逻辑：
#   1. Schema 必须先于服务就位 —— 不跑安全 DB 初始化直接启 uvicorn
#      = "写表时才发现表不存在 / 字段缺失" → 5xx + 数据丢失。
#      `ai-ops init-db` 对空库走 Alembic；无版本业务库仅在精确匹配当前
#      metadata 时 stamp head，否则拒绝启动，避免裸 upgrade 撞表后留下半成品。
#   2. 用 entrypoint + exec "$@" 让 CMD（uvicorn）成为 PID 1 的子进程，
#      tini 把 SIGTERM 透传到 uvicorn → docker stop 能优雅退出，
#      不留僵尸进程。
#   3. 多副本部署（K8s 滚动 / Compose scale）时，并发 migration 会 race。
#      给运维一个逃生口：SKIP_MIGRATIONS=1 时跳过 alembic，
#      由 initContainer / 单独 Job 统一跑迁移。
#
# 部署详见 docs/deployment.md
# ============================================================

set -euo pipefail

echo "[entrypoint] ai-ops-auto container starting..."
echo "[entrypoint] PWD=$(pwd)"
if [[ -n "${DATABASE_URL:-}" ]]; then
    # DATABASE_URL 经常含 user:password。启动日志只确认已配置，绝不回显凭证。
    echo "[entrypoint] DATABASE_URL=<configured>"
else
    echo "[entrypoint] DATABASE_URL=<unset, will fallback to settings.database_url>"
fi

# 是否跳过迁移（K8s initContainer 模式 / 调试场景用）
if [[ "${SKIP_MIGRATIONS:-0}" == "1" ]]; then
    echo "[entrypoint] SKIP_MIGRATIONS=1, skip safe database initialization"
else
    echo "[entrypoint] running: ai-ops init-db"
    ai-ops init-db
    echo "[entrypoint] alembic current:"
    alembic current || true
fi

echo "[entrypoint] handing off to CMD: $*"
# 关键：exec 让 CMD 接管当前进程 → 信号能正确传达 → 优雅退出
exec "$@"
