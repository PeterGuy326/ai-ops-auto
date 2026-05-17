#!/usr/bin/env bash
# 为 Camoufox（Firefox 反检测之王）适配上游工具。
#
# 设计取舍：
#   Camoufox 不是 Playwright drop-in（Chromium → Firefox 跨引擎），
#   sed 强改 launch 调用容易破坏代码（参数不兼容、上下文不同），
#   所以本脚本只做三件事：
#     1. 复制上游到 -camoufox 副本（保护原版）
#     2. 定位所有需要手动 patch 的 launch 调用
#     3. 输出 patch 模板与改造指引
#
# 真正适配工作（30 分钟手活）由用户完成；本脚本帮你定位到行。

set -uo pipefail
cd "$(dirname "$0")/.."

if ! python3 -c "import camoufox" 2>/dev/null; then
  echo "❌ camoufox 未装。先跑 bash scripts/install_stealth.sh"
  exit 1
fi

mkdir -p external

patch_one() {
  local name="$1"
  local src="external/${name}"
  local dst="external/${name}-camoufox"

  if [ ! -d "$src" ]; then
    echo "  ⚠️  $src 不存在，跳过（先 bash scripts/install_external.sh）"
    return
  fi

  echo
  echo "=============================================="
  echo "  ▎ ${name}  →  ${name}-camoufox"
  echo "=============================================="

  if [ -d "$dst" ]; then
    echo "  $dst 已存在，跳过复制（删除后重跑可重建）"
  else
    cp -r "$src" "$dst"
    rm -rf "$dst/.git"  # 切断与上游 git 历史，避免误推
    echo "  ✅ 已复制到 $dst"
  fi

  echo
  echo "  📍 需要手动 patch 的 launch 调用："
  grep -rnE "playwright\.(chromium|firefox|webkit)\.launch|async_playwright\(\)" "$dst" \
    --include="*.py" 2>/dev/null \
    | head -30 \
    | sed 's/^/    /'

  echo
  echo "  📝 改造模板（替换 chromium.launch 段落）："
  cat <<'EOF'

    # ❌ Before（原 Playwright Chromium）:
    # async with async_playwright() as p:
    #     browser = await p.chromium.launch(headless=True, channel="chrome")
    #     ctx = await browser.new_context()
    #     page = await ctx.new_page()

    # ✅ After（Camoufox AsyncCamoufox）:
    from camoufox.async_api import AsyncCamoufox
    async with AsyncCamoufox(
        headless=True,
        # 可选反检测增强：
        # proxy={"server": "http://..."},
        # geoip=True,            # 自动按 IP 匹配地理位置
        # locale="zh-CN",
        # screen={"width": 1920, "height": 1080},
        # os=["windows", "macos"],  # 随机一个真实 OS 指纹
    ) as browser:
        # AsyncCamoufox 返回的就是 BrowserContext 等价物，可以直接 new_page
        page = await browser.new_page()
        # ... 业务代码 ...

EOF
}

patch_one "social-auto-upload"
patch_one "XiaohongshuSkills"

echo
echo "=============================================="
echo "  下一步"
echo "=============================================="
cat <<'EOF'
  1. 按上面定位到的行，逐个手动改 launch 调用
  2. 配置 BROWSER_ENGINE=camoufox 时，把 wrapper 的
     external_sau_path / external_xhs_skills_path 指到 -camoufox 副本
     （或者在 publishers/* 加路径切换分支）
  3. 实测一次发布，确认 0 检测率
EOF
