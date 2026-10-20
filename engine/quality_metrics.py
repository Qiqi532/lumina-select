"""Deterministic OpenCV technical-quality metrics shared by all editions."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import cv2
import numpy as np


MODEL_NAME = "opencv-technical-v1"
MAX_ANALYSIS_EDGE = 1600


@dataclass(frozen=True, slots=True)
class TechnicalQuality:
    overall: float
    sharpness: float
    exposure: float
    contrast: float
    over_ratio: float
    under_ratio: float
    model_name: str = MODEL_NAME


def _clamp_score(value: float) -> float:
    return max(0.0, min(100.0, float(value)))


def _validate_rgb(rgb: np.ndarray) -> None:
    if (
        not isinstance(rgb, np.ndarray)
        or rgb.dtype != np.uint8
        or rgb.ndim != 3
        or rgb.shape[2] != 3
        or rgb.shape[0] == 0
        or rgb.shape[1] == 0
    ):
        raise ValueError("expected a non-empty HxWx3 uint8 RGB array")


def _bounded_rgb(rgb: np.ndarray) -> np.ndarray:
    height, width = rgb.shape[:2]
    longest = max(height, width)
    if longest <= MAX_ANALYSIS_EDGE:
        return rgb
    scale = MAX_ANALYSIS_EDGE / float(longest)
    return cv2.resize(
        rgb,
        (max(1, round(width * scale)), max(1, round(height * scale))),
        interpolation=cv2.INTER_AREA,
    )


def evaluate_technical_quality(rgb: np.ndarray) -> TechnicalQuality:
    """Return bounded, deterministic technical scores for one RGB image."""
    _validate_rgb(rgb)
    gray = cv2.cvtColor(_bounded_rgb(rgb), cv2.COLOR_RGB2GRAY)
    laplacian_variance = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    sharpness = _clamp_score(100.0 * (1.0 - np.exp(-laplacian_variance / 400.0)))

    histogram = cv2.calcHist([gray], [0], None, [256], [0, 256]).reshape(-1)
    total = float(gray.size)
    over_ratio = float(histogram[245:].sum()) / total
    under_ratio = float(histogram[:10].sum()) / total
    brightness_penalty = abs(float(gray.mean()) - 127.5) / 127.5
    exposure = _clamp_score(
        100.0
        * (1.0 - 0.65 * brightness_penalty - 0.35 * max(over_ratio, under_ratio))
    )
    contrast = _clamp_score(float(gray.std()) / 64.0 * 100.0)
    overall = _clamp_score(0.50 * sharpness + 0.30 * exposure + 0.20 * contrast)
    return TechnicalQuality(
        overall=overall,
        sharpness=sharpness,
        exposure=exposure,
        contrast=contrast,
        over_ratio=over_ratio,
        under_ratio=under_ratio,
    )


def evaluate_technical_quality_batch(
    rgbs: Iterable[np.ndarray],
) -> list[TechnicalQuality]:
    """Evaluate images in input order without loading models or touching disk."""
    return [evaluate_technical_quality(rgb) for rgb in rgbs]
