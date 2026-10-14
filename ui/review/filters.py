# -*- coding: utf-8 -*-
"""filters.py —— 分面筛选条（T-08 / M1-3）

这一层只做「画面 + 发意图」，**不碰 SQL、不碰 DB、不算计数**：

    * 有哪些面、每面有哪些值、各值多少张 —— 来自 ``ui/review/facets.py``；
    * 点一下筛选项 —— 通过信号把 (面, 值) 抛给页面，页面再转交 FacetModel；
    * 结论（筛后剩几张）通过 ``set_summary()`` 贴回条上。

为什么不像旧版那样写死 6 个按钮
--------------------------------
旧版是「全部照片 / AI 精选 / 待确认 / 已排除 / 技术问题 / 星级」六个写死的
按钮。它的问题不是不好看，而是**不可组合**：摄影师真实的找片方式是
"5D4 + 长焦 + 4 星以上 + 没有技术问题"，六个互斥按钮表达不了这个。

新条两层，各司其职：

    ┌─ 常用口径（第 0 行）───────────────────────────────────┐
    │ 全部照片 · AI 精选 · 待确认 · 已排除 · 技术问题 · 4★+   │  ← 互斥，一键到位
    ├─ 分面（折叠区，默认收起）───────────────────────────────┤
    │ 相机 5D4(212)  R5(88)  …                               │  ← 可多选，实时计数
    │ 焦距 广角(31) 标准(402) 中长(180) 长焦(56)              │
    │ …                                                       │
    └─────────────────────────────────────────────────────────┘

常用口径是「一把捞」，分面是「慢慢收」；两者都保留，用户不用做取舍。
注意：**只有一方可以生效**。拍照师点了「AI 精选」之后再点相机分面，
说明他想在 AI 精选里再按相机收窄 —— 那属于「口径 ∩ 分面」。为避免语义
含糊，本组件在用户点分面时自动把常用口径复位到「全部照片」，并如实反映
在按钮选中态上（绝不出现"看起来两个都亮着、实际只生效一个"的欺骗 UI）。

无 Qt 的模型层（facets.py / query_builder.py）可以单独跑测试，所以这里的
逻辑越薄越好 —— 复杂判断都留在那边。
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from services.review_service import ReviewFilter

# ---------------------------------------------------------------------------
# 常用口径（老 6 个口径，一个一个都不能丢）
# ---------------------------------------------------------------------------

LABELS: dict[ReviewFilter, str] = {
    ReviewFilter.ALL: "全部照片",
    ReviewFilter.AI_PICKS: "AI 精选",
    ReviewFilter.UNCERTAIN: "待确认",
    ReviewFilter.REJECTED: "已排除",
    ReviewFilter.TECHNICAL_ISSUE: "技术问题",
    ReviewFilter.STAR: "高星",
}

# 口径按钮的短名（工具栏窄，显示名 + 计数会挤；短名给按钮，全名给 tooltip）
SHORT_LABELS: dict[ReviewFilter, str] = {
    ReviewFilter.ALL: "全部",
    ReviewFilter.AI_PICKS: "AI 精选",
    ReviewFilter.UNCERTAIN: "待确认",
    ReviewFilter.REJECTED: "已排除",
    ReviewFilter.TECHNICAL_ISSUE: "技术问题",
    ReviewFilter.STAR: "4★+",
}

_HINTS: dict[ReviewFilter, str] = {
    ReviewFilter.ALL: "显示库中全部照片",
    ReviewFilter.AI_PICKS: "AI 选中或已标记保留（P）的照片",
    ReviewFilter.UNCERTAIN: "AI 拿不准、需要你拍板的照片",
    ReviewFilter.REJECTED: "已排除（X）或被判定为废片的照片",
    ReviewFilter.TECHNICAL_ISSUE: "模糊、过曝或欠曝的照片",
    ReviewFilter.STAR: "星级达到门槛的照片（默认 4 星及以上）",
}

# 分面区默认是否展开。摄影师点进来第一眼要看到"能按相机/焦距找"，
# 但不想让一整墙的复选框把取景区挤到屏幕外。
_DEFAULT_FACETS_EXPANDED = False


class FacetChip(QPushButton):
    """一个可选值：``标签 计数``，选中态用 ``checkable`` 表达。

    ``count == 0`` 且未选中时置灰不可点 —— 点了也是空结果，不如直接告诉用户
    这条路走不通。但**已选中且计数为 0 的必须可点**（否则用户取消不掉自己
    刚选的东西，那是比"看到 0 张"更糟的体验，见 facets.py 的
    ``_merge_hidden_selection``）。
    """

    def __init__(
        self,
        facet_key: str,
        raw_value: Any,
        label: str,
        count: int,
        *,
        selected: bool = False,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.facet_key = facet_key
        self.raw_value = raw_value
        self.setObjectName("FacetChip")
        self.setCheckable(True)
        self.setChecked(bool(selected))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setText(f"{label} {count}")
        self.setToolTip(f"{label} · {count} 张")
        # 【关键】只有"没选中 且 计数为 0"才禁用。已选中的哪怕是 0 张，
        # 也必须让用户能点掉它 —— 否则筛选条就成了单向闸门。
        if count <= 0 and not selected:
            self.setEnabled(False)
            self.setToolTip(f"{label} · 当前条件下没有照片")

    def set_selected(self, selected: bool) -> None:
        """只改选中态，不触发 ``toggled``（避免刷新时递归）。"""
        blocked = self.blockSignals(True)
        self.setChecked(bool(selected))
        self.blockSignals(blocked)

    def set_value_label(self, label: str, count: int) -> None:
        self.setText(f"{label} {count}")
        self.setToolTip(f"{label} · {count} 张")


class FacetGroup(QWidget):
    """一个分面：标题（可点收起）+ 一排可选值。

    值用 ``QGridLayout`` 铺，每行 ``_PER_ROW`` 个 —— 相机型号名可能很长，
    均匀排布比单列更省纵向空间。
    """

    _PER_ROW = 2

    def __init__(self, key: str, label: str, parent=None) -> None:
        super().__init__(parent)
        self.key = key
        self.chips: list[FacetChip] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.header = QPushButton(label)
        self.header.setObjectName("FacetHeader")
        self.header.setCheckable(True)
        self.header.setChecked(True)
        self.header.setCursor(Qt.CursorShape.PointingHandCursor)
        self.header.toggled.connect(self._on_header_toggled)
        layout.addWidget(self.header)
        self.body = QWidget()
        self.grid = QGridLayout(self.body)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(4)
        self.grid.setVerticalSpacing(4)
        layout.addWidget(self.body)
        self._label = label

    def _on_header_toggled(self, expanded: bool) -> None:
        self.body.setVisible(expanded)
        self.header.setText(f"{'▾' if expanded else '▸'} {self._label}")

    def set_label(self, label: str) -> None:
        self._label = label
        self.header.setText(f"{'▾' if self.header.isChecked() else '▸'} {label}")

    def populate(self, values: list) -> None:
        """重建可选值（值不多、重建成本低，不做 diff 复用）。"""
        while self.grid.count():
            item = self.grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.chips = []
        for index, value in enumerate(values):
            chip = FacetChip(
                self.key, value.raw_value, value.label, value.count, selected=value.selected
            )
            self.chips.append(chip)
            self.grid.addWidget(chip, index // self._PER_ROW, index % self._PER_ROW)
        if not values:
            empty = QLabel("（无可选项）")
            empty.setObjectName("Muted")
            self.grid.addWidget(empty, 0, 0, 1, self._PER_ROW)

    def count(self) -> int:
        return len(self.chips)


class FilterSidebar(QWidget):
    """左侧筛选条：常用口径 + 分面。

    对外三个信号：

    * ``filter_selected(object)`` —— 兼容旧接口，抛 ``ReviewFilter``（常用口径按钮）。
    * ``facet_toggled(str, object)`` —— (分面 key, 原始值)，点某个分面值。
    * ``facet_cleared()`` —— 清空全部分面。

    ``set_counts()`` / ``set_facets()`` / ``set_summary()`` 都由页面调用，
    本组件自己不查库。
    """

    filter_selected = Signal(object)
    facet_toggled = Signal(str, object)
    facet_cleared = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("FilterSidebar")
        self.buttons: dict[ReviewFilter, QPushButton] = {}
        self.groups: dict[str, FacetGroup] = {}
        self._active_preset: ReviewFilter = ReviewFilter.ALL

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 10, 8, 10)
        root.setSpacing(8)

        title_row = QHBoxLayout()
        title = QLabel("筛选")
        title.setObjectName("SidebarTitle")
        self.summary_label = QLabel("")
        self.summary_label.setObjectName("Muted")
        self.summary_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        title_row.addWidget(title)
        title_row.addStretch(1)
        title_row.addWidget(self.summary_label)
        root.addLayout(title_row)

        self.scope_label = QLabel("在：全部照片")
        self.scope_label.setObjectName("ScopeLabel")
        self.scope_label.setWordWrap(True)
        root.addWidget(self.scope_label)

        root.addWidget(self._section_title("常用口径"))
        preset_grid = QGridLayout()
        preset_grid.setContentsMargins(0, 0, 0, 0)
        preset_grid.setHorizontalSpacing(4)
        preset_grid.setVerticalSpacing(4)
        for index, (filter_name, label) in enumerate(LABELS.items()):
            button = QPushButton(label)
            button.setObjectName(f"filter-{filter_name.value}")
            button.setCheckable(True)
            button.setChecked(filter_name == ReviewFilter.ALL)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setToolTip(_HINTS[filter_name])
            button.clicked.connect(
                lambda _checked=False, selected=filter_name: self._on_preset_clicked(selected)
            )
            self.buttons[filter_name] = button
            preset_grid.addWidget(button, index // 2, index % 2)
        root.addLayout(preset_grid)

        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setObjectName("Divider")
        root.addWidget(divider)

        facet_header = QHBoxLayout()
        facet_header.addWidget(self._section_title("分面"))
        facet_header.addStretch(1)
        self.toggle_button = QPushButton("展开")
        self.toggle_button.setObjectName("LinkButton")
        self.toggle_button.setCheckable(True)
        self.toggle_button.setChecked(_DEFAULT_FACETS_EXPANDED)
        self.toggle_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggle_button.clicked.connect(self._on_toggle_facets)
        facet_header.addWidget(self.toggle_button)
        self.clear_button = QPushButton("清空")
        self.clear_button.setObjectName("LinkButton")
        self.clear_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_button.setToolTip("清空所有分面选择")
        self.clear_button.clicked.connect(self._on_clear_facets)
        facet_header.addWidget(self.clear_button)
        root.addLayout(facet_header)

        self.facet_host = QWidget()
        # 【为什么第一版用普通 QWidget 而不是 QScrollArea】
        # QScrollArea 里的子控件在 `isVisible()` 上会返回 False（未 show 的
        # 窗口下更是如此），让"分面是否展开"很难单测。这里用一个普通容器，
        # 由外层 splitter 控制高度；等真出现"面太多放不下"再换滚动。
        self.facet_layout = QVBoxLayout(self.facet_host)
        self.facet_layout.setContentsMargins(0, 0, 0, 0)
        self.facet_layout.setSpacing(6)
        self.facet_host.setVisible(_DEFAULT_FACETS_EXPANDED)
        root.addWidget(self.facet_host)

        self.empty_hint = QLabel("")
        self.empty_hint.setObjectName("Muted")
        self.empty_hint.setWordWrap(True)
        root.addWidget(self.empty_hint)
        root.addStretch(1)

        self.setMinimumWidth(180)
        self.setMaximumWidth(280)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)

    @staticmethod
    def _section_title(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("SectionTitle")
        return label

    # ------------------------------------------------------------------
    # 常用口径
    # ------------------------------------------------------------------
    def _on_preset_clicked(self, selected: ReviewFilter) -> None:
        """点常用口径：互斥选中，并把分面清空（口径与分面不叠加，见模块 docstring）。"""
        self._sync_preset_buttons(selected)
        self.filter_selected.emit(selected)

    def _sync_preset_buttons(self, active: ReviewFilter) -> None:
        self._active_preset = active
        for filter_name, button in self.buttons.items():
            blocked = button.blockSignals(True)
            button.setChecked(filter_name == active)
            button.blockSignals(blocked)

    def set_counts(self, counts: Mapping[ReviewFilter, int]) -> None:
        """更新常用口径按钮上的计数（兼容旧接口）。"""
        for filter_name, button in self.buttons.items():
            short = SHORT_LABELS[filter_name]
            count = counts.get(filter_name, 0)
            button.setText(f"{short} {count}")
            button.setToolTip(f"{_HINTS[filter_name]} · {count} 张")

    # ------------------------------------------------------------------
    # 分面
    # ------------------------------------------------------------------
    def set_facets(self, facets: list) -> None:
        """渲染分面快照（``FacetModel.facets_snapshot()`` 的返回值）。

        面集合一般固定，值会变；因此面只在第一次建，之后只更新值。
        """
        seen: set[str] = set()
        for facet in facets:
            seen.add(facet.key)
            group = self.groups.get(facet.key)
            if group is None:
                group = FacetGroup(facet.key, facet.label)
                group.header.toggled.connect(
                    lambda _checked, g=group: self._on_facet_group_visibility(g)
                )
                self.groups[facet.key] = group
                self.facet_layout.addWidget(group)
                group.setVisible(self.facet_host.isVisible())
            for chip in group.chips:
                chip.clicked.disconnect()
            group.set_label(facet.label)
            group.populate(facet.values)
            for chip in group.chips:
                chip.clicked.connect(
                    lambda _checked=False, k=chip.facet_key, v=chip.raw_value: self._on_facet_chip(k, v)
                )
        # 模型里已经没有的面（例如 person_id 尚未建表）就别留在条上装样子
        for key in list(self.groups):
            if key not in seen:
                group = self.groups.pop(key)
                self.facet_layout.removeWidget(group)
                group.deleteLater()
        total = sum(group.count() for group in self.groups.values())
        self._set_facet_visibility(self.toggle_button.isChecked(), total)

    def _on_facet_chip(self, key: str, raw_value: Any) -> None:
        """点分面值：自动把常用口径复位到「全部照片」，如实反映生效状态。"""
        if self._active_preset != ReviewFilter.ALL:
            self._sync_preset_buttons(ReviewFilter.ALL)
            self.filter_selected.emit(ReviewFilter.ALL)
        self.facet_toggled.emit(key, raw_value)

    def _on_clear_facets(self) -> None:
        self.facet_cleared.emit()

    def _on_toggle_facets(self) -> None:
        total = sum(group.count() for group in self.groups.values())
        self._set_facet_visibility(self.toggle_button.isChecked(), total)

    def _set_facet_visibility(self, expanded: bool, total: int) -> None:
        self.toggle_button.setText(
            ("收起" if expanded else "展开") + (f" {total}" if total else "")
        )
        self.facet_host.setVisible(bool(expanded) and total > 0)
        if total == 0:
            self.empty_hint.setText("当前没有可分面的信息（这批照片可能还没有 EXIF/评级数据）")
        else:
            self.empty_hint.setText("")

    def _on_facet_group_visibility(self, group: FacetGroup) -> None:
        group.body.setVisible(group.header.isChecked())

    # ------------------------------------------------------------------
    # 摘要 / 作用域
    # ------------------------------------------------------------------
    def set_summary(self, total: int, *, has_filters: bool) -> None:
        """右上角"筛后剩几张"，让用户不用盯取景区数人头。"""
        if has_filters:
            self.summary_label.setText(f"筛出 {total}")
        else:
            self.summary_label.setText(f"共 {total}")

    def set_scope_label(self, text: str) -> None:
        self.scope_label.setText(f"在：{text}")

    def set_star_threshold(self, threshold: int) -> None:
        """星级口径的按钮文案随门槛变（默认 4★，可被调用方改）。"""
        button = self.buttons.get(ReviewFilter.STAR)
        if button is not None:
            SHORT_LABELS[ReviewFilter.STAR] = f"{threshold}★+"
            self.set_counts({ReviewFilter.STAR: 0} if not button.text() else {})

    # ------------------------------------------------------------------
    # 测试便利：只读快照
    # ------------------------------------------------------------------
    def active_preset(self) -> ReviewFilter:
        return self._active_preset

    def chip(self, key: str, raw_value: Any) -> FacetChip | None:
        group = self.groups.get(key)
        if group is None:
            return None
        for chip in group.chips:
            if chip.raw_value == raw_value:
                return chip
        return None


__all__ = ["FilterSidebar", "FacetChip", "FacetGroup", "LABELS", "SHORT_LABELS"]
