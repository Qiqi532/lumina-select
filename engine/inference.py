# -*- coding: utf-8 -*-
"""
inference.py —— 推理后端抽象层

【v0.6 S2-1 收敛】本项目自 v0.6 起**只有一个发行版本**（标准版，Torch CLIP/ViT）。
原先的 `HeuristicBackend`（纯 OpenCV 轻量版）与 `config.INFERENCE_BACKEND` 全局开关
已删除，原因：

  * 轻量后端没有图像嵌入能力，而 L3 排序层 / L4 个性化层都以嵌入为输入，
    保留它等于把路线图锁死（见 README「已确认的架构方向」）；
  * 用进程级全局变量切换后端，会让"谁在什么时候改了后端"完全不可追踪 ——
    已有的两次真实故障（切后端导致人工复核被清库、UI 永久改写进程后端）都源于此。

现在后端是**调用参数**，不再是进程状态：
    analyze_directory(root, db, backend="torch")

本模块保留 Protocol + 注册表，是为了给 v0.7 的 ONNX 后端留一个明确的接入点
（`register_backend("onnx", OnnxBackend)` 即可，业务代码不用动）。

设计要点：
  * import 时不引入 torch；torch 仅在具体后端方法内部按需 import，
    这样无 torch 的环境仍能 import 本模块（便于静态检查）；
  * 后端实例惰性创建并按名缓存（AI 模型是单例，避免重复加载）。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

import numpy as np
from PIL import Image

from .quality_metrics import MODEL_NAME as TECHNICAL_MODEL_NAME
from .quality_metrics import evaluate_technical_quality_batch

# 默认后端。本项目只发行这一个；保留常量是为了让调用方有唯一的"默认"来源，
# 而不是各自硬编码 "torch"。
DEFAULT_BACKEND = "torch"


@runtime_checkable
class InferenceBackend(Protocol):
    """推理后端协议：所有后端必须实现以下方法。"""
    name: str

    def quality_scores(self, rgbs: list[np.ndarray]) -> list[float | None]:
        """批量画质分（0-100，越高越好）；模型不可用时返回等长 None 列表。"""
        ...

    def scene_and_aesthetics(self, images: list[Image.Image]) -> list[dict]:
        """批量美学 + 场景；返回 [{aesthetic, scene, scene_conf}, ...]。"""
        ...

    def quality_model_name(self) -> str:
        """当前生效画质模型名（参与 AI 模型签名，变更触发全量重算）。"""
        ...

    def aesthetic_model_name(self) -> str:
        """当前生效美学模型名（参与 AI 模型签名）。"""
        ...


class TorchBackend:
    """标准后端：Torch CLIP/ViT + 共享 OpenCV 技术质量评分。"""

    name = "torch"

    def __init__(self):
        # 延迟 import：避免无 torch 环境 import 本模块即失败
        from .aesthetics import aesthetic_model_name, analyze_batch

        self._analyze_batch = analyze_batch
        self._aesthetic_model_name = aesthetic_model_name

    def quality_scores(self, rgbs: list[np.ndarray]) -> list[float | None]:
        if not rgbs:
            return []
        return [result.overall for result in evaluate_technical_quality_batch(rgbs)]

    def scene_and_aesthetics(self, images: list[Image.Image]) -> list[dict]:
        if not images:
            return []
        return self._analyze_batch(images)

    def quality_model_name(self) -> str:
        return TECHNICAL_MODEL_NAME

    def aesthetic_model_name(self) -> str:
        return self._aesthetic_model_name()


# 后端注册表（v0.7 用 register_backend 接入 onnx 后端）
_BACKENDS: dict[str, type] = {"torch": TorchBackend}
_CACHE: dict[str, InferenceBackend] = {}


def register_backend(name: str, cls: type) -> None:
    """接入新后端（如 ONNX）：register_backend('onnx', OnnxBackend)。"""
    _BACKENDS[name] = cls


def backend_names() -> list[str]:
    return list(_BACKENDS.keys())


def get_backend(name: str | None = None) -> InferenceBackend:
    """返回指定后端实例（None → DEFAULT_BACKEND），惰性缓存。

    后端名是**显式参数**，不再从进程级配置读取 —— 见模块 docstring。
    """
    name = name or DEFAULT_BACKEND
    if name not in _BACKENDS:
        raise ValueError(f"未知推理后端 {name!r}，可选：{backend_names()}")
    if name not in _CACHE:
        _CACHE[name] = _BACKENDS[name]()
    return _CACHE[name]


def reset_cache() -> None:
    """测试 / 运行期重新加载模型时用。"""
    _CACHE.clear()


if __name__ == "__main__":
    print("可用后端：", backend_names())
    print("默认后端：", DEFAULT_BACKEND)
    print("实例：", get_backend().name)
