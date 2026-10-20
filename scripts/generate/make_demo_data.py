# -*- coding: utf-8 -*-
"""重构 data/demo 综合演示数据集。

组成（共 28 张）：
  A. 真实人像连拍组 ×2（face_open1/face_open2 各 3 张亮度变体，350ms 连拍）→ 最佳帧 + 去重
  B. 真实风光连拍组 ×2（landscape1/landscape2 各 3 张亮度变体）→ 最佳帧 + 去重
  C. 闭眼废片 ×2（face_closed1 临界未判 / face_closed2 明确闭眼）
  D. 合成连拍组 ×10（data/synthetic/burst_*，350ms）→ 不确定甄选
  E. 合成废片 ×4（data/synthetic/waste_*：模糊/过曝/欠曝/抖动）
"""
import os
import shutil
from datetime import datetime, timedelta

import cv2
import numpy as np
from PIL import Image, ExifTags


def save_with_exif(src, dst, ts: datetime, brightness=0.0):
    """读取源图，可选亮度调整，写入 EXIF 后保存。"""
    im = Image.open(src).convert("RGB")
    if abs(brightness) > 0.001:
        arr = np.asarray(im, dtype=np.float32) * (1.0 + brightness)
        arr = np.clip(arr, 0, 255).astype(np.uint8)
        im = Image.fromarray(arr)
    exif = im.getexif()
    exif[ExifTags.Base.DateTimeOriginal] = ts.strftime("%Y:%m:%d %H:%M:%S")
    im.save(dst, "JPEG", quality=95, exif=exif)


def make_real_burst(src, out_dir, prefix, t0: datetime, variants=(0.0, 0.05, -0.05)):
    """真实照片连拍组：亮度变体，350ms 间隔（<1500ms → 同一连拍组）。"""
    t = t0
    for i, v in enumerate(variants):
        save_with_exif(src, os.path.join(out_dir, f"{prefix}_{i:02d}.jpg"), t, v)
        t += timedelta(milliseconds=350)
    return t


def main():
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    demo = os.path.join(here, "data", "demo")
    if os.path.isdir(demo):
        shutil.rmtree(demo)
    os.makedirs(demo)
    real = os.path.join(here, "data", "real")
    syn = os.path.join(here, "data", "synthetic")

    t = datetime(2026, 9, 2, 9, 0, 0)
    # A. 真实人像连拍组
    for name in ("face_open1", "face_open2"):
        t = make_real_burst(os.path.join(real, name + ".jpg"), demo, "burst_" + name, t)
        t += timedelta(minutes=2)
    # B. 真实风光连拍组
    for name in ("landscape1", "landscape2"):
        t = make_real_burst(os.path.join(real, name + ".jpg"), demo, "burst_" + name, t)
        t += timedelta(minutes=2)
    # C. 闭眼废片（独立时间戳，形成各自单张组）
    for name in ("face_closed1", "face_closed2"):
        shutil.copy2(os.path.join(real, name + ".jpg"), os.path.join(demo, name + ".jpg"))
        t += timedelta(minutes=1)
    # D. 合成连拍组（复用已生成的 burst_*，含 EXIF）
    for f in sorted(os.listdir(syn)):
        if f.startswith("burst_") and f.lower().endswith(".jpg"):
            shutil.copy2(os.path.join(syn, f), os.path.join(demo, f))
    # E. 合成废片
    for f in sorted(os.listdir(syn)):
        if f.startswith("waste_") and f.lower().endswith(".jpg"):
            shutil.copy2(os.path.join(syn, f), os.path.join(demo, f))

    files = [f for f in os.listdir(demo) if f.lower().endswith(".jpg")]
    print(f"data/demo 共 {len(files)} 张")
    print("  真实人像连拍: burst_face_open1_00-02, burst_face_open2_00-02")
    print("  真实风光连拍: burst_landscape1_00-02, burst_landscape2_00-02")
    print("  闭眼: face_closed1, face_closed2")
    print("  合成连拍: burst_00-09 | 合成废片: waste_blur/over/under/shake")


if __name__ == "__main__":
    main()
