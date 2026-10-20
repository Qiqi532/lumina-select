from __future__ import annotations

import numpy as np
import pytest

from engine.quality_metrics import (
    TechnicalQuality,
    evaluate_technical_quality,
    evaluate_technical_quality_batch,
)


MID_GRAY_RGB = np.full((64, 64, 3), 128, dtype=np.uint8)
WHITE_RGB = np.full((64, 64, 3), 255, dtype=np.uint8)
BLACK_RGB = np.zeros((64, 64, 3), dtype=np.uint8)
_CHECKER = (np.indices((64, 64)).sum(axis=0) % 2 * 255).astype(np.uint8)
CHECKERBOARD_RGB = np.repeat(_CHECKER[:, :, None], 3, axis=2)


def test_technical_quality_contract():
    result = evaluate_technical_quality(CHECKERBOARD_RGB)

    assert isinstance(result, TechnicalQuality)
    assert 0.0 <= result.overall <= 100.0
    assert 0.0 <= result.sharpness <= 100.0
    assert 0.0 <= result.exposure <= 100.0
    assert 0.0 <= result.contrast <= 100.0
    assert 0.0 <= result.over_ratio <= 1.0
    assert 0.0 <= result.under_ratio <= 1.0
    assert result.model_name == "opencv-technical-v1"


def test_overexposure_lowers_exposure_score():
    assert (
        evaluate_technical_quality(WHITE_RGB).exposure
        < evaluate_technical_quality(MID_GRAY_RGB).exposure
    )


def test_batch_preserves_input_order_and_length():
    results = evaluate_technical_quality_batch(
        [BLACK_RGB, CHECKERBOARD_RGB, WHITE_RGB]
    )

    assert len(results) == 3
    assert results[1].sharpness > results[0].sharpness
    assert results[2].over_ratio > results[0].over_ratio


@pytest.mark.parametrize(
    "invalid",
    [
        np.zeros((64, 64), dtype=np.uint8),
        np.zeros((64, 64, 4), dtype=np.uint8),
        np.zeros((64, 64, 3), dtype=np.float32),
    ],
)
def test_invalid_input_is_rejected(invalid):
    with pytest.raises(ValueError, match="HxWx3 uint8"):
        evaluate_technical_quality(invalid)
