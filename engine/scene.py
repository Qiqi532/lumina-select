# -*- coding: utf-8 -*-
"""
scene.py —— CLIP 场景分类（人像/风景/街拍/其他，GPU 优先）

算法（设计文档 4.4.6）：
    用 CLIP zero-shot 分类候选场景文本，取 softmax 最高者；输出场景名与置信度。
    低置信度仍保留候选类别，由评分和界面标记为待确认。

说明：场景分类复用 aesthetics 中已加载的同一 CLIP 模型（一次前向同时得到
美学分与场景，见 aesthetics.analyze_batch）。本模块提供独立的场景分类接口，
便于单独调用/调试。

独立命令行调试：python -m engine.scene <图片路径...>
"""
from __future__ import annotations

import os
import sys

from . import config, loader
from .aesthetics import analyze_batch, get_clip


def classify_batch(images) -> list[tuple[str, float]]:
    """对 PIL 图像列表做场景分类，返回 [(scene, conf), ...]。"""
    return [(r["scene"], r["scene_conf"]) for r in analyze_batch(images)]


def scene_classify(path: str) -> tuple[str, float]:
    """按路径分类场景，返回 (scene, conf)。"""
    img = loader.load_image(path, max_size=512)
    if img is None:
        return "", 0.0
    return classify_batch([img])[0]


def available_scenes() -> list[str]:
    return list(config.SCENE_LABELS)


# ---------------------------------------------------------------------------
# 命令行调试入口
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    for p in sys.argv[1:]:
        s, c = scene_classify(p)
        print(f"{os.path.basename(p)}: scene={s} conf={c:.3f}")
