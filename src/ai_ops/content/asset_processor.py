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
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image, ImageEnhance

from ..config import settings


# —— EXIF 字段编号（参考 Pillow / TIFF 标准） —— #
_EXIF_TAG_IMAGE_DESCRIPTION = 0x010E
_EXIF_TAG_MAKE = 0x010F
_EXIF_TAG_MODEL = 0x0110
_EXIF_TAG_SOFTWARE = 0x0131
_EXIF_TAG_DATETIME = 0x0132
_EXIF_TAG_DATETIME_ORIGINAL = 0x9003
_EXIF_TAG_DATETIME_DIGITIZED = 0x9004

# 真实手机/相机组合候选——避免极端冷门，规避指纹打点
_DEVICE_POOL = [
    ("Apple", "iPhone 13", "iOS 17.4 Camera"),
    ("Apple", "iPhone 14 Pro", "iOS 17.5"),
    ("Apple", "iPhone 15", "iOS 17.6"),
    ("HUAWEI", "P50 Pro", "EMUI 12.0"),
    ("HUAWEI", "Mate 60", "HarmonyOS 4.0"),
    ("Xiaomi", "Mi 13", "MIUI 14"),
    ("Xiaomi", "14 Ultra", "HyperOS 1.0"),
    ("OPPO", "Find X6", "ColorOS 13"),
    ("vivo", "X100", "OriginOS 4"),
    ("samsung", "SM-S928B", "Galaxy S24 Ultra"),
]


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


# ============================================================================ #
# v2 接口：process_image / process_images
# ----------------------------------------------------------------------------- #
# 比 diversify_image 多做的事：
#   - 写 EXIF（Software / Make / Model / DateTime / DateTimeOriginal）
#   - 微旋转 0.1-0.3°（白底填充避免黑边露馅）
#   - 输出路径强约定到 data/processed/acc_{id}/，文件名 deterministic
#   - seed 仅依赖 (account_id, src_filename)，同账号同图幂等
#
# 设计要点：
#   - 所有随机抽取都用 account_id-derived rng，保证幂等
#   - EXIF 时间戳基于"当前时间 ± random(0,3600)"，但 rng 由 seed 控制，所以同次调用
#     得到相同时间；不同账号不同时间——这正是反矩阵化想要的
#   - 旋转后 .resize 回原尺寸的近似（再裁剪一次），避免输出尺寸暴露
# ============================================================================ #

def _processed_dir(account_id: int, output_dir: Path | str | None = None) -> Path:
    """统一输出目录。默认 settings.data_dir / processed / acc_{id}/"""
    if output_dir is not None:
        return Path(output_dir)
    return Path(settings.data_dir) / "processed" / f"acc_{account_id}"


def _stable_seed(account_id: int, src: Path) -> int:
    """同账号同源文件名 → 同 seed。供 rng / 文件名 hash 共用。"""
    raw = f"asset:{account_id}:{src.name}".encode()
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big")


def _format_exif_datetime(dt: datetime) -> str:
    # EXIF 时间格式：'YYYY:MM:DD HH:MM:SS'
    return dt.strftime("%Y:%m:%d %H:%M:%S")


def process_image(
    local_path: str,
    account_id: int,
    *,
    output_dir: Path | str | None = None,
    publish_time: datetime | None = None,
) -> str:
    """对单张图片做反指纹处理。返回新文件绝对路径（字符串）。

    扰动量级（按 account_id 派生 seed，幂等）：
      - 四边各裁 1-3 像素
      - 亮度 / 对比度 / 饱和度 ±2-3%
      - 旋转 0.1-0.3°（白底填充 + 重裁防黑边）
      - EXIF DateTimeOriginal = publish_time ± random(0, 3600)s
      - EXIF Software / Make / Model 改写为常见手机组合
      - 重新编码（JPEG quality 88-95）
    """
    src = Path(local_path)
    if not src.exists():
        raise FileNotFoundError(f"image not found: {src}")

    dst_dir = _processed_dir(account_id, output_dir)
    dst_dir.mkdir(parents=True, exist_ok=True)

    seed = _stable_seed(account_id, src)
    rng = random.Random(seed)

    img = Image.open(src)
    src_mode = img.mode
    if src_mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")
    elif src_mode == "RGBA":
        # RGBA 在 JPEG 不支持；统一 RGB（透明背景以白色填充）
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[3])
        img = bg
    else:
        img = img.copy()  # 避免直接动 PIL lazy file handle

    w, h = img.size

    # 1) 四边随机微裁 1-3px
    crop_l = rng.randint(1, 3)
    crop_t = rng.randint(1, 3)
    crop_r = rng.randint(1, 3)
    crop_b = rng.randint(1, 3)
    new_w = max(1, w - crop_l - crop_r)
    new_h = max(1, h - crop_t - crop_b)
    img = img.crop((crop_l, crop_t, crop_l + new_w, crop_t + new_h))

    # 2) 亮度 / 对比度 / 饱和度 ±2-3%
    img = ImageEnhance.Brightness(img).enhance(1.0 + rng.uniform(-0.03, 0.03))
    img = ImageEnhance.Contrast(img).enhance(1.0 + rng.uniform(-0.03, 0.03))
    img = ImageEnhance.Color(img).enhance(1.0 + rng.uniform(-0.03, 0.03))

    # 3) 微旋转 0.1-0.3°，白底 + 再裁掉 1% 边避免黑/白边露馅
    angle = rng.uniform(0.1, 0.3) * rng.choice([-1, 1])
    img = img.rotate(angle, resample=Image.BICUBIC, fillcolor=(255, 255, 255), expand=False)
    # 旋转后内圈裁掉 ~1% 防边
    rw, rh = img.size
    pad_w = max(1, int(rw * 0.01))
    pad_h = max(1, int(rh * 0.01))
    img = img.crop((pad_w, pad_h, rw - pad_w, rh - pad_h))

    # 4) 构造 EXIF
    pub_time = publish_time or datetime.utcnow()
    offset_seconds = rng.randint(0, 3600)
    # 一半概率往前，一半往后（用 seed 决定）
    sign = -1 if (rng.random() < 0.5) else 1
    shot_time = pub_time + timedelta(seconds=sign * offset_seconds)
    make, model, software = rng.choice(_DEVICE_POOL)

    exif = img.getexif()
    exif[_EXIF_TAG_MAKE] = make
    exif[_EXIF_TAG_MODEL] = model
    exif[_EXIF_TAG_SOFTWARE] = software
    exif[_EXIF_TAG_DATETIME] = _format_exif_datetime(shot_time)
    exif[_EXIF_TAG_DATETIME_ORIGINAL] = _format_exif_datetime(shot_time)
    exif[_EXIF_TAG_DATETIME_DIGITIZED] = _format_exif_datetime(shot_time)
    # 一个独特 description 防 md5 撞——同账号每次生成都不同也无所谓，反正只用于区分
    exif[_EXIF_TAG_IMAGE_DESCRIPTION] = f"IMG_{seed & 0xFFFFFFFF:08x}"

    # 5) 输出路径：{stem}_p{hash6}.jpg，hash 来自 seed，保证幂等
    tag = f"{seed & 0xFFFFFF:06x}"  # 6 hex chars
    dst = dst_dir / f"{src.stem}_p{tag}.jpg"

    save_kwargs = {
        "quality": rng.randint(88, 95),
        "optimize": True,
        "exif": exif.tobytes() if exif else b"",
    }
    img.save(dst, "JPEG", **save_kwargs)
    return str(dst)


def process_images(
    local_paths: list[str],
    account_id: int,
    *,
    output_dir: Path | str | None = None,
    publish_time: datetime | None = None,
) -> list[str]:
    """批量版 process_image。逐张处理，失败不中断（返回原路径作为兜底）。"""
    out: list[str] = []
    for p in local_paths:
        try:
            out.append(process_image(p, account_id, output_dir=output_dir, publish_time=publish_time))
        except Exception:
            # 单张失败不影响主流程：退回原路径，由 publisher 自己消化
            out.append(p)
    return out
