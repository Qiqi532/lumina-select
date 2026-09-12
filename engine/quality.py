"""Compatibility helpers for blur, exposure, noise, and technical quality."""
from __future__ import annotations

import cv2
import numpy as np

from . import config
from .loader import cv_imread, load_image_rgb_array
from .quality_metrics import MODEL_NAME, evaluate_technical_quality


BLUR_WASTE_THRESHOLD = config.BLUR_WASTE_THRESHOLD
EXPO_WASTE_RATIO = config.EXPO_WASTE_RATIO
BLUR_ANALYZE_SIZE = config.BLUR_ANALYZE_SIZE


def blur_score_from_gray(gray: np.ndarray) -> float:
    if gray is None or gray.size == 0:
        return 0.0
    if gray.ndim == 3:
        gray = cv2.cvtColor(gray, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, (BLUR_ANALYZE_SIZE, BLUR_ANALYZE_SIZE))
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def blur_score(path: str) -> float:
    return blur_score_from_gray(cv_imread(path, cv2.IMREAD_GRAYSCALE))


def exposure_ratio_from_gray(gray: np.ndarray) -> tuple[float, float]:
    if gray is None or gray.size == 0:
        return (0.0, 0.0)
    if gray.ndim == 3:
        gray = cv2.cvtColor(gray, cv2.COLOR_BGR2GRAY)
    histogram = cv2.calcHist([gray], [0], None, [256], [0, 256]).reshape(-1)
    total = float(gray.size)
    return float(histogram[245:].sum()) / total, float(histogram[:10].sum()) / total


def exposure_ratio(path: str) -> tuple[float, float]:
    return exposure_ratio_from_gray(cv_imread(path, cv2.IMREAD_GRAYSCALE))


def noise_estimate_from_gray(gray: np.ndarray) -> float:
    if gray is None or gray.size == 0:
        return 0.0
    if gray.ndim == 3:
        gray = cv2.cvtColor(gray, cv2.COLOR_BGR2GRAY)
    return float(np.abs(cv2.Laplacian(gray, cv2.CV_64F)).mean() / 255.0)


def analyze_image_array(rgb: np.ndarray) -> dict:
    if rgb is None or rgb.size == 0:
        return {"blur_score": 0.0, "over": 0.0, "under": 0.0, "noise": 0.0}
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    over, under = exposure_ratio_from_gray(gray)
    return {
        "blur_score": blur_score_from_gray(gray),
        "over": over,
        "under": under,
        "noise": noise_estimate_from_gray(gray),
    }


def analyze_path(path: str) -> dict:
    return analyze_image_array(load_image_rgb_array(path))


def iqa_score_batch(
    rgbs: list[np.ndarray], max_size: int | None = None
) -> list[float | None]:
    """Legacy name for the shared OpenCV technical score."""
    del max_size
    return [evaluate_technical_quality(rgb).overall for rgb in rgbs]


def iqa_score_array(rgb: np.ndarray, max_size: int | None = None) -> float | None:
    """Legacy name for a single shared OpenCV technical score."""
    del max_size
    if rgb is None or rgb.size == 0:
        return None
    return evaluate_technical_quality(rgb).overall


def quality_model_name() -> str:
    return MODEL_NAME
