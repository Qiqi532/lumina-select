# -*- coding: utf-8 -*-
"""性能测试数据：生成 1000 张混合 JPEG/PNG 图片（连拍/相似/废片）。"""
import os
import sys
import shutil
from datetime import datetime, timedelta

import cv2
import numpy as np
from PIL import Image, ExifTags

HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def save_jpeg_with_exif(path, img_bgr, timestamp):
    rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
    im = Image.fromarray(rgb)
    exif = im.getexif()
    exif[ExifTags.Base.DateTimeOriginal] = timestamp.strftime("%Y:%m:%d %H:%M:%S")
    im.save(path, "JPEG", quality=92, exif=exif)


def make_image(w=960, h=640, seed=0):
    """随机生成一张有细节的图（保证 Laplacian > 60），并随机加轻微变换。"""
    rng = np.random.default_rng(seed)
    img = np.zeros((h, w, 3), dtype=np.uint8)
    for y in range(h):
        t = y / h
        img[y, :] = (int(180 - 120 * t + rng.integers(-5, 5)),
                     int(150 - 60 * t + rng.integers(-5, 5)),
                     int(110 - 30 * t + rng.integers(-5, 5)))
    # 随机几何/纹理
    for _ in range(6):
        cx, cy, rr = int(rng.integers(0, w)), int(rng.integers(0, h)), int(rng.integers(20, 90))
        color = tuple(int(c) for c in rng.integers(0, 255, 3))
        cv2.circle(img, (cx, cy), rr, color, -1)
    # 丰富纹理（保证细节）
    tex = rng.integers(0, 255, (h, w), dtype=np.uint8)
    tex = cv2.GaussianBlur(tex, (5, 5), 0)
    img = cv2.addWeighted(img, 1.0, cv2.cvtColor(tex, cv2.COLOR_GRAY2BGR), 0.35, 0)
    return img.astype(np.uint8)


def main():
    out = os.path.join(HERE, "data", "perf")
    if os.path.isdir(out):
        shutil.rmtree(out)
    os.makedirs(out)
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1000
    t = datetime(2026, 9, 3, 8, 0, 0)
    base_seed = 1000
    for i in range(n):
        img = make_image(seed=base_seed + i)
        # 每 50 张插入一张模糊废片 / 过曝 / 欠曝，验证废片路径
        kind = i % 50
        if kind == 45:
            img = cv2.GaussianBlur(img, (31, 31), 0)
        elif kind == 46:
            img = cv2.convertScaleAbs(img, alpha=1.0, beta=150)
        elif kind == 47:
            img = cv2.subtract(img, np.full_like(img, 150))
        # 每 500 张输出一张 PNG 验证格式支持
        if i % 500 == 0:
            Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB)).save(
                os.path.join(out, f"img_{i:04d}.png"))
        else:
            save_jpeg_with_exif(os.path.join(out, f"img_{i:04d}.jpg"), img, t)
            t += timedelta(milliseconds=400)
    files = [f for f in os.listdir(out)]
    print(f"生成 {len(files)} 张（含 {sum(1 for f in files if f.endswith('.png'))} 张 PNG）-> {out}")


if __name__ == "__main__":
    main()
