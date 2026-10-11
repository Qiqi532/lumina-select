# -*- coding: utf-8 -*-
"""aesthetics.py 降级路径单测（v0.6 S3-6 / P1-11）。

覆盖三处"GPU 优先"设计在 GPU 不可用/显存不足时的真实回退行为：

  1. ``_fallback_cpu`` 必须**真的把模型和美学头搬到 CPU**，而不只是改一个 device
     变量 —— 旧实现只改变量，下一次前向会把 CPU 张量喂给 CUDA 权重直接报错；
  2. ``_aesthetic_from_embeds`` 必须把权重对齐到嵌入所在设备 —— 头部是在"加载时的
     device"上创建的，中途回退后两者会不一致；
  3. ``analyze_batch`` 在 CUDA 前向抛 RuntimeError（典型 OOM）时必须回退 CPU 重试
     **一次**，且只在确实还在 CUDA 上时才重试（不掩盖 CPU 侧真缺陷）。

torch 不可用的环境下整模块跳过：这些路径本身以 torch 为前提。
"""
from __future__ import annotations

import pytest

torch = pytest.importorskip("torch")

from engine import aesthetics, degradations  # noqa: E402
from PIL import Image  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch):
    """隔离单例与降级列表，防止用例互相污染 / 污染全局单例。"""
    degradations.drain()
    monkeypatch.setattr(aesthetics, "_device", None, raising=False)
    monkeypatch.setattr(aesthetics, "_model", None, raising=False)
    monkeypatch.setattr(aesthetics, "_head", None, raising=False)
    yield
    degradations.drain()


class _FakeModule:
    """只关心 .to(device) 的最小替身。"""

    def __init__(self):
        self.moved_to = None

    def to(self, device):
        self.moved_to = device
        return self


def test_fallback_cpu_moves_model_and_head_and_records_degradation(monkeypatch):
    monkeypatch.setattr(aesthetics, "_device", torch.device("cuda"))
    model, head = _FakeModule(), _FakeModule()
    monkeypatch.setattr(aesthetics, "_model", model)
    monkeypatch.setattr(aesthetics, "_head", (head, head))

    device = aesthetics._fallback_cpu("OutOfMemoryError: CUDA out of memory",
                                     "本次改用 CPU 推理；结果一致但更慢")

    assert device.type == "cpu"
    assert aesthetics.get_device().type == "cpu", "device 必须被锁定为 CPU"
    assert model.moved_to is not None and model.moved_to.type == "cpu", (
        "只改 device 而不搬模型 → 下一次前向 CPU 张量 × CUDA 权重直接报错"
    )
    assert aesthetics._head[0].moved_to.type == "cpu", "美学头也要一起搬"
    assert degradations.drain() == [{
        "stage": "美学与场景",
        "reason": "OutOfMemoryError: CUDA out of memory",
        "effect": "本次改用 CPU 推理；结果一致但更慢",
    }]


def test_fallback_cpu_is_safe_when_nothing_loaded_yet(monkeypatch):
    """回退可能发生在模型尚未加载时（装配阶段失败）：不许因此抛异常。"""
    monkeypatch.setattr(aesthetics, "_device", torch.device("cuda"))

    assert aesthetics._fallback_cpu("boom", "effect").type == "cpu"
    assert len(degradations.drain()) == 1


def test_aesthetic_from_embeds_aligns_head_to_embed_device(monkeypatch):
    """头部在 CPU、嵌入在别处（或反之）时不得报设备不匹配。"""
    weight = torch.ones(1, 2)
    bias = torch.zeros(1)
    monkeypatch.setattr(aesthetics, "get_aesthetic_head", lambda: (weight, bias))

    # 归一化后为 [0.6, 0.8]，与全 1 权重点积 = 1.4 → ×10 = 14.0（clip 到 0-100）
    scores = aesthetics._aesthetic_from_embeds(torch.tensor([[3.0, 4.0]]))

    assert scores == pytest.approx([14.0], abs=1e-4)


def test_analyze_batch_retries_once_on_cuda_runtime_error(monkeypatch):
    """CUDA 前向 OOM → 回退 CPU 重试一次，并上报降级；结果照常返回。"""
    monkeypatch.setattr(aesthetics, "get_device", lambda: torch.device("cuda"))
    calls = {"n": 0}

    def flaky(images, all_texts, n_good, n_bad, n_scene):   # noqa: ARG001
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("CUDA out of memory. Tried to allocate 2.00 GiB")
        return ([[0.9] + [0.0] * (n_scene - 1) for _ in images], [55.0] * len(images))

    monkeypatch.setattr(aesthetics, "_forward_and_score", flaky)

    result = aesthetics.analyze_batch([Image.new("RGB", (16, 16))])

    assert calls["n"] == 2, "必须回退后重试一次"
    assert result[0]["aesthetic"] == pytest.approx(55.0)
    events = degradations.drain()
    assert events and events[0]["stage"] == "美学与场景"


def test_analyze_batch_does_not_retry_on_cpu_runtime_error(monkeypatch):
    """已经在 CPU 上还抛 RuntimeError → 那是真缺陷，必须原样冒出（不许吞掉重试）。"""
    monkeypatch.setattr(aesthetics, "get_device", lambda: torch.device("cpu"))
    calls = {"n": 0}

    def always_boom(images, all_texts, n_good, n_bad, n_scene):   # noqa: ARG001
        calls["n"] += 1
        raise RuntimeError("mat1 and mat2 shapes cannot be multiplied")

    monkeypatch.setattr(aesthetics, "_forward_and_score", always_boom)

    with pytest.raises(RuntimeError):
        aesthetics.analyze_batch([Image.new("RGB", (16, 16))])

    assert calls["n"] == 1, "CPU 侧错误不得被当作 OOM 重试"
    assert degradations.drain() == []
