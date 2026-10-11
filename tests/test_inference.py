# -*- coding: utf-8 -*-
"""推理后端抽象层回归测试。

只验证"接缝"本身与空输入路径，不触发任何 AI 模型下载/推理，
因此运行快、且不需要联网或 GPU。

【v0.6 S2-1】HeuristicBackend 与 config.INFERENCE_BACKEND 已删除：
  * 后端是显式参数（`get_backend("torch")` / `analyze_directory(..., backend=...)`），
    不再有"从 config 读默认值"这个环节，因此相关断言改为对齐 `inference.DEFAULT_BACKEND`；
  * 原先为轻量环境准备的 `skipif(torch 缺失)` 一并去掉 —— 本项目只发行 Torch 版，
    torch 缺失就是真实故障，不该被静默跳过。
"""
import sys

import numpy as np
import pytest

from engine import inference


def test_backend_names_includes_torch():
    assert "torch" in inference.backend_names()


def test_default_backend_is_torch_and_has_no_hidden_switch():
    assert inference.DEFAULT_BACKEND == "torch"
    assert inference.get_backend().name == inference.DEFAULT_BACKEND
    # 轻量后端与配置开关必须彻底消失，防止有人重新引入第二套后端
    assert "heuristic" not in inference.backend_names()
    assert not hasattr(inference, "HeuristicBackend")


def test_get_backend_unknown_raises():
    with pytest.raises(ValueError):
        inference.get_backend("no-such-backend")


def test_empty_inputs_return_empty_lists():
    # 空输入不应触发模型加载，直接返回等长空结果
    backend = inference.get_backend("torch")
    assert backend.quality_scores([]) == []
    assert backend.scene_and_aesthetics([]) == []


def test_quality_scores_delegates_to_torch_backend():
    # 单张合成图也应走通（torch 后端会真正加载画质模型并打分）
    img = np.zeros((64, 64, 3), dtype=np.uint8)
    scores = inference.get_backend("torch").quality_scores([img])
    assert isinstance(scores, list) and len(scores) == 1
    # 纯黑图模型分可能为 None 或数值；只断言类型安全
    assert scores[0] is None or isinstance(scores[0], float)


def test_register_and_select_onnx_backend():
    # 验证"换后端只需注册 + 传参"的扩展点可用（不依赖 torch/onnx 实际安装），
    # 也是 v0.7 ONNX 后端的接入演练
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


def test_torch_backend_shares_opencv_technical_model_name():
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
