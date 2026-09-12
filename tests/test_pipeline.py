# -*- coding: utf-8 -*-
"""pipeline.py 单测：增量分析、断点续跑、场景手动修正持久化（用小型图片，CPU）。"""
from __future__ import annotations

from engine.pipeline import _apply_asset_pairing, analyze_directory
from engine.store import PhotoStore
from PIL import Image


def _run(full_dir, db, **kw):
    kw.setdefault("use_faces", False)
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


def test_phase_timing_present(full_data_dir, tmp_db_path):
    """返回分阶段耗时（供 benchmark 使用）。"""
    r = _run(full_data_dir, tmp_db_path)
    assert set(r["phase_timing"]) >= {"扫描", "读取元数据", "质量与哈希", "美学与场景",
                                      "相似聚类", "评分与甄选"}
    for v in r["phase_timing"].values():
        assert isinstance(v, float)


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
    assert {row["analysis_backend"] for row in rows} == {"heuristic"}
    assert {row["quality_model"] for row in rows} == {"opencv-technical-v1"}
    assert {row["scene_model"] for row in rows} == {"opencv-heuristic"}
    assert all(row["analysis_ms"] is not None and row["analysis_ms"] >= 0 for row in rows)
