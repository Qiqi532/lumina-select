"""Measure 1000-item review loading, asset filters, and facet combinations.

【T-08 验收口径】1,000 张库里，**任意三分面组合**响应 < 300 ms。
这里的"响应"包括两件事，缺一不可：

    1. 分面模型重算：选中/取消一个值后，所有面的计数都要重算（FacetModel.rebuild）；
    2. 筛选条绘制：把新的快照渲染成控件（FilterSidebar.set_facets）。

只测第 1 步会漏掉 UI 成本，只测第 2 步会漏掉 SQL 成本。二者都量，取较大者
判定是否达标 —— 这才是用户真实感知到的"点下去多久有反应"。

无图片 IO：只造 DB 行。图片解码不在筛选路径上（取景区自己按需加载）。
"""
from __future__ import annotations

import argparse
import json
import os
import platform
from pathlib import Path
import sys
import tempfile
from time import perf_counter

# 无头跑 UI（FilterSidebar 需要 QApplication）；必须在导入 QtWidgets 之前设。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import qVersion

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.store import PhotoStore
from services.review_service import ReviewFilter, ReviewService
from ui.review.facets import FacetModel
from ui.review.filters import FilterSidebar


FILTERS = (
    ReviewFilter.AI_PICKS,
    ReviewFilter.UNCERTAIN,
    ReviewFilter.REJECTED,
    ReviewFilter.TECHNICAL_ISSUE,
    ReviewFilter.STAR,
)

# 三分面组合：从这些面里任取 3 个 + 每个面取 1 个值，覆盖"相机×焦距×星级"
# 这类摄影师最常做的组合。值都是**真实存在的桶值/去重值**（由数据生成决定）。
FACET_TRIOS: tuple[tuple[str, str, str], ...] = (
    ("camera_model", "focal_length", "star"),
    ("camera_model", "aperture", "iso"),
    ("lens_model", "focal_length", "star"),
    ("camera_model", "lens_model", "iso"),
)

CAMERAS = ("ILCE-7M4", "EOS-R5", "X-T5")
LENSES = ("FE 24-70mm F2.8 GM", "RF 70-200mm F2.8L", "XF 16-55mm F2.8")
FOCALS = (24.0, 50.0, 200.0)
APERTURES = (1.8, 2.8, 5.6)
ISOS = (100, 800, 3200)


def _seed_rows(items: int) -> list[dict]:
    """造 1,000 行，字段互相组合，保证每个分面都有值可用。"""
    rows: list[dict] = []
    for index in range(items):
        rows.append(
            {
                "path": f"/benchmark/{index:06d}.jpg",
                "fname": f"{index:06d}.jpg",
                "asset_pair_id": f"asset-{index:06d}",
                "asset_role": "jpeg",
                "is_best": int(index % 5 == 0),
                "is_uncertain": int(index % 7 == 0),
                "is_waste": int(index % 11 == 0),
                "blur_score": 10.0 if index % 13 == 0 else 80.0,
                "star": 5 if index % 5 == 0 else (4 if index % 3 == 0 else 0),
                "camera_model": CAMERAS[index % len(CAMERAS)],
                "lens_model": LENSES[index % len(LENSES)],
                "focal_length": FOCALS[index % len(FOCALS)],
                "aperture": APERTURES[index % len(APERTURES)],
                "iso": ISOS[index % len(ISOS)],
            }
        )
    return rows


def _facet_value_for(model: FacetModel, facet_key: str) -> object | None:
    """取该分面的第一个可选值的 raw_value（没值就跳过这一组）。"""
    values = model.values(facet_key)
    if not values:
        return None
    return values[0].raw_value


def _time_facet_combo(model: FacetModel, bar: FilterSidebar, keys: tuple[str, ...]) -> float:
    """量"依次点中这 3 个面各一个值"的总耗时（含重算计数 + 重绘控件）。

    只在无 Qt 事件的同步路径上量，包含：
        * FacetModel.rebuild（每面每值一条 COUNT 查询）
        * FilterSidebar.set_facets（重建 chip 控件）

    【为什么每次先 clear_all】各组合共用同一个 model；若不彻底复位，上一组
    残留的选中会让下一组的初始状态不同（结果既不可比，还可能因"上一组把某面
    的值全筛没了"而取不到值 → 返回 -1）。每组都从干净状态起算才公平。
    """
    model.clear_all()
    # 先取好每个面要点的值（干净状态下取，避免被前面的选中影响）。
    picks: list[tuple[str, object]] = []
    for key in keys:
        raw = _facet_value_for(model, key)
        if raw is None:
            model.clear_all()
            return -1.0
        picks.append((key, raw))

    start = perf_counter()
    for key, raw in picks:
        model.toggle(key, raw)
    bar.set_facets(model.facets_snapshot())
    elapsed = perf_counter() - start
    model.clear_all()
    bar.set_facets(model.facets_snapshot())
    return elapsed


def run_benchmark(items: int) -> dict:
    if items < 1:
        raise ValueError("items must be positive")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    with tempfile.TemporaryDirectory(prefix="lumina-review-benchmark-") as directory:
        store = PhotoStore(str(Path(directory) / "review.db"), enable_wal=False)
        try:
            store.upsert_photos_batch(_seed_rows(items))
            start = perf_counter()
            service = ReviewService(store)
            load_seconds = perf_counter() - start

            filters: dict[str, float] = {}
            for name in FILTERS:
                start = perf_counter()
                service.set_filter(name)
                filters[name.value] = perf_counter() - start

            # ---- 分面部分 ----
            start = perf_counter()
            model = FacetModel(store)
            facet_initial_seconds = perf_counter() - start

            bar = FilterSidebar()
            bar.set_facets(model.facets_snapshot())

            facet_combos: dict[str, float] = {}
            for trio in FACET_TRIOS:
                label = "+".join(trio)
                facet_combos[label] = _time_facet_combo(model, bar, trio)

            # 也量"每次只点一个面"的单击响应（最常见的交互）
            single: dict[str, float] = {}
            for key in ("camera_model", "focal_length", "star"):
                raw = _facet_value_for(model, key)
                if raw is None:
                    continue
                start = perf_counter()
                model.toggle(key, raw)
                bar.set_facets(model.facets_snapshot())
                single[key] = perf_counter() - start
                model.clear(key)
                bar.set_facets(model.facets_snapshot())
        finally:
            store.close()

    valid_combos = {k: v for k, v in facet_combos.items() if v >= 0}
    result = {
        "items": items,
        "loaded_items": len(service.items),
        "machine": platform.platform(),
        "python": platform.python_version(),
        "qt": qVersion(),
        "load_seconds": load_seconds,
        "filters_seconds": filters,
        "facet_initial_seconds": facet_initial_seconds,
        "facet_single_seconds": single,
        "facet_combo_seconds": facet_combos,
        "thresholds_seconds": {"load": 3.0, "filter": 0.3, "facet_combo": 0.3},
        "passed": (
            len(service.items) == items
            and load_seconds < 3.0
            and all(elapsed < 0.3 for elapsed in filters.values())
            and all(elapsed < 0.3 for elapsed in single.values())
            and bool(valid_combos)
            and all(elapsed < 0.3 for elapsed in valid_combos.values())
        ),
    }
    app.processEvents()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--items", type=int, default=1000)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)
    result = run_benchmark(args.items)
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.json:
        args.json.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
