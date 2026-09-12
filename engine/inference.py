# -*- coding: utf-8 -*-
"""
inference.py —— 推理后端抽象层（轻量化 Phase 0 接缝）

目的：把"需要 torch / transformers 的深度学习推理"从业务流水线里隔离出来，
统一收敛到本模块。后续换轻量后端（ONNX Runtime / 纯 OpenCV 启发式）时，
**只需新增一个 Backend 实现并把 config.INFERENCE_BACKEND 切过去**，
pipeline.py 与 scorer.py 等业务代码完全不用动。

当前默认后端 torch_backend（沿用既有 quality.py / aesthetics.py，行为不变）。

设计要点：
  * 本模块在 import 时不引入 torch；torch 仅在具体后端方法内部按需 import，
    这样未来 onnx 后端即使在无 torch 的环境也能 import 本模块。
  * 后端实例惰性创建并缓存（AI 模型是单例，避免重复加载）。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

import cv2
import numpy as np
from PIL import Image

from . import config
from .quality_metrics import MODEL_NAME as TECHNICAL_MODEL_NAME
from .quality_metrics import evaluate_technical_quality_batch


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
    """标准后端：Torch CLIP/ViT + shared OpenCV technical quality."""

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


class HeuristicBackend:
    """轻量后端：纯 OpenCV + numpy 启发式，无 torch / 无模型下载 / 完全离线。

    用于"轻量桌面版"分发（exe 去掉 torch/transformers/pyiqa 后约 150MB、秒级启动）。
    算法说明（非深度学习，精度低于 LAION/MUSIQ，但由人工复核环节兜底）：
      * quality_scores：融合 清晰度(Laplacian) + 曝光均衡 + 对比度 + 低噪点 的 0-100 综合分；
      * scene_and_aesthetics：用肤色占比/色彩/边缘密度等特征对 7 类场景做粗分，
        置信度低于 config.SCENE_CONF_THRESHOLD 归入"其他"；
        美学分：融合 清晰度 + 曝光 + 对比度 + 色彩丰富度 + 三分法构图 的 0-100 启发式分。

    本后端 import 时不引入 torch，满足轻量 exe 的运行期依赖约束。
    """

    name = "heuristic"

    def __init__(self):
        # 场景置信度阈值与 config 对齐（低于阈值归入"其他"），保证 UI 筛选口径一致
        self._scene_conf_threshold = config.SCENE_CONF_THRESHOLD

    # ---- InferenceBackend 协议实现 ----
    def quality_scores(self, rgbs: list[np.ndarray]) -> list[float | None]:
        if not rgbs:
            return []
        return [result.overall for result in evaluate_technical_quality_batch(rgbs)]

    def scene_and_aesthetics(self, images: list[Image.Image]) -> list[dict]:
        if not images:
            return []
        out = []
        for im in images:
            # 兼容两种输入：流水线传 PIL.Image；离线测试可能直接传 np.ndarray
            if isinstance(im, np.ndarray):
                rgb = im if im.dtype == np.uint8 else im.astype(np.uint8)
            else:
                rgb = np.asarray(im.convert("RGB"), dtype=np.uint8)
            out.append(self._scene_and_aesthetic(rgb))
        return out

    def quality_model_name(self) -> str:
        return TECHNICAL_MODEL_NAME

    def aesthetic_model_name(self) -> str:
        return "opencv-heuristic"

    # ---- 内部：特征提取 ----
    @staticmethod
    def _features(rgb: np.ndarray) -> dict:
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        gray = cv2.resize(gray, (512, 512))
        lap = cv2.Laplacian(gray, cv2.CV_64F)
        blur = float(lap.var())
        sharp = 1.0 - float(np.exp(-blur / 200.0))          # 0..1，模糊越低分越低
        hist = cv2.calcHist([gray], [0], None, [256], [0, 256]).flatten()
        total = float(gray.size)
        over = float(hist[245:].sum()) / total
        under = float(hist[:10].sum()) / total
        exp_pen = max(0.0, over - 0.05) + max(0.0, under - 0.05)
        exp_q = max(0.0, 1.0 - exp_pen * 2.0)               # 曝光均衡度 0..1
        contrast_q = min(1.0, float(gray.std()) / 80.0)
        noise = float(np.abs(lap).mean()) / 255.0
        noise_q = max(0.0, 1.0 - noise * 4.0)

        small = cv2.resize(rgb, (256, 256))
        hsv = cv2.cvtColor(small, cv2.COLOR_RGB2HSV)
        sat = float(hsv[:, :, 1].mean()) / 255.0
        hue = hsv[:, :, 0]
        green_blue = float(((hue > 35) & (hue < 130)).mean())
        warm = float(((hue < 20) | (hue > 150)).mean())
        ycrcb = cv2.cvtColor(small, cv2.COLOR_RGB2YCrCb)
        cr = ycrcb[:, :, 1].astype(np.int16)
        cb = ycrcb[:, :, 2].astype(np.int16)
        skin_ratio = float(((cr > 135) & (cr < 180) & (cb > 85) & (cb < 135)).mean())
        edges = cv2.Canny(gray, 50, 150)
        edge_density = float(edges.mean()) / 255.0
        r = rgb[:, :, 0].astype(np.float32); g = rgb[:, :, 1].astype(np.float32); b = rgb[:, :, 2].astype(np.float32)
        colorfulness = float(np.mean(np.sqrt((r - g) ** 2 + (0.5 * (r + g) - b) ** 2)) / 255.0)
        return {"gray": gray, "sharp": sharp, "exp_q": exp_q, "contrast_q": contrast_q,
                "noise_q": noise_q, "sat": sat, "green_blue": green_blue, "warm": warm,
                "skin_ratio": skin_ratio, "edge_density": edge_density, "colorfulness": colorfulness}

    def _quality_score(self, rgb: np.ndarray) -> float:
        return evaluate_technical_quality_batch([rgb])[0].overall

    def _scene_and_aesthetic(self, rgb: np.ndarray) -> dict:
        f = self._features(rgb)
        sr = f["skin_ratio"]
        scores = {
            "人像": min(1.0, sr / 0.22),
            "风光": min(1.0, f["green_blue"] / 0.40) * (1.0 - min(1.0, sr / 0.10)),
            "建筑/城市": min(1.0, f["edge_density"] / 0.10) * (1.0 - min(1.0, f["sat"] / 0.30)),
            "街拍/纪实": min(1.0, (sr + f["edge_density"]) / 2.0) * (1.0 - f["green_blue"]),
            "宠物": min(1.0, f["warm"] / 0.30) * (1.0 - f["green_blue"]) * (1.0 - min(1.0, sr / 0.30))
                    * min(1.0, f["sat"] / 0.12),  # 无彩色（灰度/黑白）按"其他"兜底，避免暖色相误判
            "静物/美食": min(1.0, f["sat"] / 0.40) * min(1.0, f["warm"] / 0.30)
                        * (1.0 - min(1.0, f["edge_density"] / 0.10)),
        }
        best = max(scores, key=scores.get)
        best_v = scores[best]
        scene = best if best_v >= self._scene_conf_threshold else "其他"
        # 美学：技术质量 + 色彩丰富度 + 三分法构图（中心能量越低 → 构图越分散越好）
        gray = f["gray"]
        h, w = gray.shape
        center = gray[h // 3:2 * h // 3, w // 3:2 * w // 3]
        comp_q = max(0.0, min(1.0, 1.0 - (float(center.std()) / (float(gray.std()) + 1e-6))))
        aesthetic = 100.0 * (0.30 * f["sharp"] + 0.20 * f["exp_q"]
                             + 0.20 * f["contrast_q"] + 0.15 * f["colorfulness"] + 0.15 * comp_q)
        return {"aesthetic": max(0.0, min(100.0, aesthetic)),
                "scene": scene, "scene_conf": float(best_v)}


# 后端注册表（Phase 1 用 register_backend 接入 onnx 后端）
_BACKENDS: dict[str, type] = {"torch": TorchBackend, "heuristic": HeuristicBackend}
_CACHE: dict[str, InferenceBackend] = {}


def register_backend(name: str, cls: type) -> None:
    """接入新后端（如 ONNX）：register_backend('onnx', OnnxBackend)。"""
    _BACKENDS[name] = cls


def backend_names() -> list[str]:
    return list(_BACKENDS.keys())


def get_backend(name: str | None = None) -> InferenceBackend:
    """返回当前生效后端（按 config.INFERENCE_BACKEND，默认 torch），惰性缓存。"""
    from . import config

    name = name or config.INFERENCE_BACKEND
    if name not in _BACKENDS:
        raise ValueError(f"未知推理后端 {name!r}，可选：{backend_names()}")
    if name not in _CACHE:
        _CACHE[name] = _BACKENDS[name]()
    return _CACHE[name]


# ---- 业务侧统一入口（pipeline / scorer 等只调这些）----
def quality_scores(rgbs: list[np.ndarray]) -> list[float | None]:
    return get_backend().quality_scores(rgbs)


def scene_and_aesthetics(images: list[Image.Image]) -> list[dict]:
    return get_backend().scene_and_aesthetics(images)


def quality_model_name() -> str:
    return get_backend().quality_model_name()


def aesthetic_model_name() -> str:
    return get_backend().aesthetic_model_name()


def reset_cache() -> None:
    """测试 / 运行期重新加载模型时用。"""
    _CACHE.clear()


if __name__ == "__main__":
    print("可用后端：", backend_names())
    print("当前后端：", get_backend().name)
