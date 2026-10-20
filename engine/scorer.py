# -*- coding: utf-8 -*-
"""
scorer.py —— 场景自适应综合评分、废片判定、最佳帧推荐与不确定候选甄选

算法（设计文档 4.4.8 / 4.4.9）：
    1. 四类场景预设（人像/风景/街拍/其他），权重 = 清晰/曝光/美学/人脸：
         人像 (0.25, 0.20, 0.25, 0.30)，启用闭眼硬判
         风景 (0.35, 0.30, 0.35, 0.00)，不启用闭眼
         街拍/其他 (0.30, 0.20, 0.30, 0.20)，检测到人脸才启用 face 维度
    2. 综合分 = 100 * Σ(权重 * 归一化子分)，各子分归一化到 [0,1]。
    3. 废片硬性规则：严重模糊(blur<60)、严重过曝/欠曝(over|under>0.5)、
       高度重复、人像闭眼(EAR<0.20)。
    4. 最佳帧 / 不确定甄选：
         规则1：Top1-Top2 综合分差 < 3 → 无明确胜者 → uncertain
         规则2：多维度互有胜负（帕累托冲突）→ uncertain
         规则3：场景置信度 < 0.6 → uncertain
         明确时推荐 Top1（自动打星 5），次高分 Top2 打星 4；
         uncertain 时甄选 2~5 张候选进入待甄选模式。

独立命令行调试：python -m engine.scorer
"""
from __future__ import annotations

from . import config

# ---------------------------------------------------------------------------
# 场景预设（设计文档表 7；权重统一来自 engine/config.py）
# ---------------------------------------------------------------------------
PRESETS = config.SCENE_PRESETS

# 判定阈值统一来自 engine/config.py（与 quality / faces 同源，口径一致）
BLUR_WASTE = config.BLUR_WASTE          # 拉普拉斯方差 < 60 严重模糊
EXPO_WASTE = config.EXPO_WASTE          # 过曝/欠曝占比 > 0.5 严重
EAR_WASTE = config.EAR_WASTE            # 闭眼阈值（EAR）
BRISQUE_WASTE = config.BRISQUE_WASTE    # （遗留）BRISQUE 严重失真参考阈值
BRISQUE_SATURATED = config.BRISQUE_SATURATED  # （遗留）BRISQUE 饱和保护
# 【模型升级 v0.4】画质分来自可插拔模型（默认 musiq），统一为 0-100【越高越好】
EAR_CLOSED_REF = config.EAR_CLOSED_REF  # EAR ≤ 此值 → face_n = 0
EAR_OPEN_REF = config.EAR_OPEN_REF      # EAR ≥ 此值 → face_n = 1
# 【修复 v0.4】闭眼硬判不再依赖场景分类结果（见 waste_reasons 注释）
EYE_WASTE_REQUIRE_PORTRAIT = config.EYE_WASTE_REQUIRE_PORTRAIT_SCENE
SCORE_GAP = config.SCORE_GAP            # Top1-Top2 综合分差阈值（<3 视为无明确胜者）
SCENE_CONF_LOW = config.SCENE_CONF_LOW  # 场景置信度阈值
DIM_GAP = config.DIM_GAP                # 维度冲突判定的差距阈值
MAX_CANDIDATES = config.MAX_CANDIDATES  # 甄选候选最多 5 张
FACE_NEUTRAL = config.FACE_NEUTRAL      # 无脸时 face 维度中性值

DIM_KEYS = ["blur_n", "aes_n", "face_n"]   # 维度冲突比较的子维度


# ---------------------------------------------------------------------------
# 归一化
# ---------------------------------------------------------------------------
def _clip(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def norm_blur(blur_score: float) -> float:
    """清晰度归一化：拉普拉斯方差 200 视为满分，线性映射到 [0,1]。"""
    return _clip(blur_score / 200.0)


def norm_quality(quality_score) -> float:
    """画质分归一化：0-100【越高越好】→ [0, 1]。

    【v0.4 变更】旧版入参是 BRISQUE 失真分（越低越好），且需要一条
    "≥80 就当没看见"的补丁来规避它对扁平插画/截图的饱和误报。
    新版入参统一为 quality.iqa_score_* 的输出（0-100，越高越好，
    默认模型 musiq 为数据驱动模型，不存在该饱和问题），因此无需补丁。
    """
    if quality_score is None:
        return 0.5
    return _clip(float(quality_score) / 100.0)


def norm_expo(over: float, under: float) -> float:
    """曝光质量归一化：过曝/欠曝占比越小越接近 1。"""
    return 1.0 - _clip(over + under)


def norm_aes(aesthetic: float) -> float:
    """美学分归一化：0-100 映射到 [0,1]。"""
    return _clip(aesthetic / 100.0)


def norm_face(ear, is_face: bool) -> float:
    """人脸质量归一化：有脸时由 EAR 分段映射，无脸时取中性 0.5。

    【修复 v0.4】旧实现为 clip(EAR / 0.35)，实测存在两处缺陷：
      * 饱和：正常睁眼 EAR≈0.36 就打满 1.0，"睁得很开"与"勉强睁开"无法区分；
      * 区分度不足：明确闭眼 EAR≈0.118 仍得 0.34 分，只比睁眼少 0.66。
    改为 [EAR_CLOSED_REF, EAR_OPEN_REF] 线性区间 + 两端截断，
    使"闭眼→0、正常睁眼→1"，中间态平滑过渡。
    """
    if not is_face or ear is None:
        return FACE_NEUTRAL
    span = EAR_OPEN_REF - EAR_CLOSED_REF
    if span <= 0:                      # 配置异常时退回旧口径，避免除零
        return _clip(float(ear) / 0.35)
    return _clip((float(ear) - EAR_CLOSED_REF) / span)


# ---------------------------------------------------------------------------
# 综合评分
# ---------------------------------------------------------------------------
def composite(blur_n, expo_n, aes_n, face_n, scene="其他", weights=None) -> float:
    """按场景预设权重计算综合分（0-100）。weights 传入 (w0,w1,w2,w3) 时覆盖预设。"""
    w = weights or PRESETS.get(scene, PRESETS["其他"])["w"]
    return 100.0 * (w[0] * blur_n + w[1] * expo_n + w[2] * aes_n + w[3] * face_n)


def analyze_photo_score(metrics: dict, scene: str = "其他", weights=None) -> dict:
    """由单张指标 dict 计算归一化子分与综合分。

    metrics 字段：blur_score, over, under, aesthetic, ear, is_face[, quality|brisque]
    返回 dict：blur_n, expo_n, aes_n, face_n, comp_score（并保留外部传入字段）

    【模型升级 v0.4】清晰维度 = 0.5·拉普拉斯 + 0.5·画质模型分（默认 musiq）。
    拉普拉斯方差对"失焦/抖动"极敏感但会被平滑墙面误导，画质模型补上感知维度，
    二者等权融合；模型缺失时退化为纯拉普拉斯。
    兼容旧字段：DB 列名仍为 brisque，也接受新名 quality（优先）。
    """
    blur_reliable = bool(metrics.get("blur_reliable", True))
    blur_n = norm_blur(metrics.get("blur_score", 0.0)) if blur_reliable else 0.5
    q = metrics.get("quality")
    if q is None:
        q = metrics.get("brisque")
    if q is not None and blur_reliable:
        blur_n = 0.5 * blur_n + 0.5 * norm_quality(q)
    expo_n = norm_expo(metrics.get("over", 0.0), metrics.get("under", 0.0))
    aes_n = norm_aes(metrics.get("aesthetic", 0.0))
    face_n = norm_face(metrics.get("ear"), bool(metrics.get("is_face", False)))
    comp = composite(blur_n, expo_n, aes_n, face_n, scene, weights)
    result = {"blur_n": blur_n, "expo_n": expo_n, "aes_n": aes_n,
              "face_n": face_n, "comp_score": comp}
    # 保留外部传入的额外字段（如 path / aesthetic / 废片原因），便于调用方追踪
    for k, v in metrics.items():
        if k not in result:
            result[k] = v
    return result


# ---------------------------------------------------------------------------
# 废片判定
# ---------------------------------------------------------------------------
def waste_reasons(scene: str, blur_score: float, over: float, under: float,
                  eyes_closed: bool, dup: bool, brisque=None,
                  is_face: bool = False, blur_reliable: bool = True) -> list[str]:
    """返回废片原因列表；空列表表示非废片。

    eyes_closed: 融合判定结果（EAR<阈值 或 闭眼分类器>阈值）。
    is_face:    是否真的检测到人脸（由 MediaPipe 给出）。
    brisque:    仅供评分维度使用的画质模型分。不作为模糊废片的独立触发源——
                模型分对构图/语义敏感，直接拿来判废片会误伤；"模糊"仍以
                拉普拉斯方差为准，模型信号只参与综合分（见 analyze_photo_score）。
    dup:        真·近乎像素重复（pHash 距离 ≤ config.DUP_HAMMING_STRICT）。
                【修复 v0.4】旧实现把"组内排名落败者"也塞进这个参数，
                导致 20 张连拍有 18 张被打成废片——那只是没赢，不是重复。

    【修复 v0.4】闭眼硬判与场景分类解耦：
        旧实现 `if scene == "人像" and eyes_closed`，而 scene 来自 CLIP 零样本
        分类。实测 data/demo 中 face_closed1.jpg 被判为「其他」（置信度 0.410），
        意味着只要场景分类器一走神，闭眼检测——本工具最核心的卖点——就整体失效。
        改为：只要确实检测到人脸（is_face）就启用闭眼硬判。
    """
    reasons = []
    if blur_reliable and blur_score < BLUR_WASTE:
        reasons.append("模糊")
    if over > EXPO_WASTE or under > EXPO_WASTE:
        reasons.append("过曝/欠曝")
    if dup:
        reasons.append("高度重复")
    if eyes_closed:
        # 场景门控：默认关闭（只要检测到人脸即判闭眼），可由配置改回旧行为
        if is_face or (EYE_WASTE_REQUIRE_PORTRAIT and scene == "人像"):
            reasons.append("闭眼")
    return reasons


def is_waste(scene: str, blur_score: float, over: float, under: float,
             eyes_closed: bool, dup: bool, brisque=None, is_face: bool = False,
             blur_reliable: bool = True) -> bool:
    return len(waste_reasons(scene, blur_score, over, under, eyes_closed, dup,
                             brisque, is_face, blur_reliable)) > 0


def is_hard_waste(scene: str, blur_score: float, over: float, under: float,
                  eyes_closed: bool, brisque=None, is_face: bool = False,
                  blur_reliable: bool = True) -> bool:
    """硬性质量废片（不含'高度重复'，因为去重判定依赖组内 verdict）。"""
    return len(waste_reasons(scene, blur_score, over, under, eyes_closed, dup=False,
                             brisque=brisque, is_face=is_face,
                             blur_reliable=blur_reliable)) > 0


# ---------------------------------------------------------------------------
# 最佳帧推荐与不确定候选甄选（设计文档 4.4.9）
# ---------------------------------------------------------------------------
def _dim_conflict(a: dict, b: dict, gap: float = DIM_GAP) -> bool:
    """两个候选在清晰/美学/人脸等维度互有胜负且差距都小 → 帕累托冲突。"""
    return (all(abs(a[k] - b[k]) < gap for k in DIM_KEYS)
            and any(a[k] > b[k] for k in DIM_KEYS)
            and any(b[k] > a[k] for k in DIM_KEYS))


def judge_and_pick(scores: list[dict], scene: str = "其他",
                   conf: float = 1.0, score_gap: float | None = None,
                   scene_conf_low: float | None = None,
                   max_candidates: int | None = None) -> tuple[str, list[dict]]:
    """判断相似组是否有明确最佳帧；返回 (verdict, picks)。

    scores: 组内每张的 dict（含 comp_score, blur_n, aes_n, face_n, 以及
            外部关联字段如 path/star 等会被原样保留）
    score_gap: Top1-Top2 分差阈值（默认取 config.SCORE_GAP，可由界面滑块覆盖）
    scene_conf_low: 场景置信度阈值（默认取 config.SCENE_CONF_LOW）
    verdict: "best"（推荐 Top1，picks=[top1]）或 "uncertain"（甄选候选）
    """
    gap = score_gap if score_gap is not None else SCORE_GAP
    conf_low = scene_conf_low if scene_conf_low is not None else SCENE_CONF_LOW
    mc = max_candidates if max_candidates is not None else MAX_CANDIDATES
    if not scores:
        return "best", []
    # 候选池剔除硬性质量废片（模糊/过曝欠曝/闭眼）；全废组无推荐帧
    clean = [x for x in scores if not x.get("hard_waste")]
    if not clean:
        return "best", []
    s = sorted(clean, key=lambda x: -x["comp_score"])
    top1, top2 = s[0], s[1] if len(s) > 1 else None
    # 规则1：Top1-Top2 分差过小 → 无明确胜者
    if top2 is not None and (top1["comp_score"] - top2["comp_score"]) < gap:
        return "uncertain", s[:min(mc, len(s))]
    # 规则2：多维度互有胜负（帕累托冲突）
    if top2 is not None and _dim_conflict(top1, top2):
        return "uncertain", s[:min(mc, len(s))]
    # 规则3：场景置信度低 → 评分口径本身有歧义
    if conf < conf_low:
        return "uncertain", s[:min(mc, len(s))]
    # 明确：推荐最佳帧（自动打星 5）
    return "best", s[:1]


def pick_stars(verdict: str, picks: list[dict]) -> dict:
    """根据判定结果给出建议星级。

    best：Top1 -> 5 星（推荐帧）
    uncertain：候选第一张 -> 4 星（人工拍板前不给 5 星），其余保持 0
    返回 {path_or_key: star}
    """
    suggest = {}
    if not picks:
        return suggest
    if verdict == "best":
        key = _key(picks[0])
        suggest[key] = 5
    else:
        key = _key(picks[0])
        suggest[key] = 4
    return suggest


def _key(d: dict):
    return d.get("path") or d.get("key") or id(d)


# ---------------------------------------------------------------------------
# 命令行调试入口
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # 构造示例验证判定逻辑
    import random
    random.seed(0)
    print("场景预设：", {k: v["w"] for k, v in PRESETS.items()})
    for scene, conf in [("人像", 0.9), ("风景", 0.9), ("其他", 0.3)]:
        scores = []
        for i in range(4):
            m = {
                "path": f"img_{i}.jpg",
                "blur_score": random.uniform(80, 260),
                "over": random.uniform(0.0, 0.3),
                "under": random.uniform(0.0, 0.2),
                "aesthetic": random.uniform(40, 80),
                "ear": random.uniform(0.15, 0.35),
                "is_face": True,
            }
            scores.append(analyze_photo_score(m, scene))
        verdict, picks = judge_and_pick(scores, scene, conf)
        print(f"\n场景={scene} conf={conf} -> verdict={verdict} 候选数={len(picks)}")
        for p in picks:
            print(f"    {p['path']}: comp={p['comp_score']:.1f} "
                  f"(blur={p['blur_n']:.2f} aes={p['aes_n']:.2f} face={p['face_n']:.2f})")
