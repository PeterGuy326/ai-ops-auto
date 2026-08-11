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
  (cd "$VERIFY_BUILD_CWD" && "$PYTHON_BIN" -m build --no-isolation "$PROJECT_ROOT")
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
  local smoke_output

  if [[ ! -f "$wheel" ]]; then
    echo "未找到本轮临时目录生成的 wheel"
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

  (
    cd "$smoke_cwd" && env \
      DATABASE_URL="sqlite:///$smoke_db" \
      DATA_DIR="$smoke_cwd/data" \
      PYTHONPATH="$runtime_pythonpath" \
      "$smoke_venv/bin/python" -c '
from pathlib import Path
import sys

import ai_ops
from ai_ops.core.db import get_code_alembic_head, get_db_alembic_head

package_path = Path(ai_ops.__file__).resolve()
assert package_path.is_relative_to(Path(sys.prefix).resolve()), package_path
assert get_code_alembic_head() == get_db_alembic_head()
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
  skip "pytest" "未安装 dev extra：pip install -e '.[dev]'"
fi

if has_python_module ruff; then
  check "ruff check" "$PYTHON_BIN" -m ruff check .
else
  skip "ruff check" "未安装 dev extra：pip install -e '.[dev]'"
fi

echo
echo "▎前端质量门禁"
if ! command -v npm >/dev/null 2>&1; then
  skip "frontend lint/build" "npm 不可用"
elif [[ ! -d frontend/node_modules ]]; then
  skip "frontend lint/build" "依赖未安装：cd frontend && npm ci"
else
  check "frontend lint" npm --prefix frontend run lint
  check "frontend build" npm --prefix frontend run build
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
  skip "wheel + sdist" "build/setuptools 与 uv 均不可用"
fi
if [[ "$package_built" == true ]]; then
  check "installed wheel + Alembic smoke" smoke_installed_wheel
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
