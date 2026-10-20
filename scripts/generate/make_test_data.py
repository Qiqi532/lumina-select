# -*- coding: utf-8 -*-
"""
make_test_data.py —— 测试数据构造脚本（设计文档 附录 C）

对基准图施加亮度/旋转/模糊/过曝/欠曝等变换，批量生成“模拟连拍 + 相似组 + 废片”，
用于快速验证算法链路（模糊/曝光/相似聚类/评分/甄选）。所有图片写入 EXIF
时间戳（连拍组内 350ms 间隔、不同组分钟级间隔），保证连拍分组逻辑可被真实触发。

用法：
    python scripts/generate/make_test_data.py [输出目录] [每组张数]

注意：
    - 用 PIL 写入 EXIF（cv2.imencode 不支持 EXIF）；
    - 输出路径为 ASCII 时可直接 cv2/PIL 写；仍统一用 tofile 类方式保证健壮。
"""
import os
import sys
from datetime import datetime, timedelta

import cv2
import numpy as np
from PIL import Image, ExifTags


def save_jpeg_with_exif(path, img_bgr, timestamp: datetime, quality=95):
    """保存 JPEG 并写入 EXIF DateTimeOriginal。"""
    rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
    im = Image.fromarray(rgb)
    exif = im.getexif()
    exif[ExifTags.Base.DateTimeOriginal] = timestamp.strftime("%Y:%m:%d %H:%M:%S")
    im.save(path, "JPEG", quality=quality, exif=exif)


def make_base_image(w=960, h=640, hue_shift=0.0):
    """构造一张色彩丰富、细节适中的“风光感”合成基准图（hue_shift 可改色调）。"""
    rng = np.random.default_rng(42)
    img = np.zeros((h, w, 3), dtype=np.uint8)
    # 天空渐变
    for y in range(h // 2):
        t = y / (h / 2)
        img[y, :] = (int(200 - 120 * t), int(160 - 80 * t), int(120 - 40 * t))
    # 地面
    for y in range(h // 2, h):
        t = (y - h // 2) / (h / 2)
        img[y, :] = (int(60 + 40 * t), int(110 + 50 * t), int(60 + 40 * t))
    # 太阳
    cv2.circle(img, (w // 2, h // 3), 50, (255, 240, 200), -1)
    # 山体轮廓
    pts = np.array([[0, h // 2], [w * 0.2, h * 0.30], [w * 0.4, h * 0.42],
                    [w * 0.6, h * 0.28], [w * 0.8, h * 0.40], [w, h * 0.32], [w, h]], np.int32)
    cv2.fillPoly(img, [pts], (90, 120, 90))
    # 细节纹理
    for _ in range(400):
        x, y = int(rng.integers(0, w)), int(rng.integers(0, h))
        c = int(rng.integers(0, 255))
        cv2.circle(img, (x, y), int(rng.integers(1, 3)), (c, c, c), -1)
    cv2.putText(img, "SAMPLE", (w // 2 - 60, h - 30), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
    if hue_shift:
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        hsv[:, :, 0] = (hsv[:, :, 0] + hue_shift) % 180
        img = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)
    return img


def make_burst(base, out_dir, count=12, t0=None, prefix="burst"):
    """连拍组：轻微亮度/旋转差异，EXIF 间隔 350ms（<1500ms → 同连拍组）。"""
    os.makedirs(out_dir, exist_ok=True)
    t = t0 or datetime(2026, 9, 1, 10, 0, 0)
    for i in range(count):
        g = cv2.convertScaleAbs(base, alpha=1.0 + (i - count / 2) * 0.006,
                                beta=int((i - count / 2) * 1.2))
        h, w = g.shape[:2]
        M = cv2.getRotationMatrix2D((w / 2, h / 2), (i - count / 2) * 0.3, 1.0)
        g = cv2.warpAffine(g, M, (w, h), borderMode=cv2.BORDER_REPLICATE)
        save_jpeg_with_exif(os.path.join(out_dir, f"{prefix}_{i:02d}.jpg"), g, t)
        t += timedelta(milliseconds=350)   # 连拍间隔 350ms


def make_groups(base, out_dir, n_groups=3, per_group=10, t0=None):
    """多组相似组：不同色调 + 不同取景，组间分钟级间隔（pHash 可分）。"""
    os.makedirs(out_dir, exist_ok=True)
    h, w = base.shape[:2]
    t = t0 or datetime(2026, 9, 1, 11, 0, 0)
    for g in range(n_groups):
        variant = make_base_image(w, h, hue_shift=(60 + 60 * g) % 180)
        scale = 0.7 + 0.1 * g
        x0, y0 = int(w * 0.1 * g), int(h * 0.08 * g)
        crop = variant[y0:y0 + int(h * 0.8), x0:x0 + int(w * 0.8)]
        crop = cv2.resize(crop, (int(w * scale), int(h * scale)))
        for i in range(per_group):
            img = cv2.convertScaleAbs(crop, alpha=1.0 + (i - per_group / 2) * 0.005,
                                      beta=int((i - per_group / 2)))
            save_jpeg_with_exif(os.path.join(out_dir, f"grp{g}_{i:02d}.jpg"), img, t)
            t += timedelta(milliseconds=400)   # 组内也近似连拍
        t += timedelta(minutes=2)              # 组间间隔 2 分钟


def make_waste(base, out_dir, t0=None):
    """废片：模糊 / 过曝 / 欠曝 / 抖动。"""
    os.makedirs(out_dir, exist_ok=True)
    t = t0 or datetime(2026, 9, 1, 12, 0, 0)
    save_jpeg_with_exif(os.path.join(out_dir, "waste_blur.jpg"),
                        cv2.GaussianBlur(base, (31, 31), 0), t)
    t += timedelta(minutes=1)
    save_jpeg_with_exif(os.path.join(out_dir, "waste_over.jpg"),
                        cv2.convertScaleAbs(base, alpha=1.0, beta=180), t)
    t += timedelta(minutes=1)
    # 注意：过曝/欠曝不能用 convertScaleAbs 的负 beta（内部取绝对值会把暗部变亮），
    # 欠曝用 cv2.subtract 直接压暗。
    save_jpeg_with_exif(os.path.join(out_dir, "waste_under.jpg"),
                        cv2.subtract(base, np.full_like(base, 160)), t)
    t += timedelta(minutes=1)
    kernel = np.zeros((12, 12))
    kernel[6, :] = 1.0 / 12
    save_jpeg_with_exif(os.path.join(out_dir, "waste_shake.jpg"),
                        cv2.filter2D(base, -1, kernel), t)


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "data/synthetic"
    per = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    os.makedirs(out, exist_ok=True)
    t0 = datetime(2026, 9, 1, 10, 0, 0)
    base = make_base_image()
    make_burst(base, out, count=per, t0=t0, prefix="burst")
    make_groups(base, out, n_groups=3, per_group=per, t0=t0 + timedelta(minutes=1))
    make_waste(base, out, t0=t0 + timedelta(hours=1))
    files = [f for f in os.listdir(out) if f.lower().endswith(".jpg")]
    print(f"生成 {len(files)} 张测试图片 -> {out}")
    print("  - 连拍组: burst_xx.jpg（350ms 连拍）")
    print("  - 相似组: grp{g}_{i}.jpg（3 组，不同色调/取景，组间 2 分钟）")
    print("  - 废片: waste_blur / waste_over / waste_under / waste_shake")
