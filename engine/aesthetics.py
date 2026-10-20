# -*- coding: utf-8 -*-
"""
aesthetics.py —— 美学评分（GPU 优先，CPU 兼容）

美学算法（v2 升级，替代早期 CLIP 提示词 softmax）：
    采用 **LAION-Aesthetics 线性美学预测器**（sa_0_4_vit_b_32_linear.pth，
    基于 sac+logos+ava 人工评分数据集训练，约 17.6 万张标注）：
        对 openai/clip-vit-base-patch32 的图像嵌入（512 维，已归一化）做一次
        Linear(512→1)，输出 0-10 美学分，×10 归一为 0-100。
    该头部与场景分类**复用同一个 CLIP 模型、同一次前向**：
        在 analyze_batch() 中一次性得到图像嵌入，同时算美学分与场景，
        头部仅一次 512×1 矩阵乘，几乎零额外开销。

    头部权重文件：models/sa_0_4_vit_b_32_linear.pth（约 3KB，随项目分发）。
    若文件缺失/加载失败，自动降级回 CLIP 提示词 softmax 打分（功能不中断）。

设备策略：torch.cuda.is_available() 时加载到 GPU，否则 CPU 自动降级。

独立命令行调试：python -m engine.aesthetics <图片路径...>
"""
from __future__ import annotations

import os
import sys

import torch
from PIL import Image

from . import config, degradations, loader
from .log import get_logger

_log = get_logger("aesthetics")

# 与设计文档一致的提示词（作为美学头缺失时的降级方案），统一来自 engine/config.py
GOOD = list(config.AESTHETIC_GOOD_PROMPTS)
BAD = list(config.AESTHETIC_BAD_PROMPTS)

# 场景候选（设计文档 4.4.6），取空格前单词作为场景名
SCENES = list(config.SCENES)
SCENE_NAMES = [s.split(" ")[0] for s in SCENES]

# 模型名与缓存（模型权重下载到项目内 .hf_cache，不落 C 盘）
MODEL_NAME = config.CLIP_MODEL_NAME

# LAION-Aesthetics 线性头部（基于 openai/clip-vit-base-patch32 图像嵌入）
HEAD_PATH = config.AESTHETIC_HEAD_PATH

# 单例
_model = None
_proc = None
_device = None
_LOADING = False
_head = None          # (W, b) 已搬到设备
_HEAD_LOADED = None   # True/False/None(未尝试)


def get_device() -> torch.device:
    global _device
    if _device is None:
        _device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    return _device


def _fallback_cpu(reason: str, effect: str) -> torch.device:
    """锁定 CPU 并把已加载的模型/美学头一并搬过去。

    【v0.6 S3-6 / P1-11】GPU 路径失败（显存不足、驱动异常）时必须**真的回退**：
    只改 device 而不搬模型，下一次前向仍会把 CPU 张量喂给 CUDA 权重。
    """
    global _device, _head
    _device = torch.device("cpu")
    if _model is not None:
        try:
            _model.to(_device)
        except Exception:  # pragma: no cover - 回退本身失败只能忍
            _log.warning("CLIP 回退 CPU 失败", exc_info=True)
    if _head is not None:
        try:
            _head = (_head[0].to(_device), _head[1].to(_device))
        except Exception:  # pragma: no cover
            _log.warning("美学头回退 CPU 失败", exc_info=True)
    degradations.record("美学与场景", reason, effect, severity="info")
    return _device


def get_clip():
    """懒加载 CLIP 模型（GPU 优先）。返回 (model, processor, device)。

    【v0.6 S2-2 / G3 离线能力】本地快照完整时**直接以该目录为加载源**：
      * 否则 `from_pretrained` 会向 HF 端点做 HEAD/etag 往返，网络抖动时重试会把一次
        加载从 ~15s 拖到 **499s**（本项目实测），断网时还会直接抛异常；
      * 更关键的是：huggingface_hub 在 import 时就把 `HF_HUB_CACHE` 固化成模块常量，
        只传 `local_files_only=True` 并不能保证它真的去项目内 `.hf_cache` 找 ——
        显式给出目录才彻底摆脱这层时序依赖（详见 models_guard.local_model_source）。
    顺带解决模型权重被动漂移（会改 `_ai_model_signature()` → 触发全库重算，P0-1 雷区）。
    """
    global _model, _proc, _device, _LOADING
    if _model is not None:
        return _model, _proc, get_device()
    if _LOADING:  # 防重入
        raise RuntimeError("CLIP 正在加载中")
    _LOADING = True
    try:
        from transformers import CLIPModel, CLIPProcessor

        from . import models_guard

        local = models_guard.local_model_source(MODEL_NAME)
        source = local or MODEL_NAME
        kwargs = {"local_files_only": True} if local else {}
        _proc = CLIPProcessor.from_pretrained(source, **kwargs)
        _model = CLIPModel.from_pretrained(source, **kwargs)
        try:
            _model.to(get_device())
        except Exception as error:  # noqa: BLE001 —— CUDA OOM / 驱动异常
            # 【v0.6 S3-6 / P1-11】旧实现让 .to(cuda) 的异常直接冒出，整批分析失败；
            # 现在回退 CPU 并上报降级，用户可在分析汇总里看到。
            _log.warning("CLIP 迁移到 %s 失败，回退 CPU：%s", get_device(), error)
            _model.to(_fallback_cpu(
                f"{type(error).__name__}: {error}",
                "本次改用 CPU 推理；结果一致但更慢",
            ))
        _model.eval()
    finally:
        _LOADING = False
    return _model, _proc, get_device()


def get_aesthetic_head():
    """懒加载 LAION 美学线性头部 (W, b)，失败返回 None（走提示词降级）。"""
    global _head, _HEAD_LOADED
    if _HEAD_LOADED is not None:
        return _head
    _HEAD_LOADED = False
    try:
        if not os.path.exists(HEAD_PATH):
            _log.warning("未找到美学头部 %s，使用 CLIP 提示词降级", HEAD_PATH)
            degradations.record(
                "美学评分", f"未找到美学头部 {HEAD_PATH}", "改用 CLIP 提示词打分，精度略低"
            )
            return None
        sd = torch.load(HEAD_PATH, map_location="cpu", weights_only=True)
        dev = get_device()
        _head = (sd["weight"].to(dev), sd["bias"].to(dev))
        _HEAD_LOADED = True
        _log.info("LAION 美学头部已加载：%s", HEAD_PATH)
        return _head
    except Exception as e:
        _log.warning("美学头部加载失败：%s，使用 CLIP 提示词降级", e)
        degradations.record(
            "美学评分", f"{type(e).__name__}: {e}", "改用 CLIP 提示词打分，精度略低"
        )
        return None


def aesthetic_model_name() -> str:
    """当前生效的美学模型名（用于分析汇总展示）。"""
    return "laion-aesthetics(sa_0_4)" if get_aesthetic_head() is not None else "clip-prompt"


def _aesthetic_from_embeds(image_embeds) -> list[float]:
    """用 LAION 线性头把归一化图像嵌入映射为 0-100 美学分。"""
    W, b = get_aesthetic_head()
    # 【v0.6 S3-6】头部在加载时被搬到「当时的 device」；中途 GPU→CPU 回退后两者会
    # 不一致（CPU 嵌入 × CUDA 权重直接报错），这里显式对齐到嵌入所在设备。
    W, b = W.to(image_embeds.device), b.to(image_embeds.device)
    emb = image_embeds / image_embeds.norm(dim=-1, keepdim=True)
    pred = emb.float() @ W.t().float() + b.float()     # (N,1)，约 0-10
    a = (pred[:, 0].cpu().numpy() * 10.0)
    return [float(min(100.0, max(0.0, v))) for v in a]


def _prep_inputs(images, texts):
    """把 PIL 图列表 + 文本列表整理为模型输入（已搬到目标设备）。"""
    model, proc, device = get_clip()
    inputs = proc(text=texts, images=images, return_tensors="pt", padding=True)
    inputs = {k: v.to(device) for k, v in inputs.items()}
    return inputs


def _forward_and_score(images, all_texts: list[str], n_good: int, n_bad: int, n_scene: int):
    """跑一次 CLIP 前向，返回 (每张的场景分数向量, 每张美学分)。"""
    model, _processor, _device_ = get_clip()
    with torch.no_grad():
        inputs = _prep_inputs(images, all_texts)
        out = model(**inputs)
        logits = out.logits_per_image                     # (N, n_text)

        # 美学：优先 LAION 线性头（复用同一次前向的图像嵌入）
        use_head = get_aesthetic_head() is not None
        if use_head and getattr(out, "image_embeds", None) is not None:
            aesthetic = _aesthetic_from_embeds(out.image_embeds)
        else:
            ab = logits[:, : n_good + n_bad].softmax(dim=1)   # (N, 2+2) 降级方案
            aesthetic = (ab[:, :n_good].sum(dim=1) * 100.0).cpu().tolist()

        # 场景：对 SCENES 部分做 softmax
        sc = logits[:, n_good + n_bad:].softmax(dim=1)    # (N, 7)
        scores = sc.cpu().tolist()
    return scores, aesthetic


def analyze_batch(images) -> list[dict]:
    """对一批 PIL 图像做一次前向，同时返回美学分与场景分类。

    参数：images 为 PIL.Image 列表（RGB）。
    返回：每张一个 dict：
        aesthetic  0-100 美学分（LAION 美学头；缺失时降级 CLIP 提示词）
        scene      四类候选场景（低置信度时仍保留候选）
        scene_conf 七条提示词最高项的置信度（0-1）
    """
    n = len(images)
    if n == 0:
        return []
    all_texts = GOOD + BAD + SCENES
    n_good, n_bad, n_scene = len(GOOD), len(BAD), len(SCENES)
    try:
        scores, aesthetic = _forward_and_score(images, all_texts, n_good, n_bad, n_scene)
    except RuntimeError as error:
        # 【v0.6 S3-6 / P1-11】CUDA 运行时错误（典型为显存不足）→ 锁定 CPU 重试一次。
        # 只在确实还在 CUDA 上时才重试，避免掩盖真正的 CPU 侧缺陷。
        if get_device().type != "cuda":
            raise
        _log.warning("CLIP 前向在 CUDA 上失败，回退 CPU 重试：%s", error)
        _fallback_cpu(
            f"{type(error).__name__}: {error}", "本次改用 CPU 推理；结果一致但更慢"
        )
        scores, aesthetic = _forward_and_score(images, all_texts, n_good, n_bad, n_scene)
    results = []
    for i in range(n):
        idx = max(range(n_scene), key=lambda k: scores[i][k])
        conf = scores[i][idx]
        scene = config.scene_label(SCENE_NAMES[idx])
        results.append({"aesthetic": float(aesthetic[i]), "scene": scene, "scene_conf": float(conf)})
    return results


def aesthetic_score_batch(images) -> list[float]:
    """仅返回每张的美学分。"""
    return [r["aesthetic"] for r in analyze_batch(images)]


def aesthetic_score(path: str) -> float:
    """按路径计算美学分。"""
    img = loader.load_image(path, max_size=512)
    if img is None:
        return 0.0
    return analyze_batch([img])[0]["aesthetic"]


# ---------------------------------------------------------------------------
# 命令行调试入口
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    imgs = [loader.load_image(p, max_size=512) for p in sys.argv[1:]]
    for p, r in zip(sys.argv[1:], analyze_batch([x for x in imgs if x is not None])):
        print(f"{os.path.basename(p)}: aesthetic={r['aesthetic']:.1f} "
              f"scene={r['scene']} conf={r['scene_conf']:.3f}")
