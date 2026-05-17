#!/usr/bin/env bash
# 反风控浏览器引擎安装。
# 按 settings.browser_engine 选择对应组件。

set -uo pipefail
cd "$(dirname "$0")/.."

echo "=========================================="
echo "  反风控浏览器引擎安装"
echo "=========================================="

# 1. patchright (drop-in for Playwright Chromium)
echo
echo "▎安装 patchright (drop-in 反检测 Chromium)"
pip install patchright
patchright install chromium

# 2. Camoufox (Firefox 反检测之王，0% 检测率)
# 不是 Playwright drop-in，需要业务代码显式 import camoufox
echo
echo "▎安装 camoufox (Firefox 反检测，可选)"
pip install -U camoufox[geoip]
python -m camoufox fetch || echo "  ⚠️  camoufox fetch 失败可稍后手动重试"

echo
echo "=========================================="
echo "  安装完成"
echo "=========================================="
echo
echo "下一步："
echo "  1. 在 .env 配置 BROWSER_ENGINE：(默认 playwright_chrome_channel)"
echo "       playwright_chrome_channel  零依赖，SAU 上游默认"
echo "       patchright                 drop-in 反检测，立刻见效"
echo "       camoufox                   8k⭐ Firefox 反检测之王（需要 publishers/xhs_camoufox.py 适配）"
echo "  2. 在 .env 配置 BROWSER_PROXY (强烈推荐每账号独立 IP)"
echo "  3. 运行 bash scripts/verify.sh 自检"
