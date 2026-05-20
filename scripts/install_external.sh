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
    if ! git -c http.postBuffer=524288000 clone --filter=blob:none --depth=1 "${repo}" "external/${target}"; then
      # WSL/弱网下 GitHub HTTPS 可能持续 TLS 中断，最后退到 kkgithub 镜像
      echo "[retry ] 仍失败，改用 kkgithub.com 镜像"
      rm -rf "external/${target}"
      local mirror="${repo/github.com/kkgithub.com}"
      git -c http.version=HTTP/1.1 -c http.postBuffer=524288000 clone --depth=1 "${mirror}" "external/${target}"
    fi
  fi
}

# FunClip 上游 bug：stage 2 单独跑时新建 VideoClipper(None) 漏设 .lang，
# 导致 video_clip() 里 `self.lang == 'en'` 触发 AttributeError。
# CLI 两阶段分离调用必踩，这里幂等补一行 `audio_clipper.lang = lang`。
patch_funclip_stage2_lang() {
  local f="external/FunClip/funclip/videoclipper.py"
  [[ -f "$f" ]] || { echo "  ⚠️  $f 不存在，跳过 FunClip patch"; return; }
  if grep -q "audio_clipper.lang = lang" "$f"; then
    echo "  ✅ FunClip stage2 lang patch 已在位"
    return
  fi
  python3 - "$f" <<'PYEOF'
import sys
f = sys.argv[1]
src = open(f, encoding="utf-8").read()
needle = "        audio_clipper = VideoClipper(None)\n"
if needle in src:
    open(f, "w", encoding="utf-8").write(
        src.replace(needle, needle + "        audio_clipper.lang = lang\n", 1)
    )
    print("  ✅ FunClip stage2 lang patch 已应用")
else:
    print("  ⚠️  未找到 patch 锚点，FunClip 上游可能已改，请手动核对 videoclipper.py")
PYEOF
}

# 主力发布器（覆盖抖音、小红书、视频号、快手、B站、TikTok、YouTube）
clone_or_pull "https://github.com/dreammis/social-auto-upload.git" "social-auto-upload"

# 小红书专项加固
clone_or_pull "https://github.com/white0dew/XiaohongshuSkills.git" "XiaohongshuSkills"

# 视频自动生成（主力，57k⭐）— 仓库较大，强制 blob:none
clone_or_pull "https://github.com/harry0703/MoneyPrinterTurbo.git" "MoneyPrinterTurbo" "--filter=blob:none"

# 视频解说类（备选）
# clone_or_pull "https://github.com/linyqh/NarratoAI.git" "NarratoAI"

# 智能视频剪辑（FunClip，阿里达摩院/ModelScope）— ASR 转写 + 文字稿切片
clone_or_pull "https://github.com/modelscope/FunClip.git" "FunClip"
patch_funclip_stage2_lang

echo
echo "外部工具就位。下一步："
echo "  1. social-auto-upload  : pip install -r external/social-auto-upload/requirements.txt"
echo "                           playwright install chromium"
echo "  2. MoneyPrinterTurbo   : pip install -r external/MoneyPrinterTurbo/requirements.txt"
echo "                           cp external/MoneyPrinterTurbo/config.example.toml external/MoneyPrinterTurbo/config.toml"
echo "                           bash external/MoneyPrinterTurbo/webui.sh  (或 uvicorn 跑 app)"
echo "  3. FunClip             : 独立 venv（依赖体积大，勿混主项目）："
echo "                           python3 -m venv external/FunClip/.venv"
echo "                           external/FunClip/.venv/bin/pip install -r external/FunClip/requirements.txt \\"
echo "                             -i https://pypi.tuna.tsinghua.edu.cn/simple"
echo "                           ffmpeg 用 venv 自带 imageio-ffmpeg，无需 sudo——详见 docs/funclip-setup.md"
echo "  4. 配置 .env 的 EXTERNAL_*_PATH / EXTERNAL_*_URL / MPT_API_KEY / FUNCLIP_*"
