#!/usr/bin/env bash
# 工程自检脚本 — 零成本验证骨架是否真的能站立。
# 不安装依赖、不调外部服务，纯结构+语法+导入检查。

set -uo pipefail
cd "$(dirname "$0")/.."

OK="✅"
NO="❌"
W="⚠️ "

pass=0
fail=0
warn=0

check() {
  local label="$1"; shift
  if "$@" >/dev/null 2>&1; then
    echo "$OK $label"
    pass=$((pass + 1))
  else
    echo "$NO $label"
    fail=$((fail + 1))
  fi
}

soft_check() {
  local label="$1"; shift
  if "$@" >/dev/null 2>&1; then
    echo "$OK $label"
    pass=$((pass + 1))
  else
    echo "$W  $label（外部工具未拉/依赖未装，非致命）"
    warn=$((warn + 1))
  fi
}

echo "=========================================="
echo "  ai-ops-auto · 工程自检"
echo "=========================================="

echo
echo "▎结构检查"
check "目录: src/ai_ops/"           test -d src/ai_ops
check "目录: src/ai_ops/core/"      test -d src/ai_ops/core
check "目录: src/ai_ops/publishers/" test -d src/ai_ops/publishers
check "目录: src/ai_ops/video/"     test -d src/ai_ops/video
check "目录: tests/"                test -d tests
check "目录: scripts/"              test -d scripts
check "目录: docs/"                 test -d docs
check "文件: pyproject.toml"        test -f pyproject.toml
check "文件: .env.example"          test -f .env.example
check "文件: README.md"             test -f README.md
check "文件: docs/architecture.md"  test -f docs/architecture.md
check "文件: docs/external-tools.md" test -f docs/external-tools.md

echo
echo "▎语法检查（不依赖第三方库）"
check "Python compileall src/"      python3 -m compileall -q src/
check "Python compileall tests/"    python3 -m compileall -q tests/

echo
echo "▎关键模块 AST 解析"
for f in \
  src/ai_ops/core/enums.py \
  src/ai_ops/core/models.py \
  src/ai_ops/core/schemas.py \
  src/ai_ops/publishers/base.py \
  src/ai_ops/publishers/social_auto_upload.py \
  src/ai_ops/publishers/xhs_skills.py \
  src/ai_ops/publishers/zhihu.py \
  src/ai_ops/publishers/toutiao.py \
  src/ai_ops/publishers/github_pages.py \
  src/ai_ops/publishers/registry.py \
  src/ai_ops/scheduler/metrics.py \
  src/ai_ops/content/heat_engine.py \
  src/ai_ops/video/base.py \
  src/ai_ops/video/money_printer.py \
  src/ai_ops/scheduler/worker.py \
  src/ai_ops/accounts/manager.py \
  src/ai_ops/content/asset_processor.py \
  src/ai_ops/runtime/playwright_factory.py \
  src/ai_ops/runtime/browser_engine.py \
  src/ai_ops/api/main.py \
  src/ai_ops/cli.py
do
  check "AST: $f" python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "$f"
done

echo
echo "▎外部工具就位"
soft_check "external/social-auto-upload"   test -f external/social-auto-upload/sau_cli.py
soft_check "external/XiaohongshuSkills"    test -f external/XiaohongshuSkills/scripts/publish_pipeline.py
soft_check "external/MoneyPrinterTurbo"    test -f external/MoneyPrinterTurbo/app/router.py

echo
echo "▎依赖就位（若 pip install -e . 已执行）"
soft_check "依赖: fastapi"            python3 -c "import fastapi"
soft_check "依赖: sqlalchemy"         python3 -c "import sqlalchemy"
soft_check "依赖: pydantic"           python3 -c "import pydantic"
soft_check "依赖: apscheduler"        python3 -c "import apscheduler"
soft_check "依赖: cryptography"       python3 -c "from cryptography.fernet import Fernet"
soft_check "依赖: pillow"             python3 -c "from PIL import Image"
soft_check "依赖: patchright (反检测)" python3 -c "import patchright"
soft_check "依赖: camoufox (反检测)"   python3 -c "import camoufox"

echo
echo "▎一键导入（项目本身）"
PYTHONPATH=src soft_check "import ai_ops"                  python3 -c "import ai_ops"
PYTHONPATH=src soft_check "import ai_ops.core.enums"       python3 -c "import ai_ops.core.enums"
PYTHONPATH=src soft_check "import ai_ops.publishers.base"  python3 -c "import ai_ops.publishers.base"

echo
echo "▎前端（frontend/）"
soft_check "Vite 项目存在"          test -f frontend/package.json
soft_check "node_modules 已装"      test -d frontend/node_modules
soft_check "shadcn components.json" test -f frontend/components.json
soft_check "shadcn ui 组件目录"     test -d frontend/src/components/ui
soft_check "Dashboard 页面"         test -f frontend/src/pages/dashboard.tsx
soft_check "构建产物 (frontend/dist)" test -d frontend/dist

echo
echo "=========================================="
echo "  汇总: $OK 通过 $pass · $W 警告 $warn · $NO 失败 $fail"
echo "=========================================="

[ "$fail" -eq 0 ] && exit 0 || exit 1
