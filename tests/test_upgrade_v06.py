# -*- coding: utf-8 -*-
"""v0.6 S4-1：旧库升级提示 + 已复核清单导出兜底（门禁 G4）。

G4 的验收标准是「旧库升级后**人工决策零丢失**、指标**被重算**」。这里把它拆成两段：

  1. **schema 层**：v4 老库能被升级、升级前有备份，且升级这件事本身可被查询到
     （旧实现是静默升级，用户既不知道发生过升级，也不知道备份在哪）；
  2. **模型层**：库里若记录着已下线的分析后端（轻量版 heuristic），或模型签名变了，
     重算必须发生 —— 而 star / label / scene_manual 一条都不能少；并且"重算了什么、
     保留了什么"要随分析结果返回给 UI。

第三段是"兜底"：即使库坏了，用户手里也该有一份能直接读的复核清单（CSV）。
"""
from __future__ import annotations

import csv
import sqlite3

from engine.pipeline import analyze_directory
from engine.store import PhotoStore


# ---------------------------------------------------------------------------
# 与 tests/test_migrations.py 同款的 v4 最小库
# ---------------------------------------------------------------------------
def _create_minimal_v04(path):
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE photos (path TEXT PRIMARY KEY, fname TEXT, star INT, label TEXT)"
    )
    connection.executemany(
        "INSERT INTO photos(path, fname, star, label) VALUES (?, ?, ?, ?)",
        [
            ("C:/photos/IMG_0001.CR3", "IMG_0001.CR3", 4, "P"),
            ("C:/photos/IMG_0002.CR3", "IMG_0002.CR3", 0, "X"),
            ("C:/photos/IMG_0003.CR3", "IMG_0003.CR3", 0, None),
        ],
    )
    connection.commit()
    connection.close()


def test_legacy_schema_upgrade_is_reported_with_backup_path(tmp_path):
    db_path = tmp_path / "legacy.db"
    _create_minimal_v04(db_path)

    with PhotoStore(str(db_path), enable_wal=False) as store:
        report = store.upgrade_report()

        assert report["schema_upgraded"] is True
        assert report["backup_path"], "升级前必须留备份，且路径要能报给用户"
        assert tmp_path.joinpath("legacy.db.pre-v05.bak").exists()
        assert report["needs_attention"] is True
        # 人工决策一条不少（2 条：一条标星、一条判废）
        assert report["decision_count"] == 2
        assert "备份" in store.upgrade_summary()
        assert "保留" in store.upgrade_summary()


def test_current_version_project_needs_no_attention(tmp_path):
    """新库不该触发"旧项目"提示，否则等于给所有用户加噪音。"""
    with PhotoStore(str(tmp_path / "new.db"), enable_wal=False) as store:
        report = store.upgrade_report()
        assert report["needs_attention"] is False
        assert report["legacy_backends"] == []
        assert store.upgrade_summary() == "项目为当前版本，无需升级。"


def test_retired_backend_is_detected_via_registry_not_hardcoding(tmp_path):
    """判定口径是"还在不在后端注册表里"，而不是硬编码 'heuristic'。"""
    with PhotoStore(str(tmp_path / "p.db"), enable_wal=False) as store:
        store.upsert_photos_batch([
            {"path": "C:/a/1.jpg", "analysis_backend": "heuristic", "star": 5, "label": "P"},
            {"path": "C:/a/2.jpg", "analysis_backend": "torch", "star": 0},
            {"path": "C:/a/3.jpg", "analysis_backend": None, "star": 0},
        ])
        assert store.legacy_backends() == ["heuristic"]
        assert store.count_decisions() == 1


def test_export_decisions_writes_excel_readable_csv(tmp_path):
    db_path = tmp_path / "decisions.db"
    with PhotoStore(str(db_path), enable_wal=False) as store:
        store.upsert_photos_batch([
            {"path": "C:/photo/IMG_0001.CR3", "star": 5, "label": "P",
             "decision_source": "manual", "decision_updated_at": 1_700_000_000.0},
            {"path": "C:/photo/IMG_0002.CR3", "star": 0, "label": "X",
             "decision_source": "manual", "decision_updated_at": 1_700_000_100.0},
            {"path": "C:/photo/IMG_0003.CR3", "star": 3, "scene_manual": "人像"},
            # 无任何人工痕迹的行必须被排除
            {"path": "C:/photo/IMG_0004.CR3", "star": 0},
        ])

        target = tmp_path / "out" / "已复核清单.csv"
        result = store.export_decisions(target)

        assert result["rows"] == 3
        assert result["path"] == str(target)
        assert result["bytes"] > 0

    # utf-8-sig：Excel 打开中文不乱码
    raw = target.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    with open(target, encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    assert rows[0] == ["文件路径", "文件名", "星级", "保留/排除", "人工场景", "决策来源", "决策时间"]
    by_name = {row[1]: row for row in rows[1:]}
    assert by_name["IMG_0001.CR3"][2:] == ["5", "P", "", "manual", rows[1][6]]
    assert by_name["IMG_0002.CR3"][3] == "X"
    assert by_name["IMG_0003.CR3"][4] == "人像"
    assert "IMG_0004.CR3" not in by_name
    # 时间列是可读的本地时间，不是裸 epoch（人要看懂）
    assert by_name["IMG_0001.CR3"][6].count("-") == 2


# ---------------------------------------------------------------------------
# G4 端到端：旧库（旧后端 + 旧签名）→ 重分析 → 决策零丢失、指标被重算
# ---------------------------------------------------------------------------
STUB_BACKEND = "s4-stub"
STUB_AESTHETIC = "s4-aesthetic-v1"
STUB_QUALITY = "s4-quality-v1"


class _StubBackend:
    name = STUB_BACKEND

    def quality_scores(self, rgbs):
        return [40.0] * len(rgbs)

    def scene_and_aesthetics(self, images):
        return [{"aesthetic": 60.0, "scene": "人像", "scene_conf": 1.0} for _ in images]

    def quality_model_name(self):
        return STUB_QUALITY

    def aesthetic_model_name(self):
        return STUB_AESTHETIC


def test_g4_legacy_backend_recompute_keeps_every_human_decision(
    full_data_dir, tmp_db_path, monkeypatch
):
    from engine import inference

    inference.register_backend(STUB_BACKEND, _StubBackend)
    inference.reset_cache()
    try:
        paths = full_data_dir[1]
        analyze_directory(str(full_data_dir[0]), tmp_db_path,
                          use_faces=False, backend=STUB_BACKEND)

        picked, rejected = paths["burstA_0"], paths["sharp"]
        with PhotoStore(tmp_db_path) as store:
            # 逐张人工决策（走 UI 同款入口），再造出"这个库来自旧版本"的现场：
            # 后端是已下线的 heuristic、模型签名是旧的、且指标列被清空。
            store.set_pick(picked, "P")
            store.set_pick(rejected, "X")
            store.set_scene_manual(paths["blurry"], "人像")
            store.conn.execute(
                "UPDATE photos SET analysis_backend='heuristic', "
                "quality_model='opencv-technical-v1', scene_model='opencv-heuristic'"
            )
            store.conn.commit()
            store.set_meta("ai_models", "legacy-signature-heuristic")
            assert store.legacy_backends() == ["heuristic"]
            assert store.count_decisions() == 3

        result = analyze_directory(str(full_data_dir[0]), tmp_db_path,
                                   use_faces=False, backend=STUB_BACKEND)

        # 1) 重算被如实上报，且说明"旧后端是谁"
        assert result["upgrade"]["recomputed"] is True
        assert result["upgrade"]["legacy_backends"] == ["heuristic"]
        assert result["upgrade"]["decisions_preserved"] == 3

        with PhotoStore(tmp_db_path) as store:
            # 2) 人工决策零丢失
            assert store.get_photo(picked)["label"] == "P"
            assert store.get_photo(picked)["star"] == 5
            assert store.get_photo(rejected)["label"] == "X"
            assert store.get_photo(paths["blurry"])["scene_manual"] == "人像"
            # 3) 指标确实被重算（旧后端名已被新后端覆盖，且度量非空）
            rows = store.all_photos()
            assert rows
            assert {row["analysis_backend"] for row in rows} == {STUB_BACKEND}
            assert {row["quality_model"] for row in rows} == {STUB_QUALITY}
            assert all(row["blur_score"] is not None for row in rows)
            assert all(row["aesthetic"] is not None for row in rows)
            # 4) 旧后端名已从库中消失 → "旧项目"提示会随之关闭
            assert store.legacy_backends() == []
    finally:
        inference._BACKENDS.pop(STUB_BACKEND, None)
        inference.reset_cache()


def test_g4_running_analysis_on_untouched_project_does_not_claim_recompute(
    full_data_dir, tmp_db_path
):
    """没有旧库、没有换模型时不许谎报"已重算"（否则提示会变成狼来了）。"""
    from engine import inference

    inference.register_backend(STUB_BACKEND, _StubBackend)
    inference.reset_cache()
    try:
        analyze_directory(str(full_data_dir[0]), tmp_db_path,
                          use_faces=False, backend=STUB_BACKEND)
        second = analyze_directory(str(full_data_dir[0]), tmp_db_path,
                                   use_faces=False, backend=STUB_BACKEND)
        assert second["upgrade"] == {"recomputed": False}
    finally:
        inference._BACKENDS.pop(STUB_BACKEND, None)
        inference.reset_cache()
