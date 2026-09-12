# -*- coding: utf-8 -*-
"""推理后端抽象层回归测试（轻量化 Phase 0/1）。

只验证"接缝"本身与空输入路径，不触发任何 AI 模型下载/推理，
因此运行快、且不需要联网或 GPU。
"""
import importlib.util
import sys

import numpy as np
import pytest

from engine import config, inference


def test_backend_names_includes_torch():
    assert "torch" in inference.backend_names()


def test_get_backend_default_matches_config():
    backend = inference.get_backend()
    assert backend.name == config.INFERENCE_BACKEND


def test_get_backend_unknown_raises():
    with pytest.raises(ValueError):
        inference.get_backend("no-such-backend")


def test_empty_inputs_return_empty_lists():
    # 空输入不应触发模型加载，直接返回等长空结果
    assert inference.quality_scores([]) == []
    assert inference.scene_and_aesthetics([]) == []


@pytest.mark.skipif(importlib.util.find_spec("torch") is None,
                    reason="Torch backend is not installed in the lightweight environment")
def test_quality_scores_delegates_to_torch_backend():
    # 单张合成图也应走通（torch 后端会真正加载画质模型并打分）
    img = np.zeros((64, 64, 3), dtype=np.uint8)
    scores = inference.get_backend("torch").quality_scores([img])
    assert isinstance(scores, list) and len(scores) == 1
    # 纯黑图模型分可能为 None 或数值；只断言类型安全
    assert scores[0] is None or isinstance(scores[0], float)


def test_register_and_select_onnx_backend():
    # 验证"换后端只需注册+翻配置"的扩展点可用（不依赖 torch/onnx 实际安装）
    class _FakeBackend:
        name = "fake"

        def quality_scores(self, rgbs):
            return [42.0] * len(rgbs)

        def scene_and_aesthetics(self, images):
            return [{"aesthetic": 50.0, "scene": "其他", "scene_conf": 1.0}] * len(images)

        def quality_model_name(self):
            return "fake-q"

        def aesthetic_model_name(self):
            return "fake-a"

    inference.register_backend("fake", _FakeBackend)
    inference.reset_cache()
    try:
        b = inference.get_backend("fake")
        assert b.name == "fake"
        assert b.quality_scores([np.zeros((4, 4, 3), dtype=np.uint8)]) == [42.0]
    finally:
        # 清理：避免污染全局注册表
        inference._BACKENDS.pop("fake", None)
        inference.reset_cache()


# ---------------------------------------------------------------------------
# 【轻量化 Phase 1】HeuristicBackend（纯 OpenCV，无 torch / 无模型下载）
# ---------------------------------------------------------------------------
def test_heuristic_backend_available():
    assert "heuristic" in inference.backend_names()


def test_heuristic_quality_distinguishes_sharp_from_flat():
    # 清晰（高频噪声）图得分应高于纯平涂图；验证 heuristic 画质分确实反映清晰度
    rng = np.random.default_rng(0)
    sharp = rng.integers(0, 255, size=(128, 128, 3), dtype=np.uint8)
    flat = np.full((128, 128, 3), 128, dtype=np.uint8)
    b = inference.get_backend("heuristic")
    sharp_score, flat_score = b.quality_scores([sharp, flat])
    assert isinstance(sharp_score, float) and isinstance(flat_score, float)
    assert 0.0 <= sharp_score <= 100.0 and 0.0 <= flat_score <= 100.0
    assert sharp_score > flat_score


def test_both_backends_share_opencv_technical_model_name():
    assert inference.HeuristicBackend().quality_model_name() == "opencv-technical-v1"
    torch_backend = object.__new__(inference.TorchBackend)
    assert torch_backend.quality_model_name() == "opencv-technical-v1"


def test_standard_technical_quality_does_not_import_pyiqa():
    sys.modules.pop("pyiqa", None)
    torch_backend = object.__new__(inference.TorchBackend)
    image = np.full((32, 32, 3), 128, dtype=np.uint8)

    scores = torch_backend.quality_scores([image])

    assert len(scores) == 1
    assert isinstance(scores[0], float)
    assert "pyiqa" not in sys.modules


def test_heuristic_scene_and_aesthetics_schema():
    # 返回结构必须与 TorchBackend 完全一致（aesthetic/scene/scene_conf），
    # 否则 scorer / pipeline 会 KeyError
    green = np.full((96, 96, 3), (30, 180, 40), dtype=np.uint8)  # 风光主导（绿）
    b = inference.get_backend("heuristic")
    res = b.scene_and_aesthetics([green])
    assert len(res) == 1
    r = res[0]
    assert set(r.keys()) >= {"aesthetic", "scene", "scene_conf"}
    assert 0.0 <= r["aesthetic"] <= 100.0
    assert 0.0 <= r["scene_conf"] <= 1.0
    valid_scenes = {s.split(" ")[0] for s in config.SCENES} | {"其他"}
    assert r["scene"] in valid_scenes


def test_heuristic_green_image_classified_as_landscape():
    # 高绿占比图应被稳定判为"风光"（粗分阈值下置信度达标）
    green = np.full((96, 96, 3), (30, 180, 40), dtype=np.uint8)
    b = inference.get_backend("heuristic")
    r = b.scene_and_aesthetics([green])[0]
    assert r["scene"] == "风光"
    assert r["scene_conf"] >= config.SCENE_CONF_THRESHOLD


def test_heuristic_grayscale_not_misclassified_as_pet():
    # 灰度/黑白图无彩色相，不应被判为"宠物"（暖色相=0 的历史误判回归）
    gray = np.full((96, 96, 3), (128, 128, 128), dtype=np.uint8)
    b = inference.get_backend("heuristic")
    r = b.scene_and_aesthetics([gray])[0]
    assert r["scene"] != "宠物"


def test_heuristic_is_deterministic():
    img = np.full((64, 64, 3), (120, 90, 200), dtype=np.uint8)
    b = inference.get_backend("heuristic")
    a = b.scene_and_aesthetics([img])[0]
    c = b.scene_and_aesthetics([img])[0]
    assert a == c
