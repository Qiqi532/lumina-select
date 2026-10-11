# -*- coding: utf-8 -*-
"""pipeline.py 单测：增量分析、断点续跑、场景手动修正持久化（用小型图片，CPU）。"""
from __future__ import annotations

import pytest

from engine import inference
from engine.pipeline import _apply_asset_pairing, analyze_directory
from engine.store import PhotoStore
from PIL import Image

# ---------------------------------------------------------------------------
# 【v0.6 S2-1】本模块只验证【流水线编排】，不验证具体模型。
#
# 历史上靠把 config.INFERENCE_BACKEND 钉成 "heuristic"（无需 torch）来做到这一点。
# 该进程级开关与轻量后端已随"单版本"收敛一并删除，改为向注册表注入一个**确定性
# 测试后端**，并显式传参 analyze_directory(..., backend="test-stub")：
#   * 与"后端是调用参数、不是进程状态"的新设计一致；
#   * 顺带把 v0.7 ONNX 后端要用的 register_backend 扩展点纳入回归；
#   * 不加载 torch/CLIP，测试仍快、且离线可跑。
# ---------------------------------------------------------------------------
STUB_BACKEND = "test-stub"
STUB_QUALITY_MODEL = "test-quality-v1"
STUB_AESTHETIC_MODEL = "test-aesthetic-v1"


class _StubBackend:
    name = STUB_BACKEND

    def quality_scores(self, rgbs):
        return [50.0] * len(rgbs)

    def scene_and_aesthetics(self, images):
        return [{"aesthetic": 50.0, "scene": "其他", "scene_conf": 1.0} for _ in images]

    def quality_model_name(self):
        return STUB_QUALITY_MODEL

    def aesthetic_model_name(self):
        return STUB_AESTHETIC_MODEL


@pytest.fixture(autouse=True)
def _stub_backend():
    """把确定性测试后端注册进注册表，退出时清理，避免污染全局。"""
    inference.register_backend(STUB_BACKEND, _StubBackend)
    inference.reset_cache()
    yield
    inference._BACKENDS.pop(STUB_BACKEND, None)
    inference.reset_cache()


def _run(full_dir, db, **kw):
    kw.setdefault("use_faces", False)
    kw.setdefault("backend", STUB_BACKEND)
    return analyze_directory(str(full_dir[0]), db, **kw)


def test_incremental_second_run_no_reanalyze(full_data_dir, tmp_db_path, tmp_path):
    """第二次运行同目录：mtime 未变 → new_analyzed=0（不重算阶段一/二）。"""
    r1 = _run(full_data_dir, tmp_db_path)
    assert r1["new_analyzed"] == 8            # 8 张（连拍A3+B2+模糊+过曝+清晰）
    r2 = _run(full_data_dir, tmp_db_path)
    assert r2["total"] == r1["total"]
    assert r2["new_analyzed"] == 0


def test_breakpoint_resume_stage1(full_data_dir, tmp_db_path):
    """模拟阶段一中断：某张缺失阶段一字段，重启只补算缺失那张。"""
    r1 = _run(full_data_dir, tmp_db_path)
    assert r1["new_analyzed"] == 8
    # 人为破坏：清掉一张的 blur_score（模拟断电时未写入）
    target = full_data_dir[1]["burstA_0"]
    with PhotoStore(tmp_db_path) as s:
        s.update_photo(target, blur_score=None, phash=None)
    r2 = _run(full_data_dir, tmp_db_path)
    assert r2["new_analyzed"] == 1            # 只补算缺失的一张
    with PhotoStore(tmp_db_path) as s:
        assert s.get_photo(target)["blur_score"] is not None


def test_breakpoint_resume_clip(full_data_dir, tmp_db_path):
    """模拟 CLIP 阶段中断：缺 aesthetic 的照片重启只重跑 CLIP（阶段一不重算）。"""
    r1 = _run(full_data_dir, tmp_db_path)
    target = full_data_dir[1]["burstA_1"]
    with PhotoStore(tmp_db_path) as s:
        s.update_photo(target, aesthetic=None, scene=None)
    r2 = _run(full_data_dir, tmp_db_path)
    assert r2["new_analyzed"] == 0            # 阶段一全部命中
    with PhotoStore(tmp_db_path) as s:
        assert s.get_photo(target)["aesthetic"] is not None


def test_scene_manual_override_survives_reanalysis(full_data_dir, tmp_db_path):
    """场景手动修正写入 DB 后，重分析不应被自动识别覆盖。"""
    r1 = _run(full_data_dir, tmp_db_path)
    target = full_data_dir[1]["sharp"]
    with PhotoStore(tmp_db_path) as s:
        s.set_scene_manual(target, "人像")
    r2 = _run(full_data_dir, tmp_db_path)
    with PhotoStore(tmp_db_path) as s:
        row = s.get_photo(target)
        assert row["scene"] == "人像"
        assert row["scene_manual"] == "人像"


def test_empty_dir(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    r = analyze_directory(str(d), str(tmp_path / "empty.db"))
    assert r["total"] == 0
    assert "没有找到" in r["message"]


def test_phase_timing_present(full_data_dir, tmp_db_path, caplog):
    """返回分阶段耗时（供 benchmark 使用）。"""
    r = _run(full_data_dir, tmp_db_path)
    assert set(r["phase_timing"]) >= {"扫描", "读取元数据", "质量与哈希", "美学与场景",
                                      "相似聚类", "评分与甄选"}
    for v in r["phase_timing"].values():
        assert isinstance(v, float)
    assert "阶段耗时" in caplog.text
    assert "质量分块" in caplog.text


def test_stage1_uses_embedded_preview_instead_of_demosaicing_raw(monkeypatch):
    from engine import loader, pipeline

    requests = []

    def preview(path, *, max_size):
        requests.append((path, max_size))
        return Image.new("RGB", (64, 48), color=(100, 130, 160))

    monkeypatch.setattr(loader, "load_review_preview", preview)
    monkeypatch.setattr(
        loader, "load_image",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("RAW demosaic must not run")),
    )

    result = pipeline._stage1_worker("C:/photos/IMG_0001.NEF")

    assert result["ok"] is True
    assert requests == [("C:/photos/IMG_0001.NEF", pipeline.ANALYZE_SIZE)]


def test_asset_pairing_metadata_is_persistable(tmp_path):
    raw = tmp_path / "IMG_0001.CR3"
    jpeg = tmp_path / "IMG_0001.JPG"
    raw.touch()
    jpeg.touch()
    meta = [
        {"path": str(raw), "ts": 1_000.0},
        {"path": str(jpeg), "ts": 1_500.0},
    ]

    _apply_asset_pairing(meta)

    assert meta[0]["asset_pair_id"] == meta[1]["asset_pair_id"]
    assert {meta[0]["asset_role"], meta[1]["asset_role"]} == {"raw", "jpeg"}
    assert meta[0]["preview_path"] == str(jpeg)


def test_read_exif_returns_inspector_fields_and_preserves_missing(tmp_path):
    from engine.loader import read_exif

    tagged = tmp_path / "tagged.jpg"
    exif = Image.Exif()
    exif[272] = "Lumina Camera"
    exif[42036] = "Prime 50mm"
    exif[37386] = (50, 1)
    exif[33434] = (1, 125)
    exif[33437] = (28, 10)
    exif[34855] = 400
    Image.new("RGB", (20, 10)).save(tagged, exif=exif)

    result = read_exif(str(tagged))

    assert result["camera_model"] == "Lumina Camera"
    assert result["lens_model"] == "Prime 50mm"
    assert result["focal_length"] == 50.0
    assert result["shutter_speed"] == "1/125"
    assert result["aperture"] == 2.8
    assert result["iso"] == 400

    plain = tmp_path / "plain.jpg"
    Image.new("RGB", (5, 5)).save(plain)
    missing = read_exif(str(plain))
    assert all(
        missing[key] is None
        for key in (
            "camera_model",
            "lens_model",
            "focal_length",
            "shutter_speed",
            "aperture",
            "iso",
        )
    )


def test_pipeline_persists_backend_models_and_analysis_time(full_data_dir, tmp_db_path):
    _run(full_data_dir, tmp_db_path)

    with PhotoStore(tmp_db_path) as photo_store:
        rows = photo_store.all_photos(order_by="path")

    assert rows
    # 【S2-1】断言跟随"本次实际使用的后端"（本模块注入的确定性后端），不再把某个
    # 后端的取值写死在测试里；同时验证落库的模型名确实来自该后端。
    assert {row["analysis_backend"] for row in rows} == {STUB_BACKEND}
    assert {row["quality_model"] for row in rows} == {STUB_QUALITY_MODEL}
    assert {row["scene_model"] for row in rows} == {STUB_AESTHETIC_MODEL}
    assert all(row["analysis_ms"] is not None and row["analysis_ms"] >= 0 for row in rows)
    # 【S2-1】发行版只有一个后端：默认必须是 torch，且轻量后端不得回流。
    assert inference.DEFAULT_BACKEND == "torch"
    assert "heuristic" not in inference.backend_names()


# ---------------------------------------------------------------------------
# 【P0-1 回归】模型签名变化 / 换分析源，都不得静默抹掉人工复核结果
# ---------------------------------------------------------------------------
def test_review_decisions_survive_model_signature_change(
    full_data_dir, tmp_db_path, monkeypatch
):
    """模型签名变化 → 只重算度量，人工标星/判废必须保留。"""
    _run(full_data_dir, tmp_db_path)
    target = full_data_dir[1]["sharp"]
    with PhotoStore(tmp_db_path) as store:
        store.set_pick(target, "P")
        assert store.get_photo(target)["star"] == 5

    # 制造"换模型"：美学模型名变了 → _ai_model_signature() 随之变化
    monkeypatch.setattr(
        _StubBackend, "aesthetic_model_name", lambda self: "test-aesthetic-v2"
    )
    _run(full_data_dir, tmp_db_path)

    with PhotoStore(tmp_db_path) as store:
        row = store.get_photo(target)
        assert row, "照片行不应被整行删除"
        assert row["label"] == "P", "人工『保留』标记被签名变化抹掉"
        assert row["star"] == 5, "人工星级被签名变化抹掉"
        # 度量确实按新签名重算过（非 NULL），并把新签名写回
        assert row["blur_score"] is not None
        assert row["aesthetic"] is not None
        assert "test-aesthetic-v2" in (store.get_meta("ai_models") or "")


def test_switching_source_root_archives_and_restores_decisions(
    full_data_dir, tmp_db_path, tmp_path
):
    """换分析源会清库，但人工决策应归档；切回原目录时恢复。"""
    import os

    source_a, paths_a = full_data_dir
    _run(full_data_dir, tmp_db_path)
    target = paths_a["sharp"]
    with PhotoStore(tmp_db_path) as store:
        store.set_pick(target, "P")
    archive_key = f"decisions:{os.path.abspath(str(source_a))}"

    # 切到另一个目录：应清空 photos，但先把 A 的决策归档进 meta
    other = tmp_path / "other_source"
    other.mkdir()
    Image.new("RGB", (32, 32), (10, 20, 30)).save(other / "b0.jpg")
    analyze_directory(str(other), tmp_db_path, use_faces=False, backend=STUB_BACKEND)

    with PhotoStore(tmp_db_path) as store:
        # 注意：PhotoStore.get_photo() 对不存在的行返回 {}（不是 None）
        assert not store.get_photo(target), "换源后旧目录的行应已清空"
        archived = store.get_meta(archive_key)
        assert archived and "P" in archived, "换源前未归档人工决策"

    # 切回 A：决策应被恢复
    _run(full_data_dir, tmp_db_path)
    with PhotoStore(tmp_db_path) as store:
        row = store.get_photo(target)
        assert row, "切回后照片行应重新出现"
        assert row["label"] == "P", "切回原目录后人工标记未恢复"
        assert row["star"] == 5, "切回原目录后人工星级未恢复"


# ---------------------------------------------------------------------------
# 【v0.6 S2-2 / G3 离线能力】阶段二不可用必须"降级完成"，且降级不得被永久固化
# ---------------------------------------------------------------------------
def test_stage2_failure_degrades_instead_of_raising(full_data_dir, tmp_db_path, monkeypatch):
    """断网 + 本地无模型缓存时，分析必须**完成**，而不是抛异常中断。

    阶段一（质量/哈希/人脸）已落库，用户应拿到可用的甄选结果，只是这次没用上
    美学/场景信号 —— 并被如实告知。
    """
    def boom(self, images):                       # noqa: ARG001
        raise OSError("Can't load image processor for 'openai/clip-vit-base-patch32'")

    monkeypatch.setattr(_StubBackend, "scene_and_aesthetics", boom)

    result = _run(full_data_dir, tmp_db_path)

    assert result["new_analyzed"] == 8, "阶段一应照常完成"
    assert result["degradations"], "降级必须被记录，UI 才能如实告知用户"
    assert result["degradations"][0]["stage"] == "美学与场景"
    with PhotoStore(tmp_db_path) as store:
        rows = store.all_photos()
        assert rows
        assert all(row["blur_score"] is not None for row in rows), "阶段一指标必须留下"
        assert all(row["aesthetic"] is None for row in rows), (
            "美学不可用时不得写入兜底值 50.0 —— 否则下一轮增量判断会认为『已算过』，"
            "降级被永久固化、模型就绪后也不会补齐"
        )


def test_degraded_run_self_heals_when_backend_recovers(
    full_data_dir, tmp_db_path, monkeypatch
):
    """降级跑过一次之后模型就绪：下一次分析应自动补齐，无需用户重建项目。"""
    def boom(self, images):                       # noqa: ARG001
        raise OSError("offline and no local snapshot")

    monkeypatch.setattr(_StubBackend, "scene_and_aesthetics", boom)
    degraded = _run(full_data_dir, tmp_db_path)
    assert degraded["degradations"]

    monkeypatch.undo()                            # 模型恢复可用
    healed = _run(full_data_dir, tmp_db_path)

    assert not healed["degradations"]
    with PhotoStore(tmp_db_path) as store:
        rows = store.all_photos()
        assert all(row["aesthetic"] is not None for row in rows)
        assert {row["scene_model"] for row in rows} == {STUB_AESTHETIC_MODEL}


# ---------------------------------------------------------------------------
# 【v0.6 S3-6】引擎深处上报的降级（GPU→CPU 回退 / 闭眼分类器不可用 / 美学头缺失）
# 必须与 pipeline 自己记录的降级**合并**后一起返回给 UI。
#
# 这些降级发生在 engine/aesthetics.py、engine/faces.py 里，那里拿不到
# progress_cb，因此走 engine.degradations 这个极小的待取列表。这一组用例锁定
# 两件事：(a) 合并确实发生；(b) 它只是「本轮事件日志」，不会跨轮残留。
# ---------------------------------------------------------------------------
def _record_and_return_stub(self, images):        # noqa: ARG001
    """模拟美学后端内部发生降级（GPU 回退 CPU），但**本次分析照常完成**。"""
    from engine import degradations

    degradations.record("美学与场景", "OutOfMemoryError: CUDA out of memory",
                        "本次改用 CPU 推理；结果一致但更慢")
    return [{"aesthetic": 50.0, "scene": "其他", "scene_conf": 1.0} for _ in images]


def test_engine_side_degradations_are_merged_into_result(
    full_data_dir, tmp_db_path, monkeypatch
):
    """引擎侧 record() 的降级必须出现在分析汇总里（否则用户永远不知道走了 CPU）。"""
    from engine import degradations

    monkeypatch.setattr(_StubBackend, "scene_and_aesthetics", _record_and_return_stub)

    result = _run(full_data_dir, tmp_db_path)

    assert result["new_analyzed"] == 8, "GPU 回退不应中断分析"
    assert result["degradations"] == [{
        "stage": "美学与场景",
        "reason": "OutOfMemoryError: CUDA out of memory",
        "effect": "本次改用 CPU 推理；结果一致但更慢",
    }]
    assert degradations.drain() == [], "取走后不得留残"


def test_engine_side_degradations_are_merged_with_pipeline_ones(
    full_data_dir, tmp_db_path, monkeypatch
):
    """pipeline 自己的降级（阶段二整体不可用）与引擎侧降级要同时出现在汇总里。"""
    from engine import degradations

    def boom(self, images):                       # noqa: ARG001
        degradations.record("闭眼分类器", "RuntimeError: model unavailable", "只用 EAR")
        raise OSError("Can't load image processor for 'openai/clip-vit-base-patch32'")

    monkeypatch.setattr(_StubBackend, "scene_and_aesthetics", boom)

    result = _run(full_data_dir, tmp_db_path)

    stages = sorted(d["stage"] for d in result["degradations"])
    assert stages == ["美学与场景", "闭眼分类器"], "两条降级都要如实上报"


def test_stale_degradation_events_never_leak_into_a_later_run(
    full_data_dir, tmp_db_path
):
    """上一轮残留的事件必须在分析开始时被清掉 —— 降级是「事件」，不是「状态」。"""
    from engine import degradations

    degradations.record("上一轮", "残留", "不应出现在本次结果里")

    result = _run(full_data_dir, tmp_db_path)

    assert all(d["stage"] != "上一轮" for d in result["degradations"])
    assert degradations.drain() == []


def test_cancelled_result_also_carries_engine_side_degradations(full_data_dir, tmp_db_path):
    """用户中途取消时，已发生的降级同样要带回（取消 ≠ 抹掉已告知的事实）。"""
    from engine import degradations

    state = {"stop": False}

    def progress(phase, done, total):             # noqa: ARG001
        if phase == "质量与哈希" and not state["stop"]:
            degradations.record("闭眼分类器", "RuntimeError: boom", "只用 EAR")
            state["stop"] = True

    result = analyze_directory(
        str(full_data_dir[0]), tmp_db_path,
        use_faces=False, backend=STUB_BACKEND,
        progress_cb=progress, cancel_check=lambda: state["stop"],
    )

    assert result["cancelled"] is True
    assert result["degradations"] == [
        {"stage": "闭眼分类器", "reason": "RuntimeError: boom", "effect": "只用 EAR"}
    ]
    assert degradations.drain() == []


def test_each_analysis_run_resets_one_shot_engine_flags(
    full_data_dir, tmp_db_path, monkeypatch
):
    """【v0.6 S3-补 / 0B】接线检查：每轮分析开始必须重置引擎侧的一次性降级标志。

    否则模型长期不可用时，上一轮的「已提示」标志会让后续轮次静默降级
    （engine/faces.py 的回归用例锁定行为，这条锁定管线确实调用了它）。
    """
    from engine import faces

    monkeypatch.setattr(faces, "_eye_failure_reported", True)

    _run(full_data_dir, tmp_db_path)

    assert faces._eye_failure_reported is False, "分析开始时必须重置一次性降级标志"
