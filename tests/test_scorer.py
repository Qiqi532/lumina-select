# -*- coding: utf-8 -*-
"""scorer.py 单测：三场景权重、废片判定、不确定甄选三条规则。"""
from __future__ import annotations

from engine import scorer


def test_unreliable_blur_is_neutral_and_cannot_mark_waste():
    metrics = {"blur_score": 0.0, "blur_reliable": False,
               "quality": 0.0, "over": 0.0, "under": 0.0, "aesthetic": 50.0}
    score = scorer.analyze_photo_score(metrics)
    assert score["blur_n"] == 0.5
    assert "模糊" not in scorer.waste_reasons(
        "其他", 0.0, 0.0, 0.0, False, False, blur_reliable=False)
    assert "模糊" in scorer.waste_reasons(
        "其他", 0.0, 0.0, 0.0, False, False, blur_reliable=True)
from engine.config import SCENE_PRESETS


def _s(comp, blur_n=0.8, aes_n=0.8, face_n=0.8, hard_waste=False, path="p"):
    return {"comp_score": comp, "blur_n": blur_n, "aes_n": aes_n, "face_n": face_n,
            "hard_waste": hard_waste, "path": path}


def test_presets_weights():
    """三套场景预设权重存在且和为 1（人脸维度允许为 0）。"""
    for name, p in SCENE_PRESETS.items():
        w = p["w"]
        assert len(w) == 4
        assert abs(sum(w) - 1.0) < 1e-6
        assert p["label"]


def test_composite_weights():
    """composite 按场景权重加权。人像含人脸维度权重，风光人脸权重为 0。"""
    base = dict(blur_n=0.5, expo_n=0.5, aes_n=0.5, face_n=1.0)
    comp_portrait = scorer.composite(**base, scene="人像")
    comp_landscape = scorer.composite(**base, scene="风景")
    assert comp_portrait > comp_landscape  # 人像给 face 权重，风光不给


def test_composite_custom_weights():
    c = scorer.composite(0.5, 0.5, 0.5, 0.5, scene="其他", weights=(1, 0, 0, 0))
    assert abs(c - 50.0) < 1e-6


def test_waste_reasons_blur():
    assert scorer.waste_reasons("其他", 20.0, 0.0, 0.0, False, False) == ["模糊"]
    assert scorer.waste_reasons("其他", 120.0, 0.0, 0.0, False, False) == []


def test_waste_reasons_expo():
    assert "过曝/欠曝" in scorer.waste_reasons("其他", 200.0, 0.9, 0.0, False, False)
    assert "过曝/欠曝" in scorer.waste_reasons("其他", 200.0, 0.0, 0.9, False, False)


def test_waste_reasons_eyes_decoupled_from_scene():
    """闭眼判定已与场景分类解耦：只要确实检测到人脸(is_face)即判闭眼，
    不再依赖场景是否被分类为人像（避免场景分类器走神导致闭眼检测整体失效）。"""
    # 检测到人脸 → 人像场景下闭眼判定生效
    assert "闭眼" in scorer.waste_reasons("人像", 200.0, 0.0, 0.0, True, False, is_face=True)
    # 检测到人脸 → 非人像场景（如被误分类为"其他"）同样生效
    assert "闭眼" in scorer.waste_reasons("其他", 200.0, 0.0, 0.0, True, False, is_face=True)
    # 未检测到人脸 → 即便场景被标为人像也不判闭眼
    assert "闭眼" not in scorer.waste_reasons("人像", 200.0, 0.0, 0.0, True, False, is_face=False)


def test_waste_reasons_dup():
    assert "高度重复" in scorer.waste_reasons("其他", 200.0, 0.0, 0.0, False, True)


def test_judge_best_when_gap_large():
    """分差大且无冲突 → best，picks=[top1]。"""
    s = [_s(90), _s(80), _s(70)]
    verdict, picks = scorer.judge_and_pick(s)
    assert verdict == "best"
    assert len(picks) == 1


def test_judge_uncertain_rule1_gap_small():
    """规则1：Top1-Top2 分差 < 阈值 → uncertain，全部非废候选进入甄选。"""
    s = [_s(90), _s(89), _s(50)]
    verdict, picks = scorer.judge_and_pick(s, score_gap=5.0)
    assert verdict == "uncertain"
    assert len(picks) == 3


def test_judge_uncertain_rule2_pareto():
    """规则2：维度互有胜负 → uncertain。"""
    s = [_s(85, blur_n=0.9, aes_n=0.6, face_n=0.6),
         _s(85, blur_n=0.6, aes_n=0.9, face_n=0.6)]
    verdict, picks = scorer.judge_and_pick(s)
    assert verdict == "uncertain"


def test_judge_uncertain_rule3_low_conf():
    """规则3：场景置信度低 → uncertain。"""
    s = [_s(90), _s(80)]
    verdict, _ = scorer.judge_and_pick(s, conf=0.3, scene_conf_low=0.6)
    assert verdict == "uncertain"


def test_judge_hard_waste_removed():
    """硬废片从候选池剔除；若全部为废片则无推荐。"""
    s = [_s(90, hard_waste=True), _s(80, hard_waste=True)]
    verdict, picks = scorer.judge_and_pick(s)
    assert verdict == "best"
    assert picks == []


def test_pick_stars_best():
    st = scorer.pick_stars("best", [_s(90, path="a"), _s(80, path="b")])
    assert st == {"a": 5}


def test_pick_stars_uncertain():
    st = scorer.pick_stars("uncertain", [_s(90, path="a"), _s(80, path="b")])
    assert st == {"a": 4}


def test_norm_blur_saturation():
    assert scorer.norm_blur(0) == 0.0
    assert scorer.norm_blur(200) == 1.0
    assert scorer.norm_blur(500) == 1.0   # 封顶
