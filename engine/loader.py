# -*- coding: utf-8 -*-
"""
loader.py —— 目录扫描、图像解码、EXIF、缩略图

要点：
    1. Windows 中文路径：cv2.imread/imwrite 无法读写含中文路径（静默返回空），
       统一用 np.fromfile + cv2.imdecode 读、cv2.imencode + tofile 写（cv_imread/cv_imwrite）。
    2. 大众格式 JPEG/PNG 用 Pillow 解码；RAW 仅在检测到扩展名且已安装 rawpy 时启用。
    3. 缩略图统一生成并缓存到项目内 thumb 目录，避免对原图反复解码。
    4. EXIF 优先取 DateTimeOriginal 转毫秒时间戳，无 EXIF 时回退为文件 mtime。

独立命令行调试：python -m engine.loader <目录> [--limit N]
"""
from __future__ import annotations

import os
import time
from datetime import datetime
from io import BytesIO

import cv2
import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError

from . import config
from .log import get_logger

_log = get_logger("loader")

# ---------------------------------------------------------------------------
# 常量（统一来自 engine/config.py）
# ---------------------------------------------------------------------------
# 支持的图像扩展名（小写，不含点）。RAW 扩展名仅在 rawpy 可用时才会被解码。
JPEG_PNG_EXTS = set(config.JPEG_PNG_EXTS)
RAW_EXTS = set(config.RAW_EXTS)

# 是否安装了 rawpy（可选依赖，未安装不阻塞 JPEG/PNG 主流程）
try:
    import rawpy  # noqa: F401

    _HAS_RAWPY = True
except Exception:  # pragma: no cover
    _HAS_RAWPY = False


# ---------------------------------------------------------------------------
# 中文路径安全的 OpenCV 读写封装
# ---------------------------------------------------------------------------
def cv_imread(path: str, flags: int = cv2.IMREAD_GRAYSCALE):
    """读取图像（兼容任意含中文路径）。flags 默认灰度图，与原文档示例一致。"""
    try:
        data = np.fromfile(path, dtype=np.uint8)
        if data.size == 0:
            return None
        return cv2.imdecode(data, flags)
    except Exception:
        return None


def cv_imwrite(path: str, img: np.ndarray, ext: str = ".jpg") -> bool:
    """写入图像（兼容任意含中文路径）。ext 需含点，如 '.jpg'。"""
    try:
        ok, buf = cv2.imencode(ext, img)
        if ok:
            buf.tofile(path)
        return bool(ok)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 目录扫描
# ---------------------------------------------------------------------------
def scan_directory(root: str, include_raw: bool = False, recursive: bool = True):
    """递归扫描目录，返回支持的图像文件绝对路径列表（按文件名排序）。

    参数：
        root       扫描根目录
        include_raw 是否包含 RAW 扩展名（默认 False，面向大众）
        recursive   是否递归子目录
    """
    exts = set(JPEG_PNG_EXTS)
    if include_raw and _HAS_RAWPY:
        exts |= RAW_EXTS
    results = []
    for dirpath, dirnames, filenames in os.walk(root):
        # 跳过缓存/隐藏目录（.thumbs 等点开头目录），避免把缩略图当成照片
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            if fn.startswith("."):
                continue
            ext = os.path.splitext(fn)[1].lower()
            if ext in exts:
                # 统一返回绝对路径，保证跨次分析主键一致、可增量更新
                results.append(os.path.abspath(os.path.join(dirpath, fn)))
        if not recursive:
            break
    results.sort()
    return results


# ---------------------------------------------------------------------------
# EXIF 读取
# ---------------------------------------------------------------------------
def _exif_datetime_to_ms(value) -> float | None:
    """把 EXIF DateTimeOriginal（'YYYY:MM:DD HH:MM:SS'）转为毫秒时间戳。"""
    if not value:
        return None
    s = str(value).strip()
    # 兼容多种分隔符（'YYYY:MM:DD HH:MM:SS' / 'YYYY-MM-DD HH:MM:SS'）
    s = s.replace("-", ":")
    if len(s) < 19:
        return None
    try:
        dt = datetime.strptime(s[:19], "%Y:%m:%d %H:%M:%S")
        return dt.timestamp() * 1000.0
    except Exception:
        return None


def read_exif(path: str) -> dict:
    """读取 EXIF 关键信息。

    返回 dict：
        ts        拍摄时间戳（毫秒），无 EXIF 时回退为文件 mtime * 1000
        width     宽
        height    高
        has_exif  是否读到真实 EXIF 时间
    """
    info = {"ts": None, "width": 0, "height": 0, "has_exif": False}
    try:
        with Image.open(path) as im:
            info["width"], info["height"] = im.size
            exif = im.getexif()
            # 36867 = DateTimeOriginal
            if exif is not None:
                dt = exif.get(36867)
                if not dt:  # 部分文件只有 DateTimeDigitized(36868)
                    dt = exif.get(36868)
                ts = _exif_datetime_to_ms(dt)
                if ts is not None:
                    info["ts"] = ts
                    info["has_exif"] = True
    except Exception:
        pass
    if info["ts"] is None:
        try:
            info["ts"] = os.path.getmtime(path) * 1000.0
        except Exception:
            info["ts"] = 0.0
    return info


# ---------------------------------------------------------------------------
# 图像加载
# ---------------------------------------------------------------------------
def load_image(path: str, max_size: int | None = None) -> Image.Image | None:
    """加载图像为 RGB PIL.Image，可限制最大边长（用于缩略图）。

    JPEG/PNG 用 Pillow；RAW 用 rawpy（若可用）。
    """
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext in RAW_EXTS and _HAS_RAWPY:
            import rawpy

            with rawpy.imread(path) as raw:
                rgb = raw.postprocess(use_camera_wb=True, half_size=True)
            im = Image.fromarray(rgb)
        else:
            im = Image.open(path)
            im = ImageOps.exif_transpose(im)  # 按 EXIF 方向摆正
            im = im.convert("RGB")
        if max_size is not None:
            im.thumbnail((max_size, max_size), Image.LANCZOS)
        return im
    except (UnidentifiedImageError, OSError, Exception):
        return None


def load_review_preview(
    path: str,
    *,
    paired_jpeg: str | None = None,
    max_size: int | None = None,
) -> Image.Image | None:
    """Load a low-latency review preview, preferring paired/embedded JPEG data."""
    if paired_jpeg:
        return load_image(paired_jpeg, max_size=max_size)
    if os.path.splitext(path)[1].casefold() not in RAW_EXTS or not _HAS_RAWPY:
        return load_image(path, max_size=max_size)
    try:
        import rawpy

        with rawpy.imread(path) as raw:
            try:
                thumb = raw.extract_thumb()
                format_name = getattr(thumb.format, "name", str(thumb.format)).casefold()
                if "jpeg" in format_name or "jpg" in format_name:
                    image = Image.open(BytesIO(thumb.data))
                    image = ImageOps.exif_transpose(image).convert("RGB")
                else:
                    raise ValueError("embedded preview is not JPEG")
            except Exception:
                image = Image.fromarray(
                    raw.postprocess(use_camera_wb=True, half_size=True)
                ).convert("RGB")
        if max_size is not None:
            image.thumbnail((max_size, max_size), Image.LANCZOS)
        return image
    except Exception:
        return None


def load_image_rgb_array(path: str, max_size: int | None = None) -> np.ndarray | None:
    """加载图像并返回 RGB numpy 数组（H, W, 3，uint8）。"""
    im = load_image(path, max_size=max_size)
    if im is None:
        return None
    return np.asarray(im, dtype=np.uint8)


def get_mtime(path: str) -> float:
    """返回文件修改时间（秒级时间戳），供增量分析比对。"""
    try:
        return os.path.getmtime(path)
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# 缩略图
# ---------------------------------------------------------------------------
def make_thumbnail(path: str, size: int = config.THUMB_DEFAULT_SIZE,
                   thumb_dir: str | None = None) -> str | None:
    """生成并缓存缩略图，返回缩略图文件路径；失败返回 None。

    缓存目录默认：项目内 data/.thumbcache；缓存命中时直接复用，避免反复解码原图。
    缩略图以 文件绝对路径的 md5 作为文件名，规避中文路径问题。

    【修复 v0.4】旧实现把 .thumbs 目录建在【照片所在目录】里。对摄影师这是不可
    接受的副作用：会往珍贵的原始素材目录里塞隐藏文件夹，被 Lightroom/网盘/备份
    工具扫到，甚至跟着一起同步上传。缩略图是程序自身的派生数据，必须待在项目内。
    """
    import hashlib

    try:
        if thumb_dir is None:
            thumb_dir = config.THUMB_CACHE_DIR
        os.makedirs(thumb_dir, exist_ok=True)
        digest = hashlib.md5(path.encode("utf-8", "surrogatepass")).hexdigest()
        thumb_path = os.path.join(thumb_dir, f"{digest}_{size}.jpg")

        # 缓存命中且原图未变更时直接复用
        if os.path.exists(thumb_path):
            try:
                if os.path.getmtime(thumb_path) >= os.path.getmtime(path):
                    return thumb_path
            except Exception:
                pass

        im = load_image(path, max_size=size)
        if im is None:
            return None
        im.save(thumb_path, "JPEG", quality=config.THUMB_QUALITY)
        return thumb_path
    except Exception as e:
        _log.debug("缩略图生成失败 %s: %s", path, e)
        return None


# ---------------------------------------------------------------------------
# 命令行调试入口
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    root = sys.argv[1] if len(sys.argv) > 1 else "."
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    paths = scan_directory(root)
    if limit > 0:
        paths = paths[:limit]
    print(f"扫描到 {len(paths)} 张图像（目录: {root}）")
    for p in paths[:20]:
        ex = read_exif(p)
        print(f"  {os.path.basename(p):30s} ts={ex['ts']:.0f}ms {ex['width']}x{ex['height']} exif={ex['has_exif']}")
    if len(paths) > 20:
        print(f"  ... 其余 {len(paths) - 20} 张省略")
