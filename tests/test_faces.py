# -*- coding: utf-8 -*-
"""faces.py 单测：EAR + 分类器融合规则、边界区间触发逻辑。

【v0.4 重构适配】detect_face_and_eyes 不再调用 detect_face_ear，而是直接复用
_detect_landmarks() 单例结果并自行计算 EAR。因此单测改在更底层注入受控的
关键点 / EAR，并打桩 _ensure_mediapipe 以脱离本机 mediapipe 可用性——聚焦验证
"融合判定逻辑"本身；无脸用例额外验证本机 FaceMesh 能实际运行。
"""
from __future__ import annotations

import builtins
import os
import sys

import numpy as np
import pytest
from PIL import Image

from engine import faces


def test_mediapipe_junction_path_is_environment_specific():
    standard = faces._junction_path(r"D:\repo\.venv\Lib\site-packages")
    lightweight = faces._junction_path(r"D:\repo\.venv-light\Lib\site-packages")

    assert standard != lightweight
    assert standard.isascii()
    assert lightweight.isascii()


def test_frozen_face_mesh_resource_root_uses_ascii_junction(monkeypatch):
    from mediapipe.python import solution_base

    link = r"D:\photocull_mp_test"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(solution_base, "__file__", r"D:\中文\_internal\mediapipe\python\solution_base.py")
    expected_model = os.path.join(
        link, "mediapipe", "modules", "face_landmark", "face_landmark_front_cpu.binarypb",
    )
    monkeypatch.setattr(faces.os.path, "isfile", lambda path: path == expected_model)

    faces._redirect_frozen_resource_root(link)

    assert solution_base.__file__ == os.path.join(link, "mediapipe", "python", "solution_base.py")


def test_failed_mediapipe_import_is_reported_once_and_not_retried(monkeypatch, caplog):
    monkeypatch.setattr(faces, "_MEDIAPIPE_READY", False)
    monkeypatch.setattr(faces, "_MEDIAPIPE_FAILED", False, raising=False)
    monkeypatch.setattr(faces, "_find_mediapipe_site_packages", lambda: [])
    original_import = builtins.__import__
    attempts = []

    def fail_mediapipe(name, *args, **kwargs):
        if name == "mediapipe":
            attempts.append(name)
            raise ImportError("missing bundled dependency")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_mediapipe)

    assert faces._ensure_mediapipe() is False
    assert faces._ensure_mediapipe() is False
    assert attempts == ["mediapipe"]
    assert "missing bundled dependency" in caplog.text


def _rgb():
    return np.zeros((32, 32, 3), dtype=np.uint8)


def _pil():
    return Image.new("RGB", (32, 32), (100, 100, 100))


def _patch(monkeypatch, ear_value):
    """注入受控的融合管线输入：强制 mediapipe 可用、返回单张脸、EAR 可控。"""
    monkeypatch.setattr(faces, "_ensure_mediapipe", lambda: True)
    # 单张脸（478 个伪关键点）；_ear 已被打桩，坐标无所谓
    monkeypatch.setattr(faces, "_detect_landmarks",
                        lambda rgb: [[(0.5, 0.3) for _ in range(478)]])
    monkeypatch.setattr(faces, "_ear", lambda pts, size=None: ear_value)


def test_ear_open_above_hi_skips_classifier(monkeypatch):
    """明显睁眼（EAR 高于上界）时不应调用分类器。"""
    calls = {"n": 0}
    hi = faces.EYE_MODEL_EAR_HI
    _patch(monkeypatch, hi + 0.1)
    monkeypatch.setattr(faces, "eye_close_probability",
                        lambda *a, **k: calls.__setitem__("n", calls["n"] + 1))
    r = faces.detect_face_and_eyes(_rgb())
    assert calls["n"] == 0
    assert r["eye_close_prob"] is None
    assert r["eyes_closed"] is False


def test_ear_closed_below_lo_skips_classifier(monkeypatch):
    """明显闭眼（EAR 低于阈值）直接判定闭眼，不跑分类器。"""
    calls = {"n": 0}
    thr = faces.EAR_CLOSED_THRESHOLD
    _patch(monkeypatch, thr - 0.05)
    monkeypatch.setattr(faces, "eye_close_probability",
                        lambda *a, **k: calls.__setitem__("n", calls["n"] + 1))
    r = faces.detect_face_and_eyes(_rgb())
    assert calls["n"] == 0
    assert r["eyes_closed"] is True


def test_boundary_interval_triggers_classifier(monkeypatch):
    """EAR 落在边界区间才触发分类器，且分类器高置信度判闭眼。"""
    calls = {"n": 0}
    lo, hi, thr = faces.EYE_MODEL_EAR_LO, faces.EYE_MODEL_EAR_HI, faces.EAR_CLOSED_THRESHOLD
    _patch(monkeypatch, (max(lo, thr) + hi) / 2.0)   # 保证在 [max(lo,thr), hi] 内
    monkeypatch.setattr(faces, "eye_close_probability",
                        lambda *a, **k: calls.__setitem__("n", calls["n"] + 1) or 0.9)
    r = faces.detect_face_and_eyes(_rgb(), pil_img=_pil())
    assert calls["n"] == 1
    assert r["eye_close_prob"] == 0.9
    assert r["eyes_closed"] is True


def test_boundary_interval_classifier_low_conf(monkeypatch):
    """边界区间分类器低置信度 → 不误判闭眼。"""
    lo, hi, thr = faces.EYE_MODEL_EAR_LO, faces.EYE_MODEL_EAR_HI, faces.EAR_CLOSED_THRESHOLD
    _patch(monkeypatch, (max(lo, thr) + hi) / 2.0)
    monkeypatch.setattr(faces, "eye_close_probability", lambda *a, **k: 0.2)
    r = faces.detect_face_and_eyes(_rgb(), pil_img=_pil())
    assert r["eyes_closed"] is False


def test_no_face_neutral():
    """无脸时不判闭眼，eye_close_prob 为 None。"""
    r = faces.detect_face_and_eyes(_rgb())
    assert r["error"] is None
    assert r["is_face"] is False
    assert r["eyes_closed"] is False
    assert r["eye_close_prob"] is None


def test_all_face_regions_include_bbox_eye_state_and_local_sharpness(monkeypatch):
    monkeypatch.setattr(faces, "_ensure_mediapipe", lambda: True)
    first = [(0.10, 0.20), (0.30, 0.50)] + [(0.20, 0.35)] * 476
    second = [(0.60, 0.10), (0.85, 0.45)] + [(0.72, 0.25)] * 476
    monkeypatch.setattr(faces, "_detect_landmarks", lambda _rgb: [first, second])
    monkeypatch.setattr(faces, "_ear", lambda points, size=None: 0.25 if points is first else 0.28)
    calls = []
    monkeypatch.setattr(
        faces,
        "eye_close_probability",
        lambda _image, points: calls.append(points) or 0.8,
    )
    checker = (np.indices((64, 64)).sum(axis=0) % 2 * 255).astype(np.uint8)
    rgb = np.repeat(checker[:, :, None], 3, axis=2)

    result = faces.detect_face_and_eyes(rgb, pil_img=Image.fromarray(rgb))

    assert calls == [first]
    assert len(result["regions"]) == 2
    for index, region in enumerate(result["regions"]):
        assert region["face_index"] == index
        assert 0.0 <= region["x"] <= 1.0
        assert 0.0 <= region["y"] <= 1.0
        assert 0.0 < region["width"] <= 1.0
        assert 0.0 < region["height"] <= 1.0
        assert 0.0 <= region["sharpness"] <= 100.0
        assert "ear" in region and "eye_close_prob" in region
    assert result["regions"][0]["eye_close_prob"] == 0.8
    assert result["regions"][1]["eye_close_prob"] is None


# ---------------------------------------------------------------------------
# v0.6 S3-1 / P1-3：EAR 必须用「像素口径」，画幅比不得污染比值
# ---------------------------------------------------------------------------
# 一组与画幅尺寸无关的固定「像素眼型」
_EYE_PX = {
    33: (80.0, 100.0), 133: (120.0, 100.0),     # 左眼水平距 40
    160: (100.0, 90.0), 144: (100.0, 110.0),    # 垂直距 20
    158: (95.0, 92.0), 153: (95.0, 108.0),      # 垂直距 16
    362: (180.0, 100.0), 263: (220.0, 100.0),   # 右眼水平距 40
    385: (200.0, 90.0), 380: (200.0, 110.0),    # 垂直距 20
    387: (205.0, 92.0), 373: (205.0, 108.0),    # 垂直距 16
}
_EXPECTED_EAR = (20.0 + 16.0) / (2 * 40.0)      # 0.45


def _landmarks_for(size):
    """把固定像素眼型摊到 size=(w,h) 图上，返回归一化关键点（模拟 MediaPipe 输出）。"""
    w, h = size
    points = [(0.5, 0.5)] * 478
    for idx, (x, y) in _EYE_PX.items():
        points[idx] = (x / w, y / h)
    return points


def test_ear_is_aspect_ratio_invariant_in_pixel_space():
    """3:2、2:3、1:1 下同一个眼型必须给出同一个 EAR。"""
    values = [faces._ear(_landmarks_for(size), size)
              for size in ((300, 200), (200, 300), (1000, 1000))]
    assert values == pytest.approx([_EXPECTED_EAR] * 3, abs=1e-9)


def test_ear_normalized_fallback_distorts_non_square():
    """反向锁定缺陷本身：不传 size（归一化口径）在非方图上必然失真。"""
    wide = faces._ear(_landmarks_for((300, 200)), None)
    tall = faces._ear(_landmarks_for((200, 300)), None)
    assert wide != pytest.approx(tall, abs=1e-3)


def test_detect_face_ear_passes_pixel_size(monkeypatch):
    """接线检查：detect_face_ear 必须把 (w, h) 传进 _ear。"""
    size = (300, 200)
    monkeypatch.setattr(faces, "_ensure_mediapipe", lambda: True)
    monkeypatch.setattr(faces, "_detect_landmarks", lambda rgb: [_landmarks_for(size)])
    rgb = np.zeros((size[1], size[0], 3), dtype=np.uint8)
    result = faces.detect_face_ear(rgb)
    assert result["ear"] == pytest.approx(_EXPECTED_EAR, abs=1e-9)


# ---------------------------------------------------------------------------
# 【v0.6 S3-6 / P1-12】闭眼分类器运行失败：每轮只报一次，且作为降级事件上报
#
# 旧实现把 warning 写在每张照片的调用路径上：一次 5000 张的分析会刷 5000 条同样的
# 日志，把日志文件灌满、真正的问题被淹没；用户也不知道"闭眼判定"已经退化成了
# 只用 EAR 几何信号。现在改为：一条 warning + 一条降级事件，然后静默。
# ---------------------------------------------------------------------------
def test_eye_close_probability_reports_failure_only_once(monkeypatch, caplog):
    from engine import degradations

    degradations.drain()
    monkeypatch.setattr(faces, "_eye_failure_reported", False)

    def boom():
        raise RuntimeError("CUDA out of memory")

    monkeypatch.setattr(faces, "_get_eye_classifier", boom)

    landmarks = [(0.5, 0.5)] * 478
    with caplog.at_level("WARNING"):
        results = [faces.eye_close_probability(_pil(), landmarks) for _ in range(5)]

    assert results == [None] * 5, "分类器不可用时调用方按『仅 EAR』处理"
    assert caplog.text.count("闭眼分类器运行失败") == 1, "5 张照片只允许一条 warning"

    events = degradations.drain()
    assert len(events) == 1, "只允许一条降级事件"
    assert events[0]["stage"] == "闭眼分类器"
    assert "RuntimeError" in events[0]["reason"]
    assert "EAR" in events[0]["effect"]


def test_eye_classifier_load_success_rearms_the_one_time_report(monkeypatch):
    """分类器恢复可用后，下一次故障应重新提示一次（不能永远静音）。"""
    from engine import degradations, models_guard

    degradations.drain()
    monkeypatch.setattr(faces, "_eye_failure_reported", True)
    # 隔离分类器单例，用例结束后由 monkeypatch 归还（避免污染后续用例）
    for name in ("_eye_model", "_eye_proc", "_eye_dev"):
        monkeypatch.setattr(faces, name, None, raising=False)
    # 缓存自愈/环境注入属副作用，这里只关心"加载成功后的状态位"
    monkeypatch.setattr(models_guard, "apply_env", lambda: None)
    monkeypatch.setattr(models_guard, "repair_hf_cache", lambda: None)
    monkeypatch.setattr(models_guard, "local_model_source", lambda _name: None)
    monkeypatch.setattr(models_guard, "offline_local_files_only", lambda: False)

    class _FakeModel:
        def to(self, device):
            self.device = device
            return self

        def eval(self):
            return self

    import transformers

    monkeypatch.setattr(transformers.AutoImageProcessor, "from_pretrained",
                        lambda *a, **k: object())
    monkeypatch.setattr(transformers.AutoModelForImageClassification, "from_pretrained",
                        lambda *a, **k: _FakeModel())

    model, processor, device = faces._get_eye_classifier()

    assert isinstance(model, _FakeModel)
    assert processor is not None and device is not None
    assert faces._eye_failure_reported is False, "加载成功 ⇒ 允许下次故障重新提示"
    assert degradations.drain() == [], "加载成功不该产生降级事件"
