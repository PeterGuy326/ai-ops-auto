#!/usr/bin/env bash
# 真实 e2e 一键上手脚本 — 用户准备好账号后跑这个。
# 不偷偷做事，每一步都打印，便于排查。

set -euo pipefail
cd "$(dirname "$0")/.."

echo "=============================================="
echo "  ai-ops-auto · e2e 一键上手"
echo "=============================================="

ok()   { echo "  ✅ $*"; }
warn() { echo "  ⚠️  $*"; }
fail() { echo "  ❌ $*"; exit 1; }

# ---------------- 1. 环境检查 ----------------
echo
echo "▎[1/7] 环境检查"

python3 -c "import sys; sys.exit(0 if sys.version_info>=(3,11) else 1)" \
  && ok "Python >= 3.11" || warn "Python < 3.11（pyproject 要求 3.11+，可能跑不动）"

if command -v google-chrome >/dev/null 2>&1 || command -v google-chrome-stable >/dev/null 2>&1; then
  ok "Chrome 已装（反风控关键）"
else
  warn "Chrome 未装。WSL 装法：sudo apt install -y wget && wget https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb && sudo apt install -y ./google-chrome-stable_current_amd64.deb"
fi

command -v ffmpeg >/dev/null && ok "ffmpeg 已装（视频去重需要）" \
  || warn "ffmpeg 未装：sudo apt install -y ffmpeg（可选；不装则视频跳过去重）"

# ---------------- 2. 本项目依赖 ----------------
echo
echo "▎[2/7] 安装本项目依赖"
pip install -e ".[dev]" 2>&1 | tail -2
ok "ai-ops-auto 主依赖已就位"

# ---------------- 3. 反风控引擎 ----------------
echo
echo "▎[3/7] 装反风控浏览器引擎（patchright drop-in）"
bash scripts/install_stealth.sh 2>&1 | tail -5

# ---------------- 4. 外部工具 ----------------
echo
echo "▎[4/7] 拉外部工具 + 装上游依赖"
bash scripts/install_external.sh 2>&1 | tail -5
if [ -d external/social-auto-upload ]; then
  pip install -r external/social-auto-upload/requirements.txt 2>&1 | tail -2
  ok "SAU 依赖装好"
fi
if [ -d external/XiaohongshuSkills ]; then
  pip install -r external/XiaohongshuSkills/requirements.txt 2>&1 | tail -2 || warn "XHS Skills requirements 缺失"
fi

# ---------------- 5. 配置 ----------------
echo
echo "▎[5/7] 初始化配置"
if [ ! -f .env ]; then
  cp .env.example .env
  fk=$(python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')
  # 用 sed 把 FERNET_KEY= 替换
  sed -i "s|^FERNET_KEY=.*|FERNET_KEY=$fk|" .env
  ok ".env 已创建 + FERNET_KEY 已生成"
else
  ok ".env 已存在（跳过覆盖）"
fi

# ---------------- 6. 数据库 ----------------
echo
echo "▎[6/7] 初始化数据库"
PYTHONPATH=src python3 scripts/init_db.py && ok "DB 表已创建"

# ---------------- 7. 自检 ----------------
echo
echo "▎[7/7] 跑自检"
bash scripts/verify.sh | tail -5

cat <<'EOF'

==============================================
  e2e 准备完成！下一步真发布操作：
==============================================

  ## 小红书发布（你需要：一台 WSLg/有桌面的环境 + 手机扫码）
  cd external/social-auto-upload
  python sau_cli.py xiaohongshu login --account acc_1  # 扫码登录
  python sau_cli.py xiaohongshu upload_note \
    --account acc_1 \
    --title "测试标题" --note "测试正文" \
    --images /absolute/path/to/test1.jpg /absolute/path/to/test2.jpg

  ## 知乎发布（自建 publisher，复用我们的反检测）
  PYTHONPATH=src python3 -c "
import asyncio
from ai_ops.publishers.zhihu import ZhihuPublisher
from ai_ops.core.schemas import PublishContent
from ai_ops.core.enums import ContentType

async def go():
    p = ZhihuPublisher()
    # 第一次：扫码登录拿 cookies
    cred = {}
    await p.login(account_id=1, credential=cred)
    print('cookies:', len(cred.get('cookies', [])))
    # 第二次起：用 cookies 直发
    r = await p.publish(1, cred, PublishContent(
        title='AI 运营自动化实践',
        body='这是一篇测试文章。\n第二段内容。',
        content_type=ContentType.LONG_ARTICLE,
    ))
    print('result:', r)

asyncio.run(go())
  "

  ## 起 API + 用 HTTP 触发
  uvicorn ai_ops.api.main:app --reload
  # 浏览器打开 http://127.0.0.1:8000/docs

EOF
