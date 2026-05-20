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

一键脚本：`bash scripts/install_external.sh` 会 clone FunClip 并自动打 stage2 patch（见 2.4）。
之后按下面 2.2 建 venv、装依赖。

### 2.1 Clone FunClip 到 external/

```bash
mkdir -p external && cd external
git clone https://github.com/modelscope/FunClip.git
# WSL/弱网下 GitHub HTTPS 持续 TLS 中断时，改用镜像：
# git -c http.version=HTTP/1.1 clone https://kkgithub.com/modelscope/FunClip.git
cd FunClip
```

### 2.2 为 FunClip 创建独立 venv（强烈推荐）

FunClip 依赖 `torch`、`funasr`、`modelscope`，体积约 5.6 GB，与主项目的 playwright/camoufox 共存极易出现 numpy/protobuf 版本冲突。**必须独立 venv**。

```bash
# 在 external/FunClip 目录下
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

### 2.3 ffmpeg —— 用 venv 自带的 imageio-ffmpeg，无需 sudo

FunClip 切片走 moviepy，依赖 `ffmpeg`。装 requirements 时已顺带装上 `imageio-ffmpeg`，它**自带一个静态编译的全功能 ffmpeg 二进制**，把它软链进 PATH 即可，不需要 `sudo apt`：

```bash
FF=$(.venv/bin/python -c "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())")
mkdir -p ~/.local/bin && ln -sf "$FF" ~/.local/bin/ffmpeg
which ffmpeg   # 应输出 ~/.local/bin/ffmpeg
```

> 注：`~/.local/bin` 需在 `PATH` 中。该 symlink 指向 venv 内的二进制，删 venv 会失链——重装 venv 后重跑此步即可。

### 2.4 FunClip 上游 patch（stage 2 lang bug）

FunClip 的 `funclip/videoclipper.py` 有个 bug：CLI `--stage 2` 单独调用时新建
`VideoClipper(None)` 却漏设 `.lang`，导致 `video_clip()` 内 `self.lang == 'en'`
抛 `AttributeError`。`scripts/install_external.sh` 会自动幂等修复；手动打则在
`audio_clipper = VideoClipper(None)` 下一行补：

```python
        audio_clipper.lang = lang
```

### 2.5 模型自动下载（首次 stage 1 自动拉 ~1.3GB）

无需预热脚本——首次 `transcribe`/`clip` 会自动把 4 个模型
（Paraformer-large 944M + VAD + 标点 + CAM++ 说话人）下载到
`~/.cache/modelscope/`，之后命中缓存。

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
| `health()` 返回 False | 检查 `FUNCLIP_PATH` 下是否有 `funclip/videoclipper.py`，`FUNCLIP_PYTHON` 指向的文件是否存在 |
| `ModuleNotFoundError: librosa` 等 | `FUNCLIP_PYTHON` 必须指向 venv 的 `bin/python`；wrapper 已避免 `resolve()` 跟随 symlink，自定义调用时也别解析它，否则会脱离 venv |
| stage 1 `IndexError: list index out of range` | 输入视频无可识别人声（纯音乐/静音），ASR 空结果。换有清晰语音的素材 |
| stage 1 报模型下载失败 | 国内网络去 ModelScope 拉，或 `export MODELSCOPE_CACHE=...` 指本地缓存 |
| stage 2 报 `produced no clip` | `dest_text` 未命中任何语音段，必须严格匹配 SRT 里某条 cue 的文字——先 `transcribe()` 拿 `cues` 再挑文本 |
| `AttributeError: 'VideoClipper' object has no attribute 'lang'` | FunClip stage2 lang patch 未打，见 2.4 |
| `Couldn't find ffmpeg` | 见 2.3，把 imageio-ffmpeg 二进制软链进 PATH |
| TimeoutError | 调大 `FUNCLIP_TIMEOUT_SECONDS`，30 min 视频建议给 3600+ |

## 5.1 实测验证（2026-05-20）

整条链路已在 WSL2 / Python 3.10 / CPU 实测打通：

- transcribe：70s 中文语音 → 33 条 cue，时间戳与文本准确
- clip：`dest_text='试错的过程很简单'` → 产出 1.76s 切片 `clip_001_no0.mp4`（h264+aac）+ 同名 `.srt`
- 注意 FunClip 实际产物名是 `<output_file_stem>_no0.mp4`（wrapper 已自动扫描真实产物，调用方拿到的 `ClipArtifact.video_path` 即真实路径）

## 6. clip → publish 发布流水线

`src/ai_ops/pipeline/clip_to_publish.py` 的 `ClipToPublishPipeline` 把切片产物
编排成多平台发布计划：

```python
from ai_ops.pipeline import ClipToPublishPipeline
from ai_ops.core.schemas import ClipPublishRequest, ClipRequest, ClipSegment
from ai_ops.core.enums import Platform

pipe = ClipToPublishPipeline()  # 默认用 FunClipClipper
plan = await pipe.plan(ClipPublishRequest(
    clip_request=ClipRequest(
        input_video="/path/to/long.mp4",
        segments=[ClipSegment(dest_text="试错的过程很简单")],
    ),
    platforms=[Platform.DOUYIN, Platform.XIAOHONGSHU],
    title="3 分钟讲透 X",
    tags=["干货", "AI"],
))
for item in plan.items:
    print(item.platform, item.content.videos)  # 每条 = 一切片 × 一平台
```

**边界（刻意为之）**：流水线只产出 `ClipPublishPlan`（dry-run 发布计划），
**不触发真发布**。真发布走 `PublishJob` + `scheduler.worker`——那条路带
rate limit / 风控间隔 / pre_publish_check / metrics 闭环，pipeline 直接调
`publisher.publish` 会全绕过。下游拿 `plan.items` 为每条建 Article + PublishJob
入库，worker 自然会发。

## 7. 后续接入点（不在本次范围）

- `api/`：POST /clip 端点，给前端 UI 用
- 自动选段：transcribe 后用 LLM 挑精华 cue，免手填 dest_text
- 平台特化：按平台时长偏好（抖音 60s / 视频号 90s / B 站 不限）筛切片
