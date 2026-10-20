# -*- coding: utf-8 -*-
"""
faces.py —— MediaPipe 人脸检测与闭眼判定（EAR）

算法（设计文档 4.4.5）：
    MediaPipe FaceMesh 输出 478 个面部关键点，左右眼各 6 个关键点计算
    眼睛纵横比（Eye Aspect Ratio, EAR）：
        EAR = (|p2-p6| + |p3-p5|) / (2 * |p1-p4|)
    闭眼时 EAR 明显下降，EAR < 0.20（可配置）视为闭眼。

【Windows 中文路径适配（重要）】
    mediapipe 的 C++ 层读取模型文件（.binarypb/.tflite）使用窄字符 API，
    无法读取含中文的路径（与 OpenCV cv2.imread 同类问题）。本项目路径
    （例如 D:\\PHOTO\\Lumia\\照片\\...）含中文，因此：
    1) 已对 .venv 内 mediapipe/__init__.py 注释掉 tasks 导入（避免连锁导入
       tensorflow，tensorflow 与本项目 venv 的 protobuf 3.20.3 冲突）；
    2) 本模块在加载 mediapipe 前，若发现其安装路径含非 ASCII 字符，会自动
       创建 ASCII 路径 junction（<盘符>:\\photocull_mp_link → site-packages）
       并从该 ASCII 路径重新导入 mediapipe，保证模型可正常加载。

独立命令行调试：python -m engine.faces <图片路径>
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys

# 抑制 mediapipe / TensorFlow Lite 的冗余日志输出（写入 stderr，仅噪音）
os.environ.setdefault("GLOG_minloglevel", "2")
try:
    from absl import logging as _absl_logging
    _absl_logging.set_verbosity(_absl_logging.ERROR)
except Exception:
    pass

import numpy as np

from . import config
from .log import get_logger
from .quality_metrics import evaluate_technical_quality

# ---------------------------------------------------------------------------
# 常量（统一来自 engine/config.py，与界面/引擎口径一致）
# ---------------------------------------------------------------------------
LEFT_EYE = [33, 160, 158, 133, 153, 144]    # 左眼关键点索引
RIGHT_EYE = [362, 385, 387, 263, 373, 380]  # 右眼关键点索引
EAR_CLOSED_THRESHOLD = config.EAR_CLOSED_THRESHOLD  # EAR < 0.20 视为闭眼
# 人脸检测失败时 face 维度取中性值（文档 5.4：中性值 0.5，不做闭眼硬判）
NEUTRAL_FACE_N = config.FACE_NEUTRAL_N

_log = get_logger("faces")

# 【模型升级 v2.2】闭眼检测：MediaPipe EAR + 数据集训练 ViT 分类器融合
# dima806/closed_eyes_image_detection（ViT，Apache-2.0，99% 精度，输入为眼睛区域裁剪图）
EYE_MODEL_NAME = config.EYE_MODEL_NAME
EYE_MODEL_CONF = config.EYE_MODEL_CONF       # 分类器闭眼置信度阈值
EYE_CROP_CONTEXT = config.EYE_CROP_CONTEXT   # 眼睛裁剪的上下文放大系数
# 分类器触发区间：仅当 EAR 处于“边界/可疑”区间才跑 ViT 分类器
EYE_MODEL_EAR_LO = config.EYE_MODEL_EAR_LO
EYE_MODEL_EAR_HI = config.EYE_MODEL_EAR_HI

_MEDIAPIPE_READY = False
_MEDIAPIPE_FAILED = False


def _is_ascii(s: str) -> bool:
    return all(ord(c) < 128 for c in s)


def _create_junction(link: str, target: str) -> bool:
    """创建目录 junction（无需管理员权限）。失败返回 False。

    注意：mklink 输出为系统 ANSI 编码（GBK），不能按 utf-8 解码，这里按字节
    捕获并忽略输出，仅以结果路径是否存在判断成败。
    """
    try:
        if os.path.isdir(link):
            return os.path.isdir(os.path.join(link, "mediapipe"))
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", link, target],
            capture_output=True,  # 字节捕获，避免解码崩溃
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
        return os.path.isdir(os.path.join(link, "mediapipe"))
    except Exception:
        return False


def _junction_path(site_packages: str) -> str:
    """Return a stable ASCII junction path unique to one Python environment."""
    normalized = os.path.normcase(os.path.abspath(site_packages))
    suffix = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
    drive = os.path.splitdrive(normalized)[0] or "D:"
    return os.path.join(drive + os.sep, f"photocull_mp_{suffix}")


def _find_mediapipe_site_packages() -> list[str]:
    """返回可能包含 mediapipe 包的 site-packages 目录列表（按优先级）。"""
    candidates: list[str] = []
    # 1) venv 自身 site-packages（Windows 布局）
    sp = os.path.join(sys.prefix, "Lib", "site-packages")
    if os.path.isdir(os.path.join(sp, "mediapipe")):
        candidates.append(sp)
    # 2) 其他 sys.path 中含 mediapipe 的目录（含 --system-site-packages 继承）
    for p in sys.path:
        if p and os.path.isdir(os.path.join(p, "mediapipe")) and p not in candidates:
            candidates.append(p)
    return candidates


def _redirect_frozen_resource_root(link: str | None) -> None:
    """Point FaceMesh's native model lookup at the ASCII alias in frozen builds."""
    if not link or not getattr(sys, "frozen", False):
        return
    from mediapipe.python import solution_base

    if _is_ascii(solution_base.__file__):
        return
    model = os.path.join(
        link, "mediapipe", "modules", "face_landmark", "face_landmark_front_cpu.binarypb",
    )
    if os.path.isfile(model):
        solution_base.__file__ = os.path.join(link, "mediapipe", "python", "solution_base.py")


def _ensure_mediapipe():
    """确保 mediapipe 在【首次 import 前】从 ASCII 路径加载。

    原因：
      1) mediapipe 的 C++ 层用窄字符 API 读模型文件，无法读含中文的路径
         （与 OpenCV cv2.imread 同类问题），本项目路径含中文；
      2) 不能在 import 后再“删除 sys.modules 重导”——pybind11 类型注册表
         不会随重导重置（会报 generic_type already registered）。
    做法：在首次 import mediapipe 前，把 ASCII junction 路径插入 sys.path[0]。
    """
    global _MEDIAPIPE_READY, _MEDIAPIPE_FAILED, _face_mesh
    if _MEDIAPIPE_READY:
        return True
    if _MEDIAPIPE_FAILED:
        return False
    try:
        # 首次 import 之前完成路径准备
        link = None
        for sp in _find_mediapipe_site_packages():
            mp_dir = os.path.join(sp, "mediapipe")
            if _is_ascii(mp_dir):
                break  # 路径已 ASCII，直接正常 import
            link = _junction_path(sp)
            if not os.path.isdir(os.path.join(link, "mediapipe")):
                _create_junction(link, sp)
            if link not in sys.path:
                sys.path.insert(0, link)
            break
        import mediapipe  # noqa: F401
        from mediapipe.python.solutions import face_mesh
        _redirect_frozen_resource_root(link)
        _face_mesh = face_mesh
        _MEDIAPIPE_READY = True
        return True
    except Exception as error:
        _MEDIAPIPE_READY = False
        _MEDIAPIPE_FAILED = True
        package = sys.modules.get("mediapipe")
        bindings = sys.modules.get("mediapipe.python._framework_bindings")
        _log.warning(
            "MediaPipe 初始化失败：%s: %s（package=%s, bindings=%s, runtime=%s）",
            type(error).__name__, error,
            getattr(package, "__file__", None), getattr(bindings, "__file__", None),
            getattr(sys, "_MEIPASS", None),
        )
        return False


_face_mesh = None
_mesh = None   # 复用的 FaceMesh 实例（避免每张图重复初始化/加载模型）

# 【修复 v0.4】旧实现把 ASCII 路径准备放在【首次 detect 调用时】才做。
# 只要有任何代码在此之前 import 了 mediapipe（例如别处 `from mediapipe.python.solutions
# import face_mesh`），mediapipe 就会按原始中文路径解析内置模型文件，随后稳定地报
#   "The path does not exist: ...\mediapipe\modules\face_landmark\face_landmark_front_cpu.binarypb"
# 实测可复现。改为【模块导入即完成准备】，彻底消除顺序依赖。
_ensure_mediapipe()


def _get_mesh():
    """获取（并缓存）FaceMesh 实例。单线程流水线下复用安全。

    【修复 v0.4】旧实现在 detect_face_and_eyes() 内部另起一行
    `_fm.FaceMesh(...)` 新建实例且从不 close()，实测"调用 N 次 → 新建 N 个实例"。
    每个实例都会重新加载一份 .binarypb 模型并占用未被回收的原生资源，
    在连拍大批量人像上是持续的内存与耗时泄漏。全部改用本单例。
    """
    global _mesh
    if _mesh is None:
        _mesh = _face_mesh.FaceMesh(static_image_mode=True, max_num_faces=5, refine_landmarks=False)
    return _mesh


def _detect_landmarks(rgb: np.ndarray) -> list | None:
    """复用单例跑一次 FaceMesh，返回全部人脸关键点列表（无人脸返回 None）。"""
    if not _ensure_mediapipe():
        return None
    if rgb is None or rgb.size == 0:
        return None
    mesh = _get_mesh()
    res = mesh.process(np.ascontiguousarray(rgb, dtype=np.uint8))
    if not res.multi_face_landmarks:
        return None
    return [[(p.x, p.y) for p in lm.landmark] for lm in res.multi_face_landmarks]


def _ear(pts, size: tuple[int, int] | None = None) -> float:
    """计算单个眼睛的纵横比（EAR）。pts 为 6 个关键点。

    【v0.6 S3-1 / P1-3】EAR 是「垂直距离 / 水平距离」的**比值**，只有把关键点
    还原到**像素口径**才有物理意义。MediaPipe 给的是归一化坐标 ``(x/w, y/h)``：
    在非方图上直接按归一化坐标相除，会把横向与纵向两个不同的缩放混进同一个比值
    —— 同一张脸在 3:2 与 2:3 画幅下会算出**不同的 EAR**，让 ``EAR_CLOSED_THRESHOLD``
    这类绝对值阈值失去可比性（横构图会被系统性高估、竖构图被低估）。

    参数
    ----
    pts : 6 个 ``(x, y)`` 关键点，**归一化坐标**（MediaPipe 原生输出）
    size : ``(w, h)`` 像素尺寸。生产调用**必须传**；传 ``None`` 时退化为归一化口径
           （仅为不关心画幅比的纯数学场景保留，非方图上结果会被画幅比污染）
    """
    if size is None:
        w = h = 1.0
    else:
        w, h = float(size[0]), float(size[1])

    def d(a: int, b: int) -> float:
        p1, p2 = pts[a], pts[b]
        dx = (p1[0] - p2[0]) * w
        dy = (p1[1] - p2[1]) * h
        return (dx * dx + dy * dy) ** 0.5

    left = (d(160, 144) + d(158, 153)) / (2 * d(33, 133) + 1e-9)
    right = (d(385, 380) + d(387, 373)) / (2 * d(362, 263) + 1e-9)
    return float((left + right) / 2)


def detect_face_ear(rgb: np.ndarray, ear_threshold: float = EAR_CLOSED_THRESHOLD) -> dict:
    """对 RGB 图像做人脸与闭眼检测。

    返回 dict：
        is_face   是否检测到人脸
        num_faces 检测到的人脸数
        ear       最小 EAR（闭眼以最小为准）；无脸时为 None
        eyes_closed 是否闭眼（ear 不为 None 且 ear < 阈值）
        error     错误信息（mediapipe 不可用时）
    """
    result = {"is_face": False, "num_faces": 0, "ear": None, "eyes_closed": False, "error": None}
    if not _ensure_mediapipe():
        result["error"] = "mediapipe 不可用"
        return result
    if rgb is None or rgb.size == 0:
        return result
    try:
        landmarks = _detect_landmarks(rgb)
    except Exception as e:
        result["error"] = f"FaceMesh 运行失败: {e}"
        return result
    if not landmarks:
        return result
    result["is_face"] = True
    result["num_faces"] = len(landmarks)
    # 【v0.6 S3-1】按像素口径算 EAR，避免非方图画幅比污染比值
    height, width = rgb.shape[:2]
    ears = [_ear(pts, (width, height)) for pts in landmarks]
    result["ear"] = min(ears) if ears else None
    result["eyes_closed"] = (result["ear"] is not None and result["ear"] < ear_threshold)
    return result


# ---------------------------------------------------------------------------
# 【模型升级 v2.2】闭眼检测：MediaPipe EAR + 数据集训练 ViT 分类器融合
# （模型名/置信度/触发区间等参数已统一在 engine/config.py，见文件顶部）
# ---------------------------------------------------------------------------
_eye_model = None
_eye_proc = None
_eye_dev = None
# 【v0.6 S3-6 / P1-12】分类器失败只需提示一次（旧实现每张照片刷一条 warning）
# 【v0.6 S3-补 / 0B】"一次"的粒度是**每次分析运行**，而不是整个进程 —— 见 reset_run_state()。
_eye_failure_reported = False


def reset_run_state() -> None:
    """重置「本轮已提示过降级」的一次性标志；由分析流水线在每轮开始时调用。

    【v0.6 S3-补 / 0B】旧实现只在分类器**加载成功**时把标志清回 False
    （见 _get_eye_classifier 末尾）。如果模型长期不可用 —— 离线缺权重、缓存残缺 ——
    加载会一直失败，那条清零语句永远不会执行，标志永远停在 True：
    第二轮起既不写 warning，也不再上报降级事件，**静默降级**重新出现，
    恰好是 v0.6 要消除的那个行为。

    因此把「一轮一次」的语义交给明确的轮次边界，而不是依赖「下次加载成功」这个
    永远不会到来的事件。同 _get_eye_classifier 里的清零一起，两条路径都保证
    一次失败只会被提示一次、且每一轮都会重新提示。
    """
    global _eye_failure_reported
    _eye_failure_reported = False


def _get_eye_classifier():
    """懒加载闭眼 ViT 分类器（GPU 优先）。返回 (model, processor, device)；不可用抛异常。

    【修复 v0.4】加载前先做 HF 缓存自愈 + 缓存环境变量注入。
    旧实现直接用模型名 from_pretrained，一旦本地缓存残缺（.no_exist 过期标记、
    不完整快照）就抛异常，而异常被上层吞掉后仅写一行 warning——用户毫无感知，
    实际却退化成了"只有 EAR"的单信号判定。
    """
    global _eye_model, _eye_proc, _eye_dev, _eye_failure_reported
    if _eye_model is not None:
        return _eye_model, _eye_proc, _eye_dev
    from . import models_guard

    models_guard.apply_env()
    models_guard.repair_hf_cache()
    import torch
    from transformers import AutoImageProcessor, AutoModelForImageClassification

    kwargs = {}
    # 【v0.6 S2-2 / G3】本地快照完整 ⇒ 直接以该目录为加载源（理由同 aesthetics.get_clip）
    local = models_guard.local_model_source(EYE_MODEL_NAME)
    source = local or EYE_MODEL_NAME
    if local or models_guard.offline_local_files_only():
        kwargs["local_files_only"] = True
    _eye_proc = AutoImageProcessor.from_pretrained(source, **kwargs)
    _eye_model = AutoModelForImageClassification.from_pretrained(source, **kwargs)
    _eye_dev = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    try:
        _eye_model.to(_eye_dev).eval()
    except Exception as error:  # noqa: BLE001 —— CUDA OOM / 驱动异常
        # 【v0.6 S3-6 / P1-11】旧实现让 .to(cuda) 的异常直接冒出，分类器永久不可用；
        # 现在回退 CPU 并作为降级事件上报，用户可在分析汇总里看到。
        _log.warning("闭眼分类器迁移到 %s 失败，回退 CPU：%s", _eye_dev, error)
        _eye_dev = torch.device("cpu")
        _eye_model.to(_eye_dev).eval()
        from . import degradations

        degradations.record(
            "闭眼分类器",
            f"{type(error).__name__}: {error}",
            "闭眼 ViT 改用 CPU 推理；结果一致但更慢",
            severity="info",
        )
    _eye_failure_reported = False      # 加载成功 ⇒ 允许下次故障重新提示一次
    return _eye_model, _eye_proc, _eye_dev


def _crop_eye(pil_img, landmarks, pts_idx, pad: float = EYE_CROP_CONTEXT):
    """按眼睛关键点裁剪含上下文的方形区域，返回 PIL 图（已缩放到 224）。"""
    w, h = pil_img.size
    xs = [landmarks[i][0] * w for i in pts_idx]
    ys = [landmarks[i][1] * h for i in pts_idx]
    cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
    r = max(max(xs) - min(xs), max(ys) - min(ys)) * pad / 2
    r = max(r, 12)
    crop = pil_img.crop((int(cx - r), int(cy - r), int(cx + r), int(cy + r)))
    return crop.resize((224, 224))


def eye_close_probability(pil_img, landmarks) -> float:
    """对双眼区域裁剪分别分类，返回最大闭眼概率（0-1）。

    pil_img: PIL RGB 原图；landmarks: 单脸的 478 个 (x,y) 归一化关键点。
    模型不可用/异常返回 None（调用方按仅 EAR 处理）。
    """
    global _eye_failure_reported
    try:
        import torch
        model, proc, dev = _get_eye_classifier()
        probs = []
        for idxs in (LEFT_EYE, RIGHT_EYE):
            crop = _crop_eye(pil_img, landmarks, idxs)
            inp = proc(images=crop, return_tensors="pt")
            inp = {k: v.to(dev) for k, v in inp.items()}
            with torch.no_grad():
                out = model(**inp)
            p = torch.softmax(out.logits, dim=1)[0]
            probs.append(float(p[0].item()))   # id2label[0] = 'closeEye'
        return max(probs) if probs else None
    except Exception as e:
        # 【v0.6 S3-6 / P1-12】旧实现每张照片都刷一条 warning（数千行日志把日志文件
        # 灌满，真正的问题反而被淹没）。改为每次故障只报**一次**：一条 warning +
        # 一条降级事件，让用户知道本次闭眼判定退化成了「只用 EAR 几何信号」。
        if not _eye_failure_reported:
            _eye_failure_reported = True
            _log.warning("闭眼分类器运行失败（本次分析后续按仅 EAR）：%s", e)
            from . import degradations

            degradations.record(
                "闭眼分类器",
                f"{type(e).__name__}: {e}",
                "本次闭眼判定只用 EAR 几何信号；分类器恢复后重跑会自动补齐",
            )
        return None


def detect_face_and_eyes(rgb: np.ndarray, pil_img=None,
                         ear_threshold: float = EAR_CLOSED_THRESHOLD,
                         model_conf: float = EYE_MODEL_CONF) -> dict:
    """人脸 + 融合闭眼判定（EAR 与数据集训练分类器互补）。

    - EAR 对“明显闭眼”（睁眼幅度极小）可靠；分类器对“EAR 边界漏判”更敏感，
      二者取“或”可互补。实测：睁眼样本两者均无误报。
    - 性能：仅当 EAR 处于 [EYE_MODEL_EAR_LO, EYE_MODEL_EAR_HI] 边界区间才跑 ViT 分类器，
      明显睁/闭眼直接由 EAR 判定（含人脸大批量更快）。

    【修复 v0.4】两处缺陷：
      1) 分类器分支里新建 FaceMesh 且不 close —— 每次都重新加载 .binarypb 模型，
         原生资源持续泄漏。现改为复用 _detect_landmarks() 的单例结果，零额外推理。
      2) 多人脸口径不一致 —— 综合 EAR 取【所有人脸的最小值】（最闭眼的那张脸），
         但分类器却固定看 multi_face_landmarks[0]（第一张脸），两者可能不是同一张脸。
         现改为对【EAR 最小的那张脸】跑分类器，与判定口径对齐。

    返回 dict：
        is_face, num_faces, ear,
        eye_close_prob 分类器最大闭眼概率（无脸/未触发/不可用为 None）
        eyes_closed    EAR<阈值 或 分类器>阈值
        error
    """
    result = {"is_face": False, "num_faces": 0, "ear": None,
              "eye_close_prob": None, "eyes_closed": False, "error": None,
              "regions": []}
    if not _ensure_mediapipe():
        result["error"] = "mediapipe 不可用"
        return result
    try:
        landmarks = _detect_landmarks(rgb)
    except Exception as e:
        result["error"] = f"FaceMesh 运行失败: {e}"
        return result
    if not landmarks:
        return result

    result["is_face"] = True
    result["num_faces"] = len(landmarks)
    # 【v0.6 S3-1】同 detect_face_ear：EAR 必须用像素口径
    height, width = rgb.shape[:2]
    ears = [_ear(pts, (width, height)) for pts in landmarks]
    regions = [_face_region(rgb, index, points, ears[index])
               for index, points in enumerate(landmarks)]
    result["regions"] = regions
    result["ear"] = min(ears) if ears else None
    result["eyes_closed"] = (result["ear"] is not None and result["ear"] < ear_threshold)
    if result["eyes_closed"]:
        return result                       # EAR 已判闭眼 → 无需分类器

    ear = result["ear"]
    if ear is None or not (EYE_MODEL_EAR_LO <= ear <= EYE_MODEL_EAR_HI):
        return result                       # 明显睁眼 → 跳过分类器（省时）
    if pil_img is None:
        return result
    # 对"EAR 最小的那张脸"跑分类器，与上面的综合判定口径一致
    try:
        worst = landmarks[int(np.argmin(ears))] if ears else landmarks[0]
        close_prob = eye_close_probability(pil_img, worst)
    except Exception as e:
        _log.warning("闭眼分类器运行失败（按仅 EAR）：%s", e)
        close_prob = None
    result["eye_close_prob"] = close_prob
    if ears:
        regions[int(np.argmin(ears))]["eye_close_prob"] = close_prob
    if close_prob is not None and close_prob > model_conf:
        result["eyes_closed"] = True
    return result


def _face_region(rgb: np.ndarray, face_index: int, landmarks, ear: float) -> dict:
    xs = [min(1.0, max(0.0, float(point[0]))) for point in landmarks]
    ys = [min(1.0, max(0.0, float(point[1]))) for point in landmarks]
    left, right = min(xs), max(xs)
    top, bottom = min(ys), max(ys)
    pad_x = (right - left) * 0.08
    pad_y = (bottom - top) * 0.08
    left, right = max(0.0, left - pad_x), min(1.0, right + pad_x)
    top, bottom = max(0.0, top - pad_y), min(1.0, bottom + pad_y)
    height, width = rgb.shape[:2]
    x1, x2 = int(left * width), max(int(np.ceil(right * width)), int(left * width) + 1)
    y1, y2 = int(top * height), max(int(np.ceil(bottom * height)), int(top * height) + 1)
    crop = rgb[max(0, y1):min(height, y2), max(0, x1):min(width, x2)]
    sharpness = (
        evaluate_technical_quality(np.ascontiguousarray(crop)).sharpness
        if crop.size
        else 0.0
    )
    return {
        "face_index": face_index,
        "x": left,
        "y": top,
        "width": right - left,
        "height": bottom - top,
        "ear": ear,
        "eye_close_prob": None,
        "sharpness": sharpness,
    }


def eye_model_name() -> str:
    """当前生效的闭眼模型名（供分析汇总展示/版本失效）。"""
    return "vit+dima806+ear"


# ---------------------------------------------------------------------------
# 命令行调试入口
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    from .loader import load_image_rgb_array

    for p in sys.argv[1:]:
        img = load_image_rgb_array(p, max_size=512)
        r = detect_face_ear(img)
        print(f"{os.path.basename(p)}: is_face={r['is_face']} num_faces={r['num_faces']} "
              f"ear={r['ear'] if r['ear'] is not None else 'N/A'} eyes_closed={r['eyes_closed']} "
              f"error={r['error']}")
