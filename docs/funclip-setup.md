# FunClip 集成 & 部署指南

> 视频智能剪辑能力 —— 阿里达摩院 / 通义实验室开源的 FunClip（基于 FunASR Paraformer）。
> 项目通过 **外置 + subprocess CLI** 模式集成，依赖完全隔离，主项目 venv 不受影响。

## 1. 整体架构

```
┌──────────────────────────────────┐
│  ai-ops-auto (主项目 venv)        │
│  src/ai_ops/video/clipper/       │
│    funclip.py  ←—— 薄 wrapper    │
└──────────┬───────────────────────┘
           │ subprocess
           ▼
┌──────────────────────────────────┐
│  external/FunClip (独立 venv)    │
│  funclip/videoclipper.py         │
│   - stage 1: ASR → SRT           │
│   - stage 2: dest_text → 切片    │
│  + FunASR / Paraformer / torch   │
└──────────────────────────────────┘
```

- 主项目代码：`src/ai_ops/video/clipper/funclip.py`
- 抽象基类：`src/ai_ops/video/clipper_base.py`（与 `VideoEngineBase` 正交）
- 配置入口：`Settings.funclip_*`（`src/ai_ops/config.py`）

## 2. 安装步骤

### 2.1 Clone FunClip 到 external/

```bash
mkdir -p external && cd external
git clone https://github.com/modelscope/FunClip.git
cd FunClip
```

### 2.2 为 FunClip 创建独立 venv（强烈推荐）

FunClip 依赖 `torch`、`funasr`、`modelscope`，体积 GB 级，与主项目的 playwright/camoufox 共存极易出现 numpy/protobuf 版本冲突。**必须独立 venv**。

```bash
# 在 external/FunClip 目录下
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# ffmpeg 系统包（Ubuntu/Debian）
sudo apt-get install -y ffmpeg
```

### 2.3 模型预热（首次跑会自动下载 ~1.5GB）

```bash
# 用一段短视频试跑一次 stage 1，把模型缓存到 ~/.cache/modelscope/
python funclip/videoclipper.py --stage 1 --file <短视频路径> --output_dir ./tmp
```

## 3. 主项目配置

在主项目根目录 `.env`（或环境变量）里写：

```dotenv
# FunClip 仓库路径（默认 ./external/FunClip）
FUNCLIP_PATH=./external/FunClip
# FunClip venv 的 python
FUNCLIP_PYTHON=./external/FunClip/.venv/bin/python
# 子进程超时（秒），长视频转写慢，默认 1800
FUNCLIP_TIMEOUT_SECONDS=1800
# 切片产物根目录
FUNCLIP_OUTPUT_ROOT=./data/clips
```

## 4. 使用示例

```python
import asyncio
from ai_ops.video import FunClipClipper
from ai_ops.core.schemas import ClipRequest, ClipSegment

async def demo():
    clipper = FunClipClipper()
    assert await clipper.health(), "FunClip not ready, check FUNCLIP_PATH"

    result = await clipper.clip(
        ClipRequest(
            input_video="/path/to/long.mp4",
            segments=[
                ClipSegment(dest_text="我们把它跟乡村振兴去结合起来", start_ost_ms=0, end_ost_ms=500),
                ClipSegment(dest_text="这个技术的核心是什么呢", start_ost_ms=0, end_ost_ms=0),
            ],
            output_dir="./data/clips",
        )
    )
    for c in result.clips:
        print(c.video_path, c.dest_text)
```

## 5. 常见问题

| 现象 | 排查 |
|------|------|
| `health()` 返回 False | 检查 `FUNCLIP_PATH` 下是否有 `funclip/videoclipper.py`，`FUNCLIP_PYTHON` 是否存在 |
| stage 1 报模型下载失败 | 国内网络去 ModelScope 拉，或 `export MODELSCOPE_CACHE=...` 指本地缓存 |
| stage 2 报 `dest_text not found` | 文本必须严格命中 SRT 里的某条 cue，建议先 transcribe 拿 cues 后挑文本 |
| TimeoutError | 调大 `FUNCLIP_TIMEOUT_SECONDS`，30 min 视频建议给 3600+ |

## 6. 后续接入点（不在本次范围）

- `pipeline/`：长视频 → FunClip 切片 → 自动喂 publishers 多平台分发
- `api/`：POST /clip 端点，给前端 UI 用
- `core/dispatch.py`：按平台时长偏好（抖音 60s、视频号 90s、B 站 不限）自动挑切片
