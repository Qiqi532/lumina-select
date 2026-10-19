# -*- coding: utf-8 -*-
"""端到端冒烟测试：构造含连拍/模糊/过曝/清晰的图片集（带 EXIF 时间戳），
跑通「导入→分析→废片→推荐/甄选→星级→导出」全链并断言关键数字。

说明：分组细节（组数、best/uncertain 分布）依赖合成图 pHash，不在此作过强断言；
“最佳帧判定规则”由 tests/test_scorer.py 的单元测试精确覆盖。
"""
from __future__ import annotations

import csv
import os

import pytest

from engine.pipeline import analyze_directory
from engine.store import PhotoStore


def _hard_waste_reasons(row) -> list[str]:
    """硬性质量废因（排除'高度重复'）。"""
    reasons = (row.get("waste_reasons") or "").split(",")
    return [r for r in reasons if r and r != "高度重复"]


@pytest.mark.e2e
def test_e2e_full_chain(full_data_dir, tmp_db_path, tmp_path):
    d, paths = full_data_dir
    r = analyze_directory(str(d), tmp_db_path, use_faces=False)

    # 基本盘点
    assert r["total"] == 8
    assert r["new_analyzed"] == 8
    assert r["groups"] >= 1
    assert r["elapsed"] > 0
    # 推荐/甄选判定系统必须产出结果（best 或 uncertain）
    assert r["best_groups"] + r["uncertain_groups"] >= 1

    with PhotoStore(tmp_db_path) as s:
        rows = s.all_photos()
        by_path = {x["path"]: x for x in rows}

        # 连拍组 A 三张应同组（EXIF 连拍 → 确定性成立）
        ga = {by_path[paths["burstA_0"]]["group_id"],
              by_path[paths["burstA_1"]]["group_id"],
              by_path[paths["burstA_2"]]["group_id"]}
        assert len(ga) == 1

        # 夹具仅 64 px：模糊分不得用于自动判废，须人工确认；曝光仍可判废。
        assert by_path[paths["blurry"]]["blur_reliable"] == 0
        assert "模糊" not in _hard_waste_reasons(by_path[paths["blurry"]])
        assert by_path[paths["blurry"]]["is_uncertain"] == 1
        assert "过曝/欠曝" in _hard_waste_reasons(by_path[paths["overexposed"]])
        # 连拍 A 的小图模糊版同样不能自动剔除。
        assert "模糊" not in _hard_waste_reasons(by_path[paths["burstA_1"]])
        assert _hard_waste_reasons(by_path[paths["burstA_0"]]) == []

    # 人工甄选 + 星级
    with PhotoStore(tmp_db_path) as s:
        s.set_star(paths["burstA_0"], 5)
        s.set_label(paths["burstA_0"], "P")
        selected = [p for p in s.all_photos()
                    if (p.get("star") or 0) >= 4 or p.get("label") == "P"]
    assert paths["burstA_0"] in [p["path"] for p in selected]

    # 导出 CSV
    export_dir = str(tmp_path / "export")
    os.makedirs(export_dir, exist_ok=True)
    csv_path = os.path.join(export_dir, "smart_cull_export.csv")
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["path", "star", "label"])
        for p in selected:
            w.writerow([p["path"], p.get("star"), p.get("label")])
    with open(csv_path, encoding="utf-8-sig") as f:
        content = f.read()
    assert paths["burstA_0"] in content
