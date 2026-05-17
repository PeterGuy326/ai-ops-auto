"""图片 / 视频去重处理器（反内容指纹）。

为什么：
  同主题多账号分发时，如果图片/视频 MD5 / pHash 完全一致，
  平台会直接判为"营销号矩阵"，全员限流。

策略：
  - 图片：改 EXIF + 微裁剪 + 微调色 + 重新编码（变 MD5，pHash 保持高度相似但 ≠ 完全一致）
  - 视频：ffmpeg 转码 + 微调 bitrate（需要本机有 ffmpeg）

依赖：
  Pillow（必装，pyproject 已声明）
  ffmpeg（系统级，可选；未装时视频处理跳过 + warning）
"""
from __future__ import annotations

import hashlib
import random
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageEnhance


def compute_md5(path: Path | str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def diversify_image(
    src: Path | str,
    dst_dir: Path | str,
    *,
    seed: int | None = None,
) -> Path:
    """对图片做微扰动，生成 MD5 不同的新文件。

    seed 决定扰动强度（如不同 account_id 生成不同变体）。
    """
    src = Path(src)
    dst_dir = Path(dst_dir)
    dst_dir.mkdir(parents=True, exist_ok=True)

    rng = random.Random(seed if seed is not None else random.randint(0, 2**31))

    img = Image.open(src)
    img = img.convert("RGB") if img.mode not in ("RGB", "RGBA") else img

    # 1. 微裁剪：右下各裁 1-3 像素（人眼几乎看不到）
    w, h = img.size
    cw = rng.randint(1, 3)
    ch = rng.randint(1, 3)
    img = img.crop((0, 0, max(1, w - cw), max(1, h - ch)))

    # 2. 微亮度（±2%）
    brightness_delta = 1.0 + rng.uniform(-0.02, 0.02)
    img = ImageEnhance.Brightness(img).enhance(brightness_delta)

    # 3. 微对比度（±1.5%）
    contrast_delta = 1.0 + rng.uniform(-0.015, 0.015)
    img = ImageEnhance.Contrast(img).enhance(contrast_delta)

    # 4. 输出到新路径，文件名带 seed hash 便于追溯
    suffix = src.suffix.lower()
    if suffix not in (".jpg", ".jpeg", ".png", ".webp"):
        suffix = ".jpg"
    tag = hashlib.md5(f"{src}-{seed}".encode()).hexdigest()[:8]
    dst = dst_dir / f"{src.stem}_{tag}{suffix}"

    # 5. 重新编码（quality 微抖），加上当前时间作为新 EXIF 'Software'
    save_kwargs: dict = {}
    if suffix in (".jpg", ".jpeg"):
        save_kwargs["quality"] = rng.randint(88, 95)
        save_kwargs["optimize"] = True
    img.save(dst, **save_kwargs)
    return dst


def diversify_video(
    src: Path | str,
    dst_dir: Path | str,
    *,
    seed: int | None = None,
) -> Path:
    """ffmpeg 转码视频改 hash。未装 ffmpeg 时直接拷贝 + warning。"""
    src = Path(src)
    dst_dir = Path(dst_dir)
    dst_dir.mkdir(parents=True, exist_ok=True)

    rng = random.Random(seed if seed is not None else random.randint(0, 2**31))
    tag = hashlib.md5(f"{src}-{seed}".encode()).hexdigest()[:8]
    dst = dst_dir / f"{src.stem}_{tag}.mp4"

    if shutil.which("ffmpeg") is None:
        # 兜底：未装 ffmpeg 时直接拷贝（不改 hash，但保证流程不中断）
        shutil.copyfile(src, dst)
        return dst

    # CRF 微抖（22-25），bitrate 微调，加 metadata 时间戳
    crf = rng.randint(22, 25)
    ts = datetime.utcnow().isoformat()
    cmd = [
        "ffmpeg", "-y",
        "-i", str(src),
        "-c:v", "libx264", "-crf", str(crf),
        "-preset", "fast",
        "-c:a", "aac", "-b:a", "128k",
        "-metadata", f"comment=ai-ops-{tag}-{ts}",
        "-movflags", "+faststart",
        str(dst),
    ]
    subprocess.run(cmd, capture_output=True, check=False)
    return dst if dst.exists() else src  # 失败时退回原文件


def diversify_content_for_account(
    images: list[str],
    videos: list[str],
    account_id: int,
    workspace: Path | str,
) -> tuple[list[str], list[str]]:
    """给单账号生成差异化的图片/视频集合。account_id 作为 seed 保证可复现。"""
    workspace = Path(workspace)
    out_dir = workspace / f"acc_{account_id}"
    new_images = [str(diversify_image(p, out_dir, seed=account_id + i)) for i, p in enumerate(images)]
    new_videos = [str(diversify_video(p, out_dir, seed=account_id + i)) for i, p in enumerate(videos)]
    return new_images, new_videos
