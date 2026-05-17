#!/usr/bin/env bash
# 拉取所有外部开源工具到 external/ 目录。
# 这些是发布器和视频引擎的真正实现，本项目只做编排。

set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p external

# 加固：大仓库 + WSL/弱网环境下 GnuTLS 在传大包时可能崩
# - http.postBuffer 调大到 500MB
# - --filter=blob:none 部分克隆，blob 按需 lazy fetch
# - --depth=1 浅克隆
export GIT_HTTP_POSTBUFFER=524288000

clone_or_pull() {
  local repo="$1"
  local target="$2"
  local extra_args="${3:-}"

  if [[ -d "external/${target}/.git" ]] && git -C "external/${target}" rev-parse HEAD >/dev/null 2>&1; then
    echo "[update] external/${target}"
    git -C "external/${target}" pull --rebase --autostash || true
    return
  fi

  echo "[clone ] ${repo} -> external/${target}"
  rm -rf "external/${target}"
  # 第一次尝试普通 shallow
  if ! git -c http.postBuffer=524288000 clone --depth=1 ${extra_args} "${repo}" "external/${target}"; then
    echo "[retry ] 第一次失败，改用 --filter=blob:none 部分克隆"
    rm -rf "external/${target}"
    git -c http.postBuffer=524288000 clone --filter=blob:none --depth=1 "${repo}" "external/${target}"
  fi
}

# 主力发布器（覆盖抖音、小红书、视频号、快手、B站、TikTok、YouTube）
clone_or_pull "https://github.com/dreammis/social-auto-upload.git" "social-auto-upload"

# 小红书专项加固
clone_or_pull "https://github.com/white0dew/XiaohongshuSkills.git" "XiaohongshuSkills"

# 视频自动生成（主力，57k⭐）— 仓库较大，强制 blob:none
clone_or_pull "https://github.com/harry0703/MoneyPrinterTurbo.git" "MoneyPrinterTurbo" "--filter=blob:none"

# 视频解说类（备选）
# clone_or_pull "https://github.com/linyqh/NarratoAI.git" "NarratoAI"

echo
echo "外部工具就位。下一步："
echo "  1. social-auto-upload  : pip install -r external/social-auto-upload/requirements.txt"
echo "                           playwright install chromium"
echo "  2. MoneyPrinterTurbo   : pip install -r external/MoneyPrinterTurbo/requirements.txt"
echo "                           cp external/MoneyPrinterTurbo/config.example.toml external/MoneyPrinterTurbo/config.toml"
echo "                           bash external/MoneyPrinterTurbo/webui.sh  (或 uvicorn 跑 app)"
echo "  3. 配置 .env 的 EXTERNAL_*_PATH / EXTERNAL_*_URL / MPT_API_KEY"
