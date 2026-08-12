#!/usr/bin/env bash
# Local verification entry point. It never installs dependencies or calls real platforms.

set -uo pipefail
cd "$(dirname "$0")/.."
PROJECT_ROOT="$(pwd -P)"
VERIFY_BUILD_CWD="$(mktemp -d)" || {
  echo "❌ 无法创建临时构建目录"
  exit 1
}

cleanup() {
  if [[ -n "$VERIFY_BUILD_CWD" && -d "$VERIFY_BUILD_CWD" ]]; then
    rm -rf -- "$VERIFY_BUILD_CWD"
  fi
}
trap cleanup EXIT

OK="✅"
NO="❌"
WARN="⚠️ "

pass=0
fail=0
warn=0

check() {
  local label="$1"
  shift
  echo "▸ $label"
  if "$@"; then
    echo "$OK $label"
    pass=$((pass + 1))
  else
    echo "$NO $label"
    fail=$((fail + 1))
  fi
}

soft_check() {
  local label="$1"
  shift
  if "$@" >/dev/null 2>&1; then
    echo "$OK $label"
    pass=$((pass + 1))
  else
    echo "$WARN ${label}（可选工具/依赖未就位）"
    warn=$((warn + 1))
  fi
}

skip() {
  local label="$1"
  local reason="$2"
  echo "$WARN ${label}（跳过：${reason}）"
  warn=$((warn + 1))
}

if [[ -x .venv/bin/python ]]; then
  PYTHON_BIN="$PROJECT_ROOT/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python3)"
else
  echo "$NO 未找到 Python"
  exit 1
fi

has_python_module() {
  # Run outside the repository so directories such as ./build cannot be
  # mistaken for an installed Python module of the same name.
  (cd "$VERIFY_BUILD_CWD" && "$PYTHON_BIN" -c "import $1") >/dev/null 2>&1
}

build_project() {
  # The source path is absolute and the interpreter starts from an empty temp
  # directory, avoiding local build artifacts on sys.path.
  (cd "$VERIFY_BUILD_CWD" && "$PYTHON_BIN" -m build --no-isolation \
    --outdir "$VERIFY_BUILD_CWD/dist" "$PROJECT_ROOT")
}

build_project_with_uv() {
  # uv supplies an isolated PEP 517 environment without modifying the project
  # venv.  Keep artifacts in the same disposable verification directory.
  (cd "$VERIFY_BUILD_CWD" && uv build --clear --out-dir dist "$PROJECT_ROOT")
}

smoke_installed_wheel() {
  local wheels=("$VERIFY_BUILD_CWD"/dist/*.whl)
  local wheel="${wheels[0]}"
  local smoke_venv="$VERIFY_BUILD_CWD/wheel-venv"
  local smoke_site
  local parent_sites
  local runtime_pythonpath
  local smoke_cwd="$VERIFY_BUILD_CWD/wheel-run"
  local smoke_db="$VERIFY_BUILD_CWD/wheel-smoke.db"
  local doctor_json="$smoke_cwd/doctor.json"
  local demo_json="$smoke_cwd/demo.json"
  local agent_help="$smoke_cwd/agent-help.txt"
  local download_help="$smoke_cwd/download-help.txt"
  local principal_json="$smoke_cwd/principal.json"
  local plugin_list_json="$smoke_cwd/plugins-list.json"
  local plugin_doctor_json="$smoke_cwd/plugins-doctor.json"
  local plugin_sentinel="$smoke_cwd/plugin-imported.sentinel"
  local fixture_root="$PROJECT_ROOT/tests/fixtures/publisher_plugin"
  local fixture_build_root="$VERIFY_BUILD_CWD/plugin-fixture"
  local fixture_dist="$VERIFY_BUILD_CWD/plugin-dist"
  local fixture_wheels
  local smoke_output

  if [[ ! -f "$wheel" ]]; then
    echo "未找到本轮临时目录生成的 wheel"
    return 1
  fi

  mkdir -p "$fixture_dist" "$fixture_build_root"
  cp -R "$fixture_root"/. "$fixture_build_root"/ || return 1
  if has_python_module build && has_python_module setuptools.build_meta && has_python_module wheel; then
    (cd "$VERIFY_BUILD_CWD" && "$PYTHON_BIN" -m build --no-isolation --wheel \
      --outdir "$fixture_dist" "$fixture_build_root") >/dev/null || return 1
  elif command -v uv >/dev/null 2>&1; then
    (cd "$VERIFY_BUILD_CWD" && uv build --wheel --out-dir "$fixture_dist" \
      "$fixture_build_root") >/dev/null || return 1
  else
    echo "无法构建 Publisher plugin wheel fixture"
    return 1
  fi
  fixture_wheels=("$fixture_dist"/*.whl)
  if [[ ! -f "${fixture_wheels[0]}" ]]; then
    echo "Publisher plugin wheel fixture 缺失"
    return 1
  fi

  # Install only the wheel payload into a real disposable venv so sys.prefix
  # points at the wheel's share/ resources. Runtime dependencies come from the
  # already-verified project interpreter via PYTHONPATH *after* this venv's
  # site-packages; no dependency download occurs, and an editable source install
  # cannot hide a missing package resource.
  "$PYTHON_BIN" -m venv "$smoke_venv" || return 1
  "$smoke_venv/bin/python" -m pip install \
    --disable-pip-version-check --no-deps --no-index "$wheel" >/dev/null || return 1
  "$smoke_venv/bin/python" -m pip install \
    --disable-pip-version-check --no-deps --no-index "${fixture_wheels[0]}" >/dev/null || return 1
  smoke_site="$(
    "$smoke_venv/bin/python" -c 'import site; print(site.getsitepackages()[0])'
  )" || return 1
  parent_sites="$(
    "$PYTHON_BIN" -c '
import os
import site

paths = [*site.getsitepackages(), site.getusersitepackages()]
print(os.pathsep.join(path for path in paths if path))
'
  )" || return 1
  runtime_pythonpath="$smoke_site${parent_sites:+:$parent_sites}"
  mkdir -p "$smoke_cwd"

  smoke_output="$({
    cd "$smoke_cwd" && env \
      DATABASE_URL="sqlite:///$smoke_db" \
      DATA_DIR="$smoke_cwd/data" \
      PYTHONPATH="$runtime_pythonpath" \
      "$smoke_venv/bin/ai-ops" init-db
  } 2>&1)" || {
    echo "$smoke_output"
    return 1
  }
  if [[ "$smoke_output" != *"OK: db initialized"* ]]; then
    echo "$smoke_output"
    return 1
  fi

  smoke_output="$({
    cd "$smoke_cwd" && env \
      DATABASE_URL="sqlite:///$smoke_db" \
      DATA_DIR="$smoke_cwd/data" \
      PYTHONPATH="$runtime_pythonpath" \
      "$smoke_venv/bin/ai-ops" doctor --json
  } 2>&1)" || {
    echo "$smoke_output"
    return 1
  }
  printf '%s\n' "$smoke_output" >"$doctor_json"

  smoke_output="$({
    cd "$smoke_cwd" && env \
      AI_OPS_PLUGIN_SENTINEL="$plugin_sentinel" \
      DATABASE_URL="sqlite:///$smoke_db" \
      DATA_DIR="$smoke_cwd/data" \
      PYTHONPATH="$runtime_pythonpath" \
      "$smoke_venv/bin/ai-ops" plugins list --json
  } 2>&1)" || {
    echo "$smoke_output"
    return 1
  }
  printf '%s\n' "$smoke_output" >"$plugin_list_json"
  if [[ -e "$plugin_sentinel" ]]; then
    echo "plugins list imported disabled third-party code"
    return 1
  fi

  smoke_output="$({
    cd "$smoke_cwd" && env \
      AI_OPS_PLUGIN_SENTINEL="$plugin_sentinel" \
      PUBLISHER_PLUGIN_ALLOWLIST='["ai-ops-auto-fixture-plugin:fixture.zhihu"]' \
      DATABASE_URL="sqlite:///$smoke_db" \
      DATA_DIR="$smoke_cwd/data" \
      PYTHONPATH="$runtime_pythonpath" \
      "$smoke_venv/bin/ai-ops" plugins doctor --json
  } 2>&1)" || {
    echo "$smoke_output"
    return 1
  }
  printf '%s\n' "$smoke_output" >"$plugin_doctor_json"
  if [[ ! -f "$plugin_sentinel" ]]; then
    echo "plugins doctor did not load the selected fixture"
    return 1
  fi

  smoke_output="$({
    cd "$smoke_cwd" && env \
      DATABASE_URL="sqlite:///$smoke_db" \
      DATA_DIR="$smoke_cwd/data" \
      PYTHONPATH="$runtime_pythonpath" \
      "$smoke_venv/bin/ai-ops" demo --json
  } 2>&1)" || {
    echo "$smoke_output"
    return 1
  }
  printf '%s\n' "$smoke_output" >"$demo_json"

  smoke_output="$({
    cd "$smoke_cwd" && env \
      DATABASE_URL="sqlite:///$smoke_db" \
      DATA_DIR="$smoke_cwd/data" \
      PYTHONPATH="$runtime_pythonpath" \
      "$smoke_venv/bin/ai-ops" agent --help
  } 2>&1)" || {
    echo "$smoke_output"
    return 1
  }
  printf '%s\n' "$smoke_output" >"$agent_help"

  smoke_output="$({
    cd "$smoke_cwd" && env \
      DATABASE_URL="sqlite:///$smoke_db" \
      DATA_DIR="$smoke_cwd/data" \
      PYTHONPATH="$runtime_pythonpath" \
      "$smoke_venv/bin/ai-ops" agent download-approval-asset --help
  } 2>&1)" || {
    echo "$smoke_output"
    return 1
  }
  printf '%s\n' "$smoke_output" >"$download_help"

  smoke_output="$({
    cd "$smoke_cwd" && env \
      DATABASE_URL="sqlite:///$smoke_db" \
      DATA_DIR="$smoke_cwd/data" \
      PYTHONPATH="$runtime_pythonpath" \
      "$smoke_venv/bin/ai-ops" gen-principal-token
  } 2>&1)" || {
    echo "$smoke_output"
    return 1
  }
  printf '%s\n' "$smoke_output" >"$principal_json"

  (
    cd "$smoke_cwd" && env \
      DATABASE_URL="sqlite:///$smoke_db" \
      DATA_DIR="$smoke_cwd/data" \
      AI_OPS_MCP_COMMAND="$smoke_venv/bin/ai-ops-mcp" \
      PYTHONPATH="$runtime_pythonpath" \
      "$smoke_venv/bin/python" -c '
import asyncio
import json
import hashlib
import os
from pathlib import Path
import sys

import ai_ops
from mcp import Client, StdioServerParameters, stdio_client
from ai_ops.publishers import PublisherPluginManifest

assert PublisherPluginManifest
assert "ai_ops.publishers.registry" not in sys.modules

from sqlalchemy import create_engine, inspect
from ai_ops.agent_contract.cli_commands import agent_app
from ai_ops.api.main import app
from ai_ops.core.db import get_code_alembic_head, get_db_alembic_head

package_path = Path(ai_ops.__file__).resolve()
assert package_path.is_relative_to(Path(sys.prefix).resolve()), package_path
assert get_code_alembic_head() == get_db_alembic_head() == "e8b4c6d2a901"
database_inspector = inspect(create_engine(os.environ["DATABASE_URL"]))
tables = set(database_inspector.get_table_names())
assert {
    "publication_plans",
    "approval_requests",
    "agent_operations",
    "metrics_collection_tasks",
} <= tables
metric_columns = {column["name"] for column in database_inspector.get_columns("metrics")}
assert "collection_task_id" in metric_columns
expected_v1_routes = {
    ("POST", "/v1/contents"),
    ("POST", "/v1/publication-plans"),
    ("POST", "/v1/approval-requests"),
    ("GET", "/v1/approval-requests/{approval_id}"),
    ("GET", "/v1/approval-requests/{approval_id}/assets/{asset_id}"),
    ("POST", "/v1/approvals/{approval_id}/decision"),
    ("POST", "/v1/publication-plans/{plan_id}/schedule"),
    ("GET", "/v1/jobs/{job_id}"),
    ("POST", "/v1/jobs/{job_id}/metrics-collections"),
    ("POST", "/v1/performance-reviews"),
}
assert len(expected_v1_routes) == 10
shipped_v1_routes = {
    (method.upper(), path)
    for path, operations in app.openapi()["paths"].items()
    if path.startswith("/v1/")
    for method in operations
    if method.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE"}
}
assert shipped_v1_routes == expected_v1_routes
download_contract = app.openapi()["paths"][
    "/v1/approval-requests/{approval_id}/assets/{asset_id}"
]["get"]
assert set(download_contract["responses"]["200"]["content"]) == {
    "application/octet-stream"
}
assert download_contract["security"] == [{"HTTPBearer": []}]
templates = package_path.parent / "api" / "templates"
required_templates = {
    "base.html",
    "dashboard.html",
    "login.html",
    "list.html",
    "article_detail.html",
    "account_detail.html",
}
assert all((templates / name).is_file() for name in required_templates)

doctor = json.loads(Path("doctor.json").read_text(encoding="utf-8"))
assert doctor["schema_version"] == 1
assert doctor["ok"] is True

plugin_list = json.loads(Path("plugins-list.json").read_text(encoding="utf-8"))
assert plugin_list["ok"] is True
assert plugin_list["code_loaded"] is False
plugins_by_selector = {item["selector"]: item for item in plugin_list["plugins"]}
assert plugins_by_selector["ai-ops-auto-fixture-plugin:fixture.zhihu"]["status"] == "disabled"

plugin_doctor = json.loads(Path("plugins-doctor.json").read_text(encoding="utf-8"))
assert plugin_doctor["ok"] is True
assert plugin_doctor["code_loaded"] is True
assert plugin_doctor["summary"] == {"enabled": 1, "invalid": 0, "valid": 1}

demo = json.loads(Path("demo.json").read_text(encoding="utf-8"))
assert demo["ok"] is True
assert demo["exit_code"] == 0
assert demo["synthetic"] is True
assert demo["offline"] is True
assert demo["credentials_used"] is False
assert demo["external_calls"] == 0
assert demo["review"]["passed"] is True

principal = json.loads(Path("principal.json").read_text(encoding="utf-8"))
assert principal["schema_version"] == 1
assert principal["token"].startswith("aop_")
assert hashlib.sha256(principal["token"].encode()).hexdigest() == principal["token_sha256"]

expected_agent_commands = {
    "stage-content",
    "plan-publication",
    "request-approval",
    "get-approval",
    "download-approval-asset",
    "decide-approval",
    "schedule",
    "get-job-status",
    "collect-metrics",
    "review-performance",
}
assert len(expected_agent_commands) == 10
shipped_agent_commands = {command.name for command in agent_app.registered_commands}
assert shipped_agent_commands == expected_agent_commands
help_text = Path("agent-help.txt").read_text(encoding="utf-8")
for command in expected_agent_commands:
    assert command in help_text
download_help = Path("download-help.txt").read_text(encoding="utf-8")
for required in {"approval_id", "asset_id", "--output"}:
    assert required in download_help

expected_mcp_tools = {
    "stage_content",
    "plan_publication",
    "request_approval",
    "schedule",
    "get_job_status",
    "collect_metrics",
    "review_performance",
}


async def verify_installed_mcp_stdio() -> None:
    server = StdioServerParameters(
        command=os.environ["AI_OPS_MCP_COMMAND"],
        env={
            "DATABASE_URL": os.environ["DATABASE_URL"],
            "DATA_DIR": os.environ["DATA_DIR"],
            "PYTHONPATH": os.environ["PYTHONPATH"],
        },
    )
    for mode in ("auto", "legacy"):
        async with Client(
            stdio_client(server),
            read_timeout_seconds=10,
            mode=mode,
        ) as client:
            tools = await client.list_tools()
        assert {tool.name for tool in tools.tools} == expected_mcp_tools
        for tool in tools.tools:
            assert tool.input_schema["type"] == "object"
            assert tool.input_schema["additionalProperties"] is False
            assert tool.output_schema["type"] == "object"
            assert len(tool.output_schema["oneOf"]) == 2


asyncio.run(verify_installed_mcp_stdio())
'
  )
}

echo "=========================================="
echo "  ai-ops-auto · 工程自检"
echo "  Python: $PYTHON_BIN"
echo "=========================================="

echo
echo "▎结构与配置"
check "Python >= 3.11" "$PYTHON_BIN" -c \
  'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'
check "目录: src/ai_ops/" test -d src/ai_ops
check "目录: tests/" test -d tests
check "目录: docs/" test -d docs
check "文件: pyproject.toml" test -f pyproject.toml
check "文件: .env.example" test -f .env.example
check "文件: LICENSE" test -f LICENSE
check "文件: SECURITY.md" test -f SECURITY.md
check "文件: docs/platform-capabilities.md" test -f docs/platform-capabilities.md
check "文件: GitHub CI" test -f .github/workflows/ci.yml

echo
echo "▎Python 语法与导入"
check "compileall src/" "$PYTHON_BIN" -m compileall -q src/
check "compileall tests/" "$PYTHON_BIN" -m compileall -q tests/
check "import ai_ops" env PYTHONPATH=src "$PYTHON_BIN" -c "import ai_ops"
check "import ai_ops.api.main" env PYTHONPATH=src "$PYTHON_BIN" -c "import ai_ops.api.main"
check "import ai_ops.scheduler.worker" env PYTHONPATH=src "$PYTHON_BIN" -c \
  "import ai_ops.scheduler.worker"

echo
echo "▎后端质量门禁"
if has_python_module pytest; then
  check "pytest" "$PYTHON_BIN" -m pytest -q --disable-warnings
else
  echo "$NO pytest（缺少必需的 dev extra：pip install -e '.[dev]'）"
  fail=$((fail + 1))
fi

if has_python_module ruff; then
  check "ruff check" "$PYTHON_BIN" -m ruff check .
else
  echo "$NO ruff check（缺少必需的 dev extra：pip install -e '.[dev]'）"
  fail=$((fail + 1))
fi

echo
echo "▎前端质量门禁"
if [[ -f frontend/package.json ]]; then
  if ! command -v npm >/dev/null 2>&1; then
    echo "$NO frontend lint/build（npm 不可用）"
    fail=$((fail + 1))
  elif [[ ! -d frontend/node_modules ]]; then
    echo "$NO frontend lint/build（依赖未安装：cd frontend && npm ci）"
    fail=$((fail + 1))
  else
    check "frontend lint" npm --prefix frontend run lint
    check "frontend build" npm --prefix frontend run build
  fi
fi

echo
echo "▎Python 打包"
package_built=false
if has_python_module build && has_python_module setuptools.build_meta; then
  if build_project; then
    echo "$OK wheel + sdist"
    pass=$((pass + 1))
    package_built=true
  else
    echo "$NO wheel + sdist"
    fail=$((fail + 1))
  fi
elif command -v uv >/dev/null 2>&1; then
  if build_project_with_uv; then
    echo "$OK wheel + sdist (uv)"
    pass=$((pass + 1))
    package_built=true
  else
    echo "$NO wheel + sdist (uv)"
    fail=$((fail + 1))
  fi
else
  echo "$NO wheel + sdist（build/setuptools 与 uv 均不可用）"
  fail=$((fail + 1))
fi
if [[ "$package_built" == true ]]; then
  check "installed wheel + Alembic/doctor/demo/MCP smoke" smoke_installed_wheel
fi

echo
echo "▎可选外部工具"
soft_check "external/social-auto-upload" test -f external/social-auto-upload/sau_cli.py
soft_check "external/XiaohongshuSkills" \
  test -f external/XiaohongshuSkills/scripts/publish_pipeline.py
soft_check "external/MoneyPrinterTurbo" test -f external/MoneyPrinterTurbo/app/router.py
soft_check "patchright" "$PYTHON_BIN" -c "import patchright"
soft_check "camoufox" "$PYTHON_BIN" -c "import camoufox"

echo
echo "=========================================="
echo "  汇总: $OK 通过 $pass · $WARN 警告 $warn · $NO 失败 $fail"
echo "=========================================="

[[ "$fail" -eq 0 ]]
