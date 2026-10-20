# -*- coding: utf-8 -*-
"""quality.py 单测：模糊/曝光判定、BRISQUE 饱和中性化保护。"""
from __future__ import annotations

import numpy as np
import cv2

from engine import quality


def _arr(shape, value):
    return np.full(shape, value, dtype=np.uint8)


def test_flat_image_is_low_blur():
    """纯色平滑图拉普拉斯方差应接近 0（明显低于废片阈值 60）。"""
    v = quality.blur_score_from_gray(_arr((256, 256), 128))
    assert v < quality.BLUR_WASTE_THRESHOLD


def test_noisy_image_high_blur():
    """随机噪声图拉普拉斯方差应显著高于废片阈值。"""
    rng = np.random.default_rng(0)
    v = quality.blur_score_from_gray(rng.integers(0, 256, (256, 256)).astype(np.uint8))
    assert v > quality.BLUR_WASTE_THRESHOLD


def test_small_image_is_not_upsampled_and_has_unreliable_blur():
    gray = np.zeros((200, 300), dtype=np.uint8)
    gray[:, 150:] = 255
    expected = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    assert quality.blur_score_from_gray(gray) == expected
    rgb = np.stack([gray] * 3, axis=-1)
    assert quality.analyze_image_array(rgb)["blur_reliable"] is False


def test_large_image_keeps_aspect_ratio_when_downsampled():
    gray = np.zeros((800, 1200), dtype=np.uint8)
    gray[:, 600:] = 255
    resized = cv2.resize(gray, (512, 341), interpolation=cv2.INTER_AREA)
    expected = float(cv2.Laplacian(resized, cv2.CV_64F).var())
    assert quality.blur_score_from_gray(gray) == expected


def test_white_overexposed():
    """全白图应判为严重过曝（over > 0.5）。"""
    gray = _arr((256, 256), 255)
    over, under = quality.exposure_ratio_from_gray(gray)
    assert over > quality.EXPO_WASTE_RATIO
    assert under < quality.EXPO_WASTE_RATIO


def test_black_underexposed():
    """全黑图应判为严重欠曝（under > 0.5）。"""
    gray = _arr((256, 256), 0)
    over, under = quality.exposure_ratio_from_gray(gray)
    assert over < quality.EXPO_WASTE_RATIO
    assert under > quality.EXPO_WASTE_RATIO


def test_analyze_image_array_shape():
    """analyze_image_array 对合法 RGB 输入返回四指标。"""
    rgb = np.full((32, 32, 3), 200, dtype=np.uint8)
    r = quality.analyze_image_array(rgb)
    assert set(r) >= {"blur_score", "over", "under", "noise"}


def test_analyze_image_array_none():
    """空输入不抛异常，返回全 0。"""
    r = quality.analyze_image_array(None)
    assert r["blur_score"] == 0.0


def test_norm_quality_higher_is_better():
    """v0.4：画质分统一为 0-100【越高越好】，norm_quality 线性映射到 [0,1]，
    不再对高分做"饱和中性化"补丁（模型为数据驱动，无此饱和问题）。"""
    from engine import scorer
    # 缺失/无模型 → 中性 0.5（与旧版一致）
    assert scorer.norm_quality(None) == 0.5
    # 0-100 越高越好：90 分 ≈ 0.9；低分 < 高分
    assert abs(scorer.norm_quality(90.0) - 0.9) < 1e-6
    assert scorer.norm_quality(10.0) < scorer.norm_quality(40.0)
    # 边界裁剪到 [0,1]
    assert scorer.norm_quality(200.0) == 1.0
    assert scorer.norm_quality(-50.0) == 0.0
