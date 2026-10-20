# -*- coding: utf-8 -*-
"""T-08 验收测试：分面筛选（query_builder / facets / 筛选条 / 审片集成）。

【本阶段 ROI 最高的三项里最优先的一项】因此测试覆盖三个层次：

1. **安全**：条件树编译出的 SQL 里，用户值**永远**是 ``?`` 占位符。这条
   不能只靠"看一眼代码觉得没问题"——用真实注入串跑一遍，断言它没被拼进 SQL。
2. **语义**：分面计数的口径（排除自身面）、多选、作用域、桶区间。这些是
   "数字对不对"的问题，错了会让摄影师看到 0 张而不敢选。
3. **集成**：点分面值 → 审片队列真的收窄；点常用口径 → 分面清空。

原则上只断言**当前这次运行的输出**（G2 门禁：不复用历史跑分）。
"""
from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from engine.store import PhotoStore
from services.query_builder import (
    FIELD_COLUMNS,
    QueryError,
    compile_query,
    compile_scope,
    describe_query,
    resolve_order,
)
from services.review_service import ReviewFilter
from ui.review.facets import (
    APERTURE_BUCKETS,
    DEFAULT_FACETS,
    FOCAL_BUCKETS,
    ISO_BUCKETS,
    FacetDef,
    FacetModel,
)


# ---------------------------------------------------------------------------
# 数据构造
# ---------------------------------------------------------------------------

CAMERAS = ("ILCE-7M4", "EOS-R5", "X-T5")
LENSES = ("FE 24-70mm F2.8 GM", "RF 70-200mm F2.8L", "XF 16-55mm F2.8")
FOCALS = (24.0, 50.0, 200.0)
APERTURES = (1.8, 2.8, 5.6)
ISOS = (100, 800, 3200)


@pytest.fixture()
def store(tmp_db_path):
    s = PhotoStore(tmp_db_path, enable_wal=False)
    yield s
    s.close()


def _seed(store: PhotoStore, n: int = 60) -> None:
    """n 行，各分面值均匀分布，方便断言精确计数。"""
    rows = []
    for index in range(n):
        rows.append(
            {
                "path": f"/p/{index:04d}.jpg",
                "fname": f"{index:04d}.jpg",
                "ts": 1_700_000_000 + index,
                "camera_model": CAMERAS[index % 3],
                "lens_model": LENSES[index % 3],
                "focal_length": FOCALS[index % 3],
                "aperture": APERTURES[index % 3],
                "iso": ISOS[index % 3],
                "star": (5, 4, 0)[index % 3],
                "label": (None, "P", "X")[index % 3],
                "scene": ("portrait", "landscape", None)[index % 3],
                "comp_score": float(index),
                "blur_score": 80.0,
            }
        )
    store.upsert_photos_batch(rows)


def _q(app=None):
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


# ===========================================================================
# 1. query_builder —— 安全与语义
# ===========================================================================


class TestQueryBuilderSafety:
    """用户值必须走参数绑定，绝不拼进 SQL 文本。"""

    INJECTIONS = (
        "%' OR 1=1 --%",
        "'; DROP TABLE photos; --",
        '" OR ""="',
        "1; DELETE FROM photos WHERE 1=1",
        "\\'; SELECT * FROM sqlite_master; --",
    )

    @pytest.mark.parametrize("payload", INJECTIONS)
    def test_eq_value_is_never_concatenated(self, payload):
        query = compile_query({"field": "camera_model", "op": "eq", "value": payload})
        # SQL 里只有占位符，没有 payload 的任何片段
        assert "?" in query.where_sql
        assert payload not in query.where_sql
        assert payload not in " ".join(str(a) for a in query.args[:0])  # 不曾在 SQL 里
        # payload 作为参数原样绑定（转义交给驱动）
        assert payload in query.args

    @pytest.mark.parametrize("payload", INJECTIONS)
    def test_contains_escapes_like_wildcards(self, payload):
        query = compile_query({"field": "fname", "op": "contains", "value": payload})
        assert "?" in query.where_sql
        assert payload not in query.where_sql
        assert "ESCAPE" in query.where_sql.upper()

    def test_unknown_field_is_rejected_not_passed_through(self):
        with pytest.raises(QueryError):
            compile_query({"field": "comp_score; DROP TABLE photos", "op": "eq", "value": 1})

    def test_unknown_operator_is_rejected(self):
        with pytest.raises(QueryError):
            compile_query({"field": "star", "op": "eval", "value": 1})

    def test_injection_through_in_list_is_bound(self):
        payload = "x') OR 1=1 --"
        query = compile_query({"field": "camera_model", "op": "in", "value": [payload, "EOS-R5"]})
        assert payload not in query.where_sql
        assert query.args == (payload, "EOS-R5")

    def test_injection_actually_fails_against_real_db(self, store):
        """端到端：注入串查不出任何东西（若被拼接，1=1 会返回全表）。"""
        _seed(store, 12)
        for payload in self.INJECTIONS:
            query = compile_query({"field": "camera_model", "op": "eq", "value": payload})
            rows = store.query_photos_where(query.where_sql, query.args)
            assert rows == [], f"注入串 {payload!r} 竟然命中了 {len(rows)} 行"


class TestQueryBuilderSemantics:
    def test_and_or_not_compile(self):
        tree = {
            "and": [
                {"field": "star", "op": "gte", "value": 4},
                {"or": [
                    {"field": "camera_model", "op": "eq", "value": "EOS-R5"},
                    {"field": "camera_model", "op": "eq", "value": "X-T5"},
                ]},
            ]
        }
        query = compile_query(tree)
        assert "AND" in query.where_sql.upper()
        assert "OR" in query.where_sql.upper()
        assert query.args == (4, "EOS-R5", "X-T5")

    def test_empty_and_is_unconditional(self):
        assert compile_query({"and": []}).where_sql == ""

    def test_empty_or_is_always_false(self):
        assert compile_query({"or": []}).where_sql == "1 = 0"

    def test_empty_not_is_rejected(self):
        with pytest.raises(QueryError):
            compile_query({"not": {}})

    def test_in_with_empty_list_is_always_false(self):
        assert compile_query({"field": "star", "op": "in", "value": []}).where_sql == "1 = 0"

    def test_between_autocorrects_reversed_bounds(self):
        query = compile_query({"field": "iso", "op": "between", "value": [6400, 100]})
        assert query.args == (100, 6400)

    def test_isnull_and_notnull(self):
        assert "IS NULL" in compile_query({"field": "label", "op": "isnull", "value": None}).where_sql.upper()
        assert "IS NOT NULL" in compile_query({"field": "label", "op": "notnull", "value": None}).where_sql.upper()

    def test_scope_collection_and_person(self):
        col = compile_scope({"kind": "collection", "paths": ["/a.jpg", "/b.jpg"]})
        assert col.where_sql.count("?") == 2
        assert col.args == ("/a.jpg", "/b.jpg")
        per = compile_scope({"kind": "person", "person_id": 7})
        assert per.args == (7,)

    def test_scope_empty_collection_is_false(self):
        assert compile_scope({"kind": "collection", "paths": []}).where_sql == "1 = 0"

    def test_scope_all_is_unconditional(self):
        assert compile_scope(None).where_sql == ""
        assert compile_scope({"kind": "all"}).where_sql == ""

    def test_unknown_scope_kind_rejected(self):
        with pytest.raises(QueryError):
            compile_scope({"kind": "magic"})

    def test_order_whitelist_falls_back(self):
        resolved = resolve_order("star")
        assert resolved.startswith("star")  # 白名单内的字段排在最前
        # 白名单外回落默认，不抛错（排序不是安全边界）
        assert resolve_order("comp_score; DROP TABLE x") == "comp_score DESC"

    def test_describe_is_human_readable(self):
        text = describe_query({"field": "camera_model", "op": "eq", "value": "EOS-R5"})
        assert "相机" in text and "EOS-R5" in text

    def test_all_default_facet_fields_are_whitelisted(self):
        for facet in DEFAULT_FACETS:
            assert facet.field in FIELD_COLUMNS, f"{facet.field} 不在白名单里"


# ===========================================================================
# 2. facets —— 计数语义
# ===========================================================================


class TestFacetCounting:
    def test_initial_counts_equal_total(self, store):
        _seed(store, 60)
        model = FacetModel(store)
        assert model.count() == 60
        # 相机三值各 20
        counts = {v.raw_value: v.count for v in model.values("camera_model")}
        assert counts == {"ILCE-7M4": 20, "EOS-R5": 20, "X-T5": 20}

    def test_self_facet_counts_do_not_narrow(self, store):
        """选中一个相机值后，**相机面自己**的计数仍是全部（可继续多选）。"""
        _seed(store, 60)
        model = FacetModel(store)
        model.toggle("camera_model", "ILCE-7M4")
        assert model.count() == 20
        counts = {v.raw_value: v.count for v in model.values("camera_model")}
        assert counts == {"ILCE-7M4": 20, "EOS-R5": 20, "X-T5": 20}

    def test_other_facets_are_constrained(self, store):
        """选中相机后，**其它面**的计数应受该相机约束。"""
        _seed(store, 60)
        model = FacetModel(store)
        model.toggle("camera_model", "ILCE-7M4")
        focal_counts = {v.raw_value: v.count for v in model.values("focal_length")}
        # ILCE-7M4 的 20 张里，焦距三档各多少
        assert sum(focal_counts.values()) == 20

    def test_multi_select_same_facet_is_or(self, store):
        _seed(store, 60)
        model = FacetModel(store)
        model.toggle("camera_model", "ILCE-7M4")
        model.toggle("camera_model", "EOS-R5")
        assert model.count() == 40

    def test_different_facets_are_and(self, store):
        _seed(store, 60)
        model = FacetModel(store)
        model.toggle("camera_model", "ILCE-7M4")
        model.toggle("focal_length", "广角 ≤35mm")
        # 只有 index%3==0 的行同时满足相机 idx0 与焦距 idx0
        assert model.count() == 20

    def test_single_choice_facet_replaces(self, store):
        _seed(store, 60)
        model = FacetModel(store)
        star = next(f for f in DEFAULT_FACETS if f.key == "star")
        assert star.multi is False
        model.toggle("star", 5)
        model.toggle("star", 4)
        assert model.count() == 20  # 只有 4 星
        assert [v.raw_value for v in model._selected["star"]] == [4]

    def test_toggle_off_restores(self, store):
        _seed(store, 60)
        model = FacetModel(store)
        model.toggle("camera_model", "ILCE-7M4")
        model.toggle("camera_model", "ILCE-7M4")
        assert model.count() == 60

    def test_bucket_range_is_used_not_label(self, store):
        """**回归**：桶分面必须按区间筛，而不是把显示名当等值。"""
        _seed(store, 60)
        model = FacetModel(store)
        model.toggle("focal_length", "长焦 >135mm")
        # 200.0mm 的行 = index%3==2 → 20 张
        assert model.count() == 20

    def test_bucket_counts_are_within_bucket(self, store):
        _seed(store, 60)
        model = FacetModel(store)
        counts = {v.raw_value: v.count for v in model.values("focal_length")}
        assert counts["广角 ≤35mm"] == 20
        assert counts["标准 35–70mm"] == 20
        assert counts["长焦 >135mm"] == 20
        # 中长 70–135mm 在我们的数据里没有值 → 0
        assert counts["中长 70–135mm"] == 0

    def test_scope_narrows_counts(self, store):
        _seed(store, 60)
        model = FacetModel(store)
        model.set_scope({"kind": "collection", "paths": ["/p/0000.jpg", "/p/0003.jpg"]})
        assert model.count() == 2
        assert model.scope_label() == "集合「未命名」"

    def test_scope_label_variants(self, store):
        model = FacetModel(store)
        assert model.scope_label() == "全部照片"
        model.set_scope({"kind": "collection", "name": "婚礼精选"})
        assert model.scope_label() == "集合「婚礼精选」"
        model.set_scope({"kind": "person", "name": "新娘"})
        assert model.scope_label() == "人物「新娘」"

    def test_clear_removes_filters(self, store):
        _seed(store, 60)
        model = FacetModel(store)
        model.toggle("camera_model", "ILCE-7M4")
        model.toggle("focal_length", "长焦 >135mm")
        assert model.has_filters
        model.clear_all()
        assert not model.has_filters
        assert model.count() == 60

    def test_hidden_selected_value_stays_visible(self, store):
        """已选中但不在结果里的值要补回列表 —— 否则用户取消不掉。"""
        _seed(store, 60)
        model = FacetModel(store)
        # 制造一个"选中后该面其它值都归零"的情形
        model.toggle("camera_model", "ILCE-7M4")
        model.select("lens_model", "XF 16-55mm F2.8")
        # 若某些值被挤掉，至少选中的那个必须还在
        raw_values = {v.raw_value for v in model.values("lens_model")}
        assert "XF 16-55mm F2.8" in raw_values
        selected = [v for v in model.values("lens_model") if v.selected]
        assert selected and selected[0].raw_value == "XF 16-55mm F2.8"

    def test_unknown_facet_raises(self, store):
        model = FacetModel(store)
        with pytest.raises(QueryError):
            model.toggle("no_such_facet", "x")

    def test_snapshot_and_to_dict_are_serializable(self, store):
        import json

        _seed(store, 12)
        model = FacetModel(store)
        model.toggle("camera_model", "ILCE-7M4")
        payload = json.dumps(model.to_dict(), ensure_ascii=False)
        assert "facets" in payload and "scope" in payload

    def test_facets_with_missing_column_degrade_to_empty(self, store):
        """person_id 尚未建表时分面留空，而不是崩掉整个筛选条。"""
        _seed(store, 12)
        model = FacetModel(store, facets=[FacetDef("person_id", "人物", "person_id")])
        # 不抛异常；列存在与否都接受（本库 photos 表有 person_id 但全 NULL）
        assert isinstance(model.values("person_id"), list)

    def test_query_returns_scoped_rows(self, store):
        _seed(store, 60)
        model = FacetModel(store)
        model.toggle("camera_model", "EOS-R5")
        rows = model.query(order_by="path")
        assert len(rows) == 20
        assert all(r["camera_model"] == "EOS-R5" for r in rows)


# ===========================================================================
# 3. 筛选条（UI）
# ===========================================================================


class TestFilterBar:
    def _bar(self, store):
        _q()
        _seed(store, 60)
        from ui.review.filters import FilterSidebar

        model = FacetModel(store)
        bar = FilterSidebar()
        bar.set_facets(model.facets_snapshot())
        return model, bar

    def test_all_presets_have_buttons(self, store):
        from services.review_service import ReviewFilter as RF
        from ui.review.filters import LABELS

        _, bar = self._bar(store)
        assert set(bar.buttons) == set(LABELS)
        assert set(bar.buttons) == set(RF)

    def test_default_preset_is_all(self, store):
        _, bar = self._bar(store)
        assert bar.active_preset() == ReviewFilter.ALL

    def test_preset_click_emits_filter_selected(self, store):
        _, bar = self._bar(store)
        got = []
        bar.filter_selected.connect(got.append)
        bar.buttons[ReviewFilter.UNCERTAIN].click()
        assert got == [ReviewFilter.UNCERTAIN]
        assert bar.active_preset() == ReviewFilter.UNCERTAIN

    def test_counts_rendered_on_presets(self, store):
        _, bar = self._bar(store)
        bar.set_counts({f: idx for idx, f in enumerate(ReviewFilter)})
        assert bar.buttons[ReviewFilter.ALL].text().endswith(" 0")
        assert bar.buttons[ReviewFilter.STAR].text().endswith(" 5")

    def test_facet_chip_click_emits_and_resets_preset(self, store):
        _, bar = self._bar(store)
        bar.buttons[ReviewFilter.AI_PICKS].click()  # 先偏离默认
        assert bar.active_preset() == ReviewFilter.AI_PICKS
        got = []
        bar.facet_toggled.connect(lambda k, v: got.append((k, v)))
        chip = bar.chip("camera_model", "ILCE-7M4")
        assert chip is not None
        chip.click()
        assert got == [("camera_model", "ILCE-7M4")]
        # 点分面后口径自动复位（避免隐式叠加）
        assert bar.active_preset() == ReviewFilter.ALL

    def test_zero_count_chip_disabled(self, store):
        _, bar = self._bar(store)
        # "中长 70–135mm" 在此数据下为 0
        chip = bar.chip("focal_length", "中长 70–135mm")
        assert chip is not None
        assert chip.isEnabled() is False

    def test_selected_zero_count_chip_stays_enabled(self, store):
        _q()
        _seed(store, 60)
        from ui.review.filters import FilterSidebar

        model = FacetModel(store)
        model.toggle("focal_length", "中长 70–135mm")
        bar = FilterSidebar()
        bar.set_facets(model.facets_snapshot())
        chip = bar.chip("focal_length", "中长 70–135mm")
        assert chip.isChecked() is True
        assert chip.isEnabled() is True  # 必须能取消

    def test_clear_button_emits(self, store):
        _, bar = self._bar(store)
        got = []
        bar.facet_cleared.connect(lambda: got.append(True))
        bar.clear_button.click()
        assert got == [True]

    def test_summary_label_text(self, store):
        model, bar = self._bar(store)
        bar.set_summary(model.count(), has_filters=False)
        assert bar.summary_label.text() == "共 60"
        model.toggle("camera_model", "ILCE-7M4")
        bar.set_summary(model.count(), has_filters=True)
        assert "筛出 20" in bar.summary_label.text()

    def test_scope_label_text(self, store):
        _, bar = self._bar(store)
        bar.set_scope_label("全部照片")
        assert bar.scope_label.text() == "在：全部照片"
        bar.set_scope_label("集合「婚礼」")
        assert bar.scope_label.text() == "在：集合「婚礼」"

    def test_facets_hidden_by_default_but_toggleable(self, store):
        _, bar = self._bar(store)
        assert bar.facet_host.isVisibleTo(bar) is False
        bar.toggle_button.click()
        assert bar.facet_host.isVisibleTo(bar) is True

    def test_refresh_keeps_selection_reflected(self, store):
        model, bar = self._bar(store)
        model.toggle("camera_model", "ILCE-7M4")
        bar.set_facets(model.facets_snapshot())
        chip = bar.chip("camera_model", "ILCE-7M4")
        assert chip.isChecked() is True

    def test_facet_groups_built_for_all_default_facets(self, store):
        _, bar = self._bar(store)
        assert "camera_model" in bar.groups
        assert "focal_length" in bar.groups
        assert "star" in bar.groups


# ===========================================================================
# 4. 审片页面集成
# ===========================================================================


class TestReviewPageIntegration:
    def _page(self, store):
        _q()
        _seed(store, 60)
        from ui.pages.review_page import ReviewPage

        return ReviewPage(store)

    def test_queue_narrows_when_facet_selected(self, store):
        page = self._page(store)
        assert len(page.review.queue) == 60
        page._facet_toggled("camera_model", "ILCE-7M4")
        assert len(page.review.queue) == 20
        assert all(
            item.asset_id.endswith(".jpg") for item in page.review.queue
        )

    def test_three_facet_combo_narrows_queue(self, store):
        page = self._page(store)
        page._facet_toggled("camera_model", "ILCE-7M4")
        page._facet_toggled("focal_length", "广角 ≤35mm")
        page._facet_toggled("star", 5)
        assert len(page.review.queue) == 20  # 同一批 20 张
        assert page.facets.count() == 20

    def test_preset_click_clears_facets(self, store):
        page = self._page(store)
        page._facet_toggled("camera_model", "ILCE-7M4")
        assert page.facets.has_filters
        page._filter_selected(ReviewFilter.AI_PICKS)
        assert not page.facets.has_filters

    def test_facet_clear_restores_full_queue(self, store):
        page = self._page(store)
        page._facet_toggled("camera_model", "ILCE-7M4")
        assert len(page.review.queue) == 20
        page._facet_cleared()
        assert len(page.review.queue) == 60

    def test_scope_can_be_set_externally(self, store):
        page = self._page(store)
        page.set_facet_scope({"kind": "collection", "name": "子集", "paths": ["/p/0000.jpg"]})
        assert page.facets.count() == 1
        assert "集合" in page.sidebar.scope_label.text()

    def test_sidebar_shows_facets_after_refresh(self, store):
        page = self._page(store)
        assert "camera_model" in page.sidebar.groups
        assert page.sidebar.summary_label.text() != ""


# ===========================================================================
# 5. 性能门禁（当前运行实测，不复用历史跑分）
# ===========================================================================


class TestFacetPerformance:
    def test_three_facet_combination_under_300ms(self, store):
        _q()
        _seed(store, 1000)
        model = FacetModel(store)
        from ui.review.filters import FilterSidebar

        bar = FilterSidebar()
        start = time.perf_counter()
        model.toggle("camera_model", "ILCE-7M4")
        model.toggle("focal_length", "广角 ≤35mm")
        model.toggle("star", 5)
        bar.set_facets(model.facets_snapshot())
        elapsed = time.perf_counter() - start
        assert elapsed < 0.3, f"三分面组合耗时 {elapsed:.3f}s ≥ 0.3s"

    def test_single_facet_toggle_under_300ms(self, store):
        _q()
        _seed(store, 1000)
        model = FacetModel(store)
        start = time.perf_counter()
        model.toggle("camera_model", "ILCE-7M4")
        elapsed = time.perf_counter() - start
        assert elapsed < 0.3, f"单分面耗时 {elapsed:.3f}s ≥ 0.3s"
