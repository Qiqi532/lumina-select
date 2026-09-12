# -*- coding: utf-8 -*-
"""
pipeline.py —— 端到端分析流水线（扫描→逐张指标→聚类→评分→入库）

流程（设计文档 4.3 / 5）：
    1. 扫描目录（JPEG/PNG 默认，RAW 可选）
    2. 阶段一（分块流式）：解码 → 质量（模糊/曝光）→ pHash，线程池并行
       → 画质模型分块批量 GPU 推理 → 人脸/闭眼 → 【分块批量写库】
    3. 阶段二（CLIP 批量，一次前向）：美学 + 场景，结果分块回写
    4. 相似聚类（时间戳连拍 + pHash 并查集）
    5. 组内场景自适应评分、废片判定、最佳帧推荐 / 不确定候选甄选 → 分块入库

关键特性：
    - 内存有界：按 CHUNK 分块处理，任何时刻只有一"块"原图在内存，5000+ 张不爆内存；
    - 断点续跑：阶段一/二结果实时落库，中断后重启只补算缺失部分；
    - 增量分析：mtime 未变化的照片跳过重算；
    - 并行化：解码/质量/哈希用线程池；画质模型与 CLIP 走 GPU 批量前向；
    - 分阶段计时：返回 phase_timing，供 scripts/benchmark.py 统计。

【修复 v0.4】相较上一版修正的问题：
    * 组循环内重建 `idx_of_path = {path: i for i in range(n)}` → O(G×N) 复杂度。
      5000 张照片时是 2500 万次字典插入，纯属浪费。已提到循环外构建一次。
    * 组内 `next(x for x in scored if x["path"] == p)` 与 `picks.index(p)` 均为
      线性查找 → O(组²)。改为字典索引。
    * 「高度重复」误伤：Top2 之外一律判废（20 张连拍废 18 张）。改为只有与最佳帧
      pHash 距离 ≤ DUP_HAMMING_STRICT 的真·重复才判废，落选者仅标记"相似·落选"。
    * 取消不可达：as_completed 后再调 f.cancel() 对已执行/执行中的任务无效，
      且 ThreadPoolExecutor.__exit__ 会阻塞等待全部完成。改为分块提交 +
      块间检查，取消在一个块内（通常 <2s）生效。
    * futures dict 持有全部结果 → 所有解码后的 RGB 数组驻留内存直到循环结束。
      改为分块提交，用完即弃。

独立命令行调试：python -m engine.pipeline <目录> [--db 路径]
"""
from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from . import config, loader, quality, similarity, scorer
from . import inference
from .log import get_logger
from .store import PhotoStore
from services.asset_pairing import pair_assets

try:
    from . import faces as _faces
except Exception:  # pragma: no cover
    _faces = None

_log = get_logger("pipeline")

ANALYZE_SIZE = config.ANALYZE_SIZE        # 质量/人脸分析尺寸
CLIP_BATCH = config.CLIP_BATCH            # CLIP 批量推理大小（GPU 友好）
CLIP_LOAD_SIZE = config.CLIP_LOAD_SIZE    # CLIP 重解码尺寸
QUALITY_WORKERS = config.QUALITY_WORKERS  # 质量检测线程池并行数
# 分块大小：内存与"取消响应速度"的共同旋钮。块越大 GPU 利用率越高，
# 但内存占用与取消延迟同步上升。经验值 = 线程数 × 4。
CHUNK = max(QUALITY_WORKERS * 4, 8)


def _apply_asset_pairing(meta: list[dict]) -> None:
    """Attach stable asset ids, roles, and preferred preview paths in place."""
    by_path = {item["path"]: item for item in meta}
    timestamps = {item["path"]: item.get("ts") for item in meta}
    for asset in pair_assets(
        by_path,
        lambda path: timestamps.get(str(path)),
    ):
        for member, role in ((asset.raw_path, "raw"), (asset.jpeg_path, "jpeg")):
            if member is None:
                continue
            item = by_path[str(member)]
            item["asset_pair_id"] = asset.asset_id
            item["asset_role"] = role
            item["preview_path"] = str(asset.preview_path)


def _same_asset(left: dict, right: dict) -> bool:
    left_id = left.get("asset_pair_id")
    return bool(left_id and left_id == right.get("asset_pair_id"))


def _ai_model_signature() -> str:
    """AI 模型签名（美学 + 画质 + 闭眼）。任一模型升级 → 签名变化 → 全量重算。"""
    aes = inference.aesthetic_model_name()
    q = inference.quality_model_name()
    eye = _faces.eye_model_name() if _faces is not None else "none"
    return f"aes={aes}|q={q}|eye={eye}"


def _eyes_closed_from_row(row: dict) -> bool:
    """从已存 DB 行重建融合闭眼判定（增量分析时不重算照片，需忠实还原当时结论）。

    判定 = EAR<阈值 或 闭眼分类器概率>阈值（与 analyze 时 detect_face_and_eyes 一致）。
    """
    ear = row.get("eye_open")
    ep = row.get("eye_close_prob")
    if ear is not None and float(ear) < scorer.EAR_WASTE:
        return True
    if ep is not None and float(ep) > (_faces.EYE_MODEL_CONF if _faces is not None else config.EYE_MODEL_CONF):
        return True
    return False


def _stage1_worker(path: str) -> dict:
    """线程池 worker：解码 + 质量 + pHash（纯 cv2/PIL，无共享模型，线程安全）。

    返回 dict：ok / rgb / pil / q(质量指标) / phash。
    """
    started_at = time.perf_counter()
    try:
        pil = loader.load_image(path, max_size=ANALYZE_SIZE)
        if pil is None:
            return {"ok": False, "path": path}
        rgb = np.asarray(pil, dtype=np.uint8)
        q = quality.analyze_image_array(rgb)
        ph = similarity.phash_of_image(pil) or ""
        return {"ok": True, "path": path, "rgb": rgb, "pil": pil, "q": q, "phash": ph,
                "analysis_ms": (time.perf_counter() - started_at) * 1000.0}
    except Exception as e:
        _log.warning("阶段一处理失败 %s: %s", path, e)
        return {"ok": False, "path": path}


def _write_stage1_batch(store: PhotoStore, metas: list[dict], results: list[dict]):
    """批量写入阶段一结果（单事务；断点续跑的最小落库单元）。"""
    rows = []
    backend_name = inference.get_backend().name
    quality_model = inference.quality_model_name()
    for m, s1 in zip(metas, results):
        base = {"path": m["path"], "fname": m["fname"], "ts": m["ts"], "mtime": m["mtime"],
                "width": m["width"], "height": m["height"],
                "asset_pair_id": m.get("asset_pair_id"), "asset_role": m.get("asset_role"),
                "camera_model": m.get("camera_model"), "lens_model": m.get("lens_model"),
                "focal_length": m.get("focal_length"), "shutter_speed": m.get("shutter_speed"),
                "aperture": m.get("aperture"), "iso": m.get("iso"),
                "analysis_backend": backend_name, "quality_model": quality_model,
                "analysis_ms": s1.get("analysis_ms")}
        if not s1.get("ok"):
            # 解码失败的照片也记录 mtime，避免每次重试（仍是 NULL 指标，重算可覆盖）
            rows.append(base)
            continue
        rows.append({**base,
                     "blur_score": s1["blur_score"], "over_ratio": s1["over"],
                     "under_ratio": s1["under"], "brisque": s1["quality"],
                     "is_face": int(s1["is_face"]), "eye_open": s1["ear"],
                     "eye_close_prob": s1["eye_close_prob"], "phash": s1["phash"]})
    if rows:
        store.upsert_photos_batch(rows)
    for m, result in zip(metas, results):
        if result.get("ok"):
            store.replace_face_regions(m["path"], result.get("regions", []))


def _run_stage1(store: PhotoStore, metas: list[dict], idxs: list[int],
                use_faces: bool, progress_cb, cancel_check) -> tuple[dict, int]:
    """阶段一：分块流式处理，返回 (stage1 指标 dict, 已完成数)。

    分块是本项目内存与响应性的关键：
      * 每块提交 CHUNK 个任务 → 内存占用恒定，与照片总数无关；
      * 块与块之间检查取消标志 → 取消在一个块内生效（不再等完全部任务）；
      * 每块内部：线程池解码 → 画质模型批量 GPU 推理 → 人脸检测（单例）→ 批量写库。
    """
    stage1: dict[int, dict] = {}
    total = len(idxs)
    done = 0
    for start in range(0, total, CHUNK):
        if cancel_check and cancel_check():
            break
        chunk_idxs = idxs[start:start + CHUNK]
        chunk_metas = [metas[i] for i in chunk_idxs]

        # --- a) 线程池解码 + 质量 + pHash（纯 CPU，GIL 可释放）---
        with ThreadPoolExecutor(max_workers=QUALITY_WORKERS) as ex:
            raw = list(ex.map(_stage1_worker, [m.get("preview_path", m["path"]) for m in chunk_metas]))

        # --- b) 画质模型批量 GPU 推理（一次前向算完整个块）---
        valid_pos = [k for k, r in enumerate(raw) if r.get("ok")]
        qscores: dict[int, float | None] = {}
        if valid_pos:
            try:
                vals = inference.quality_scores([raw[k]["rgb"] for k in valid_pos])
                qscores = dict(zip(valid_pos, vals))
            except Exception as e:
                _log.warning("画质模型批量推理失败，本块退化为纯拉普拉斯：%s", e)

        # --- c) 人脸/闭眼（MediaPipe 单例，串行安全）+ 汇总 ---
        results = []
        for k, r in enumerate(raw):
            if not r.get("ok"):
                results.append({"ok": False})
                continue
            face = {"is_face": False, "ear": None, "eyes_closed": False,
                    "eye_close_prob": None, "regions": []}
            if use_faces and _faces is not None:
                try:
                    fr = _faces.detect_face_and_eyes(r["rgb"], r["pil"])
                    face = {"is_face": fr["is_face"], "ear": fr["ear"],
                            "eyes_closed": fr["eyes_closed"],
                            "eye_close_prob": fr["eye_close_prob"],
                            "regions": fr.get("regions", [])}
                except Exception as e:
                    _log.warning("人脸检测异常 %s: %s", r["path"], e)
            results.append({
                "ok": True,
                "blur_score": r["q"]["blur_score"],
                "over": r["q"]["over"], "under": r["q"]["under"],
                "quality": qscores.get(k),      # 0-100，越高越好（可为 None）
                "is_face": face["is_face"], "ear": face["ear"],
                "eyes_closed": face["eyes_closed"],
                "eye_close_prob": face["eye_close_prob"],
                "regions": face["regions"],
                "analysis_ms": r.get("analysis_ms", 0.0),
                "phash": r["phash"],
            })

        # --- d) 批量落库（断点续跑：已完成部分立即持久化）---
        _write_stage1_batch(store, chunk_metas, results)
        for i, s in zip(chunk_idxs, results):
            stage1[i] = s
        done += len(chunk_idxs)
        if progress_cb:
            progress_cb("质量与哈希", done, total)
    return stage1, done


def analyze_directory(root: str, db_path: str,
                      include_raw: bool = False,
                      use_faces: bool = True,
                      progress_cb=None,
                      cancel_check=None,
                      scene_override: dict | None = None,
                      forced_scene: str | None = None,
                      weights: tuple | None = None,
                      burst_interval_ms: float | None = None,
                      phash_threshold: int | None = None,
                      score_gap: float | None = None,
                      scene_conf_low: float | None = None) -> dict:
    """对目录执行完整分析并入库。

    参数：
        root            待分析目录
        db_path         SQLite 路径
        include_raw     是否包含 RAW
        use_faces       是否做人脸/闭眼检测（人像功能依赖）
        progress_cb     optional 回调(阶段名, 完成数, 总数)
        cancel_check    optional 返回 True 时中止
        scene_override  optional {path: scene} 人工修正的场景覆盖
        forced_scene    可选，强制整批按指定场景（人像/风光/其他）
        weights         可选，自定义综合分权重 (w0,w1,w2,w3)，覆盖场景预设
        burst_interval_ms 可选，连拍间隔阈值覆盖（默认 config）
        phash_threshold 可选，pHash 距离阈值覆盖（默认 config）
        score_gap       可选，Top1-Top2 分差阈值覆盖（默认 config）
        scene_conf_low  可选，场景置信度阈值覆盖（默认 config）
    返回：
        汇总 dict（total, new_analyzed, groups, wastes, bests, uncertain_groups,
        phase_timing 等）
    """
    t_start = time.time()
    phase_t = {}       # 分阶段耗时（秒），供 benchmark

    def _phase_start(name):
        phase_t[name] = time.time()

    def _phase_end(name):
        if name in phase_t:
            phase_t[name] = time.time() - phase_t[name]

    def report(phase: str, done: int, total: int):
        if progress_cb:
            progress_cb(phase, done, total)

    def cancelled(phase: str, **extra) -> dict:
        payload = {"total": n, "message": "已取消", "cancelled": True,
                   "new_analyzed": n_new, "phase_timing": phase_t}
        payload.update(extra)
        _phase_end(phase)
        return payload

    # 阈值覆盖（默认取 config，界面滑块修改即生效）
    burst_interval_ms = burst_interval_ms if burst_interval_ms is not None else config.BURST_INTERVAL_MS
    phash_threshold = phash_threshold if phash_threshold is not None else config.PHASH_THRESHOLD
    score_gap = score_gap if score_gap is not None else config.SCORE_GAP
    scene_conf_low = scene_conf_low if scene_conf_low is not None else config.SCENE_CONF_LOW

    # ---- 0) 模型环境准备：缓存自愈 + 变量注入（避免离线/缓存残缺导致整体崩溃）----
    try:
        from . import models_guard

        models_guard.apply_env()
        models_guard.repair_hf_cache()
    except Exception as e:  # noqa: BLE001 —— 环境准备失败不应阻断分析
        _log.warning("模型环境准备失败（继续尝试加载）：%s", e)

    # ---- 1) 扫描 ----
    _phase_start("扫描")
    report("扫描", 0, 1)
    root_abs = os.path.abspath(root)
    paths = loader.scan_directory(root, include_raw=include_raw)
    n = len(paths)
    if n == 0:
        report("扫描", 1, 1)
        _phase_end("扫描")
        return {"total": 0, "message": "目录中没有找到支持的图片（JPEG/PNG）", "phase_timing": phase_t}
    _phase_end("扫描")

    # ---- 2) 读取元数据 + 增量/断点判断 ----
    _phase_start("读取元数据")
    report("读取元数据", 0, n)
    with PhotoStore(db_path) as store:
        prev_root = store.get_meta("source_root")
        if prev_root and prev_root != root_abs:
            store.clear_all()
        prev_sig = store.get_meta("ai_models")
        cur_sig = _ai_model_signature()
        if prev_sig and prev_sig != cur_sig:
            store.clear_all()
        elif prev_sig is None and store.count_photos() > 0:
            store.clear_all()
        meta: list[dict] = []
        old_map = store.photos_map()
        need_stage1: list[int] = []
        need_clip: set[int] = set()
        for i, p in enumerate(paths):
            if cancel_check and cancel_check():
                return {"total": n, "message": "已取消", "cancelled": True, "phase_timing": phase_t}
            ex = loader.read_exif(p)
            mt = loader.get_mtime(p)
            meta.append({"path": p, "fname": os.path.basename(p), "ts": ex["ts"],
                         "mtime": mt, "width": ex["width"], "height": ex["height"],
                         "camera_model": ex.get("camera_model"),
                         "lens_model": ex.get("lens_model"),
                         "focal_length": ex.get("focal_length"),
                         "shutter_speed": ex.get("shutter_speed"),
                         "aperture": ex.get("aperture"), "iso": ex.get("iso")})
            old = old_map.get(p)
            mtime_ok = old and abs((old.get("mtime") or 0) - mt) < 1e-6
            stage1_ok = mtime_ok and old.get("blur_score") is not None and old.get("phash")
            if not stage1_ok:
                need_stage1.append(i)
                need_clip.add(i)
            elif old and (old.get("aesthetic") is None or old.get("scene") is None):
                need_clip.add(i)
            if progress_cb:
                progress_cb("读取元数据", i + 1, n)
        _apply_asset_pairing(meta)
        store.upsert_photos_batch(
            [
                {
                    "path": item["path"],
                    "asset_pair_id": item.get("asset_pair_id"),
                    "asset_role": item.get("asset_role"),
                }
                for item in meta
            ]
        )
        _phase_end("读取元数据")

        # ---- 3) 阶段一：质量/画质模型/人脸/phash（分块流式 + 线程池 + 批量 GPU）----
        n_new = len(need_stage1)
        stage1: dict[int, dict] = {}
        if n_new > 0:
            _phase_start("质量与哈希")
            report("质量与哈希", 0, n_new)
            stage1, done = _run_stage1(store, meta, need_stage1, use_faces,
                                       progress_cb, cancel_check)
            _phase_end("质量与哈希")
            if cancel_check and cancel_check():
                return cancelled("质量与哈希", new_analyzed=done)

        # ---- 4) 阶段二：CLIP 批量（美学 + 场景，一次前向，GPU 优先）----
        _phase_start("美学与场景")
        order = sorted(need_clip)
        clip_res: dict[int, dict] = {}
        if order:
            report("美学与场景", 0, len(order))
            for b in range(0, len(order), CLIP_BATCH):
                if cancel_check and cancel_check():
                    return cancelled("美学与场景")
                batch_idx = order[b:b + CLIP_BATCH]
                batch_imgs, valid = [], []
                for i in batch_idx:
                    im = loader.load_review_preview(
                        meta[i].get("preview_path", meta[i]["path"]),
                        max_size=CLIP_LOAD_SIZE,
                    )
                    if im is not None:
                        batch_imgs.append(im)
                        valid.append(i)
                if valid:
                    res = inference.scene_and_aesthetics(batch_imgs)
                    rows = []
                    for i, r in zip(valid, res):
                        clip_res[i] = r
                        rows.append({"path": meta[i]["path"],
                                     "aesthetic": r["aesthetic"],
                                     "scene": r["scene"],
                                     "scene_conf": r["scene_conf"],
                                     "scene_model": inference.aesthetic_model_name()})
                    # 【断点续跑】逐批回写美学/场景（批量事务）
                    store.upsert_photos_batch(rows)
                if progress_cb:
                    progress_cb("美学与场景", min(b + CLIP_BATCH, len(order)), len(order))
        _phase_end("美学与场景")

        # ---- 5) pHash 汇总（复用 stage1 / 已存库，避免重新解码原图）----
        _phase_start("相似聚类")
        report("相似聚类", 0, n)
        phash_hexes = []
        ts_list = []
        for i, m in enumerate(meta):
            if i in stage1 and stage1[i].get("ok"):
                h = stage1[i]["phash"]
            else:
                h = (old_map.get(m["path"]) or {}).get("phash") or ""
            phash_hexes.append(h)
            ts_list.append(m["ts"])

        hexes, group_ids, groups = similarity.group_similar(
            [m["path"] for m in meta], ts_list,
            burst_interval_ms=burst_interval_ms, phash_threshold=phash_threshold,
            progress_cb=lambda d, t: report("相似聚类", d, t),
            phash_hexes=phash_hexes)
        _phase_end("相似聚类")

        # ---- 6) 场景覆盖（人工修正 / 强制场景）----
        scene_override = scene_override or {}

        def resolve_scene(idx):
            p = meta[idx]["path"]
            if p in scene_override:
                return scene_override[p], 1.0
            if forced_scene:
                return forced_scene, 1.0
            old_row = old_map.get(p)
            if old_row and old_row.get("scene_manual"):
                return old_row["scene_manual"], 1.0
            if idx in clip_res:
                return clip_res[idx]["scene"], clip_res[idx]["scene_conf"]
            if old_row and old_row.get("scene"):
                return old_row["scene"], old_row.get("scene_conf") or 1.0
            return "其他", 1.0

        def resolve_aesthetic(idx):
            if idx in clip_res:
                return clip_res[idx]["aesthetic"]
            old = old_map.get(meta[idx]["path"])
            if old and old.get("aesthetic") is not None:
                return old["aesthetic"]
            return 50.0

        # ---- 7) 组内评分、废片判定、最佳帧/不确定甄选 ----
        _phase_start("评分与甄选")
        report("评分与甄选", 0, len(groups))

        # 【修复 v0.4】路径→索引映射只构建一次。
        # 旧实现把它写在组循环内部，复杂度 O(组数 × 照片数)；5000 张照片、
        # 1000 个组就是 500 万次无谓的字典插入，纯 CPU 空转。
        idx_of_path = {m["path"]: i for i, m in enumerate(meta)}

        n_best = 0
        n_uncertain_groups = 0
        n_candidate_photos = 0
        waste_count = 0

        for gi, group in enumerate(groups):
            if cancel_check and cancel_check():
                return cancelled("评分与甄选")
            member_idx = [idx_of_path[p] for p in group]

            # 场景：取组内主要场景（人像/风光优先于其他）
            scene_counts: dict[str, int] = {}
            for i in member_idx:
                s, _ = resolve_scene(i)
                scene_counts[s] = scene_counts.get(s, 0) + 1
            if scene_counts.get("人像", 0) > 0:
                scene = "人像"
            elif scene_counts.get("风光", 0) > 0:
                scene = "风光"
            else:
                scene = "其他"
            confs = [resolve_scene(i)[1] for i in member_idx]
            group_conf = sum(confs) / len(confs) if confs else 1.0

            # 组内每张的综合分
            scored = []
            for i in member_idx:
                p = meta[i]["path"]
                m = stage1.get(i)
                if m is None or not m.get("ok"):
                    old = old_map.get(p) or {}
                    m = {"ok": True,
                         "blur_score": old.get("blur_score") or 0.0,
                         "over": old.get("over_ratio") or 0.0,
                         "under": old.get("under_ratio") or 0.0,
                         "is_face": bool(old.get("is_face")),
                         "ear": old.get("eye_open"),
                         "quality": old.get("brisque"),
                         "eyes_closed": _eyes_closed_from_row(old)}
                aes = resolve_aesthetic(i)
                s, _ = resolve_scene(i)
                photo_score = scorer.analyze_photo_score({**m, "aesthetic": aes}, scene, weights)
                # 闭眼硬判不再要求 scene == "人像"（见 scorer.waste_reasons）
                hw = scorer.is_hard_waste(
                    scene, m["blur_score"], m["over"], m["under"],
                    bool(m.get("eyes_closed", False)), m.get("quality"),
                    is_face=bool(m.get("is_face", False)))
                scored.append({**photo_score, "path": p, "hard_waste": hw})

            verdict, picks = scorer.judge_and_pick(scored, scene, group_conf,
                                                   score_gap=score_gap,
                                                   scene_conf_low=scene_conf_low)
            best_path = picks[0]["path"] if picks else ""
            is_uncertain = (verdict == "uncertain")
            n_candidates = len(picks) if is_uncertain else 0
            if is_uncertain:
                n_uncertain_groups += 1
            elif picks:
                n_best += 1

            # ---- 「高度重复」判定（修复误伤）----
            # 旧实现：组内排序 Top2 之外一律判废。那是"排名落败"，不是"重复"。
            #   组大小 3→废1、5→废3、10→废8、20→废18，摄影师会平白丢掉大量可用素材。
            # 新实现：只有与最佳帧 pHash 汉明距离极近（≤ DUP_HAMMING_STRICT）
            #   才算真·重复；其余落选者仅标记 is_similar_loser（默认收起，可找回）。
            dup_paths: set[str] = set()
            loser_paths: set[str] = set()
            if best_path and len(scored) > 1:
                best_i = idx_of_path.get(best_path)
                best_hex = hexes[best_i] if best_i is not None else ""
                for x in scored:
                    if x["path"] == best_path:
                        continue
                    i = idx_of_path.get(x["path"])
                    if i is not None and best_i is not None and _same_asset(meta[best_i], meta[i]):
                        continue
                    h = hexes[i] if i is not None else ""
                    if best_hex and h and similarity.hamming_hex(best_hex, h) <= config.DUP_HAMMING_STRICT:
                        dup_paths.add(x["path"])
                    else:
                        loser_paths.add(x["path"])

            # ---- 组内每张入库 ----
            # 【修复 v0.4】旧实现用 next(x for x in scored if x["path"] == p) 与
            # picks.index(p) 做线性查找，复杂度 O(组²)。改为字典，O(1)。
            scored_by_path = {x["path"]: x for x in scored}
            pick_rank = {x["path"]: k + 1 for k, x in enumerate(picks)}

            rows = []
            for i in member_idx:
                p = meta[i]["path"]
                m = stage1.get(i, {})
                m_ok = bool(m.get("ok"))
                old = old_map.get(p) or {}
                ph = hexes[i]
                ps = scored_by_path.get(p)
                comp = ps["comp_score"] if ps else 0.0

                # 指标取值：优先本次计算结果，否则回落到库中历史值
                blur = m.get("blur_score") if m_ok else (old.get("blur_score") or 0.0)
                over = m.get("over") if m_ok else (old.get("over_ratio") or 0.0)
                under = m.get("under") if m_ok else (old.get("under_ratio") or 0.0)
                qscore = m.get("quality") if m_ok else old.get("brisque")
                is_face = bool(m.get("is_face", False)) if m_ok else bool(old.get("is_face"))
                eyes_closed = (bool(m.get("eyes_closed", False)) if m_ok
                               else _eyes_closed_from_row(old))

                reasons = scorer.waste_reasons(
                    scene, blur, over, under, eyes_closed,
                    dup=p in dup_paths, brisque=qscore, is_face=is_face)

                s, conf = resolve_scene(i)
                aes = resolve_aesthetic(i)
                is_cand = is_uncertain and p in pick_rank
                rank = pick_rank.get(p, 0) if is_cand else 0
                is_best = (not is_uncertain and p == best_path)
                rows.append({
                    "path": p, "fname": meta[i]["fname"], "ts": meta[i]["ts"],
                    "mtime": meta[i]["mtime"], "width": meta[i]["width"],
                    "height": meta[i]["height"],
                    "asset_pair_id": meta[i].get("asset_pair_id"),
                    "asset_role": meta[i].get("asset_role"),
                    "scene": s, "scene_conf": conf,
                    "blur_score": blur, "over_ratio": over, "under_ratio": under,
                    "aesthetic": aes,
                    "eye_open": (m.get("ear") if m_ok else old.get("eye_open")),
                    "is_face": int(is_face), "brisque": qscore,
                    "eye_close_prob": (m.get("eye_close_prob") if m_ok
                                       else old.get("eye_close_prob")),
                    "phash": ph, "group_id": gi, "comp_score": comp,
                    "is_waste": 1 if reasons else 0,
                    "is_best": 1 if is_best else 0,
                    "is_uncertain": 1 if is_uncertain else 0,
                    "is_candidate": 1 if is_cand else 0,
                    "candidate_rank": rank,
                    "is_similar_loser": 1 if p in loser_paths else 0,
                    "waste_reasons": ",".join(reasons) if reasons else None,
                    # 人工标星优先级高于自动推荐：已有星级一律保留
                    "star": (old.get("star") or 0) or (5 if is_best else 0),
                    "label": (old.get("label") if old else None),
                })
                if reasons:
                    waste_count += 1
            store.upsert_photos_batch(rows)
            store.set_group(gi, len(group), best_path, is_uncertain, n_candidates)
            if is_uncertain:
                n_candidate_photos += n_candidates
            if progress_cb:
                progress_cb("评分与甄选", gi + 1, len(groups))
        _phase_end("评分与甄选")

        # ---- 汇总 ----
        store.set_meta("source_root", root_abs)
        store.set_meta("ai_models", _ai_model_signature())
        st = store.stats()
        _log.info("分析完成：%s 张，新增 %s，耗时 %.1fs", n, n_new, time.time() - t_start)
        return {
            "total": n,
            "new_analyzed": n_new,
            "groups": len(groups),
            "best_groups": n_best,
            "uncertain_groups": n_uncertain_groups,
            "candidate_photos": n_candidate_photos,
            "waste": waste_count,
            "scene_dist": st.get("scenes", {}),
            "aesthetic_model": inference.aesthetic_model_name(),
            "quality_model": inference.quality_model_name(),
            "eye_model": _faces.eye_model_name() if _faces is not None else "none",
            "elapsed": time.time() - t_start,
            "phase_timing": phase_t,
            "message": "分析完成",
        }


# ---------------------------------------------------------------------------
# 命令行调试入口
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    _root = sys.argv[1] if len(sys.argv) > 1 else "data"
    _db = sys.argv[2] if len(sys.argv) > 2 else "data/cull.db"
    print(f"分析目录: {_root} -> DB: {_db}")
    print("汇总：", analyze_directory(_root, _db))
