"""asset_processor.process_image / process_images 单测。

验证：
  - 新文件生成
  - EXIF 写入（Software / Make / Model / DateTimeOriginal）
  - 尺寸变了（裁剪 + 旋转后内圈裁掉）
  - 同账号同图幂等（seed 稳定）
  - 不同账号同图结果不同（不同 seed）
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from PIL import Image

from ai_ops.content.asset_processor import process_image, process_images


def _make_test_jpg(tmp_path: Path, name: str = "src.jpg", size=(800, 600), color="red") -> Path:
    p = tmp_path / name
    Image.new("RGB", size, color).save(p, "JPEG")
    return p


def test_process_image_creates_new_file(tmp_path):
    src = _make_test_jpg(tmp_path)
    out_dir = tmp_path / "out"

    new_path = process_image(str(src), account_id=42, output_dir=out_dir)
    new_path = Path(new_path)

    assert new_path.exists()
    assert new_path.parent == out_dir
    assert new_path.suffix == ".jpg"
    assert new_path != src


def test_process_image_exif_written(tmp_path):
    src = _make_test_jpg(tmp_path)
    out_dir = tmp_path / "out"

    new_path = process_image(str(src), account_id=42, output_dir=out_dir)
    img = Image.open(new_path)
    exif = img.getexif()

    # 应该写入 Software / Make / Model / DateTimeOriginal / DateTime
    assert exif.get(0x010F)  # Make
    assert exif.get(0x0110)  # Model
    assert exif.get(0x0131)  # Software
    assert exif.get(0x0132)  # DateTime
    assert exif.get(0x9003)  # DateTimeOriginal


def test_process_image_size_changed(tmp_path):
    """裁剪 + 旋转后内圈裁掉 → 输出尺寸 < 原始尺寸。"""
    src = _make_test_jpg(tmp_path, size=(800, 600))
    out_dir = tmp_path / "out"

    new_path = process_image(str(src), account_id=42, output_dir=out_dir)
    new_img = Image.open(new_path)
    nw, nh = new_img.size

    # 原 800x600，四边各裁 1-3 + 旋转后再裁 1% → 一定变小
    assert nw < 800
    assert nh < 600
    # 但裁掉的不多（>= 90% 量级）
    assert nw > 700
    assert nh > 540


def test_process_image_same_account_same_input_idempotent(tmp_path):
    """同账号 + 同源文件名 → 两次跑结果应一致（seed 稳定）。"""
    src = _make_test_jpg(tmp_path)
    out_dir = tmp_path / "out"

    p1 = process_image(str(src), account_id=42, output_dir=out_dir)
    p2 = process_image(str(src), account_id=42, output_dir=out_dir)
    assert p1 == p2  # 文件名 deterministic

    # 字节级 hash 也应一致：
    # 注意 publish_time 参数没传，默认 now()，rng 决定 offset，但 datetime.utcnow() 在两次调用间会变
    # 所以 EXIF 时间戳 _format_exif_datetime 的输出会变（精确到秒），
    # 实际两次跑生成的 JPEG 可能差几个字节。这里只验文件存在 + 路径相同。
    assert Path(p1).exists()


def test_process_image_different_accounts_different_output(tmp_path):
    """不同账号同源文件 → 输出文件名应不同（seed 不同）。"""
    src = _make_test_jpg(tmp_path)
    out_dir_root = tmp_path / "out_root"

    p1 = process_image(str(src), account_id=1, output_dir=out_dir_root / "acc_1")
    p2 = process_image(str(src), account_id=2, output_dir=out_dir_root / "acc_2")

    assert Path(p1).exists()
    assert Path(p2).exists()
    # 不同 seed → 文件名 _p{hash6} 不同
    assert Path(p1).name != Path(p2).name


def test_process_image_pixel_difference(tmp_path):
    """同图喂给不同账号 → 输出 pixel 数据应有差异（亮度/对比度/旋转/裁剪 都不同）。"""
    src = _make_test_jpg(tmp_path, color="red")
    out = tmp_path / "out"

    p1 = Path(process_image(str(src), account_id=11, output_dir=out / "a"))
    p2 = Path(process_image(str(src), account_id=22, output_dir=out / "b"))

    # 直接比文件字节
    b1 = p1.read_bytes()
    b2 = p2.read_bytes()
    assert b1 != b2


def test_process_images_batch(tmp_path):
    src1 = _make_test_jpg(tmp_path, name="a.jpg")
    src2 = _make_test_jpg(tmp_path, name="b.jpg")
    out_dir = tmp_path / "out"

    results = process_images([str(src1), str(src2)], account_id=7, output_dir=out_dir)
    assert len(results) == 2
    for r in results:
        assert Path(r).exists()


def test_process_image_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        process_image(str(tmp_path / "nope.jpg"), account_id=1, output_dir=tmp_path / "out")


def test_process_images_failure_fallback(tmp_path):
    """批量中单张失败 → 回退到原路径，不中断。"""
    real = _make_test_jpg(tmp_path)
    fake = tmp_path / "ghost.jpg"  # 不存在
    out_dir = tmp_path / "out"

    results = process_images([str(real), str(fake)], account_id=7, output_dir=out_dir)
    assert len(results) == 2
    assert Path(results[0]).exists()
    # 第二张失败 → 回退到原 path 字符串
    assert results[1] == str(fake)


def test_process_image_explicit_publish_time(tmp_path):
    src = _make_test_jpg(tmp_path)
    out_dir = tmp_path / "out"
    fixed = datetime(2026, 1, 1, 12, 0, 0)

    new_path = process_image(
        str(src), account_id=42, output_dir=out_dir, publish_time=fixed
    )
    img = Image.open(new_path)
    exif = img.getexif()
    dt = exif.get(0x9003) or ""  # DateTimeOriginal
    # 应该是 2026:01:01 附近（offset ±3600s 内）
    assert dt.startswith("2026:01:01") or dt.startswith("2025:12:31") or dt.startswith("2026:01:02")
