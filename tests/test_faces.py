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
    monkeypatch.setattr(faces.config, "INFERENCE_BACKEND", "torch")
    monkeypatch.setattr(faces, "_ensure_mediapipe", lambda: True)
    # 单张脸（478 个伪关键点）；_ear 已被打桩，坐标无所谓
    monkeypatch.setattr(faces, "_detect_landmarks",
                        lambda rgb: [[(0.5, 0.3) for _ in range(478)]])
    monkeypatch.setattr(faces, "_ear", lambda pts: ear_value)


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


def test_heuristic_backend_skips_vit_classifier(monkeypatch):
    _patch(monkeypatch, 0.25)
    monkeypatch.setattr(faces.config, "INFERENCE_BACKEND", "heuristic")
    calls = []
    monkeypatch.setattr(
        faces, "eye_close_probability", lambda *_args: calls.append(True) or 0.9,
    )

    result = faces.detect_face_and_eyes(_rgb(), pil_img=_pil())

    assert calls == []
    assert result["eye_close_prob"] is None
    assert result["eyes_closed"] is False


def test_no_face_neutral():
    """无脸时不判闭眼，eye_close_prob 为 None。"""
    r = faces.detect_face_and_eyes(_rgb())
    assert r["error"] is None
    assert r["is_face"] is False
    assert r["eyes_closed"] is False
    assert r["eye_close_prob"] is None


def test_all_face_regions_include_bbox_eye_state_and_local_sharpness(monkeypatch):
    monkeypatch.setattr(faces.config, "INFERENCE_BACKEND", "torch")
    monkeypatch.setattr(faces, "_ensure_mediapipe", lambda: True)
    first = [(0.10, 0.20), (0.30, 0.50)] + [(0.20, 0.35)] * 476
    second = [(0.60, 0.10), (0.85, 0.45)] + [(0.72, 0.25)] * 476
    monkeypatch.setattr(faces, "_detect_landmarks", lambda _rgb: [first, second])
    monkeypatch.setattr(faces, "_ear", lambda points: 0.25 if points is first else 0.28)
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
