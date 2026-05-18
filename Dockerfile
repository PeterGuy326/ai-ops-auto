# syntax=docker/dockerfile:1.6
#
# ai-ops-auto 生产镜像
#
# 底层逻辑：
#   1. python:3.11-slim — pyproject 要求 3.11+，slim 镜像 ~50MB，最小可用
#   2. entrypoint 跑 alembic upgrade head 再启 uvicorn —— schema 永不漏跑
#   3. data/ 暴露为 VOLUME —— SQLite 文件 + 素材产物必须持久化
#   4. 不在镜像里装 playwright 浏览器（~500MB） + 不装 dev / llm-* 可选依赖
#      —— 真发布场景由运维按需 `docker exec ... playwright install chromium`
#         或 `pip install -e .[stealth-pro]` 加固
#
# 部署详见 docs/deployment.md
# ============================================================

FROM python:3.11-slim AS base

# 系统依赖：
#   build-essential —— cryptography / Pillow 等 C 扩展构建时可能需要
#   curl —— 健康检查 + 运维自检用
#   ca-certificates —— httpx / cryptography 走 HTTPS 必备
#   tini —— PID 1 信号转发，让 docker stop 能优雅退出
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        ca-certificates \
        tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先拷贝依赖元数据 → 利用 Docker layer cache：源码变了不重装依赖
COPY pyproject.toml README.md ./
COPY src/ ./src/
COPY alembic/ ./alembic/
COPY alembic.ini ./
COPY scripts/ ./scripts/

# 装本项目 + 必要运行时依赖
# 不装 [dev]（pytest 等）/ [llm-*]（按需）/ [stealth-*]（按需）
# 运维需要哪个档自己 docker exec 进去 `pip install -e .[stealth-pro]`
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -e .

# 入口脚本
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# 数据卷：SQLite 文件、素材产物、视频产物都在这里 → 必须持久化
VOLUME ["/app/data"]

EXPOSE 8000

# tini 接管 PID 1 → 转发信号给 entrypoint → entrypoint exec uvicorn
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/docker-entrypoint.sh"]

# CMD 是 entrypoint exec "$@" 接管的命令；运维想覆盖 uvicorn 参数直接 docker run ... <cmd>
CMD ["uvicorn", "ai_ops.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
