# -*- coding: utf-8 -*-
"""facets.py —— 分面筛选的**模型层**（无 Qt 依赖，可单测）

【T-08 / M1-3】分面筛选条要显示"每类筛选项各有几张"。把"有哪些面、每面有哪些值、
各值多少张、哪些被选中"收敛成这一层，好处是：

    * **可单测**：不装 Qt 就能验证计数与会话逻辑；
    * **UI 只负责画面**：筛选条拿到的是"面 + 值 + 计数 + 选中态"，不再自己
      拼 SQL、也不自己算数；
    * 未来 Agent 想"按分面收窄"时，依赖的也是这一层，而不是某个 QWidget。

「实时计数」的口径（关键设计）
------------------------------
每个面值的计数是：**在"其它面 + 作用域"的约束下**，该值有多少张。
这叫「faceted search」的标准做法 —— 如果计数只反映当前全部条件（含自己这一面
已选的值），用户会看到"选了 ILCE-7M4 之后，X-T5 显示 0 张"，从而无法多点选值。
排除自身面既符合直觉，也让"再加一个值"的预览有个正确数字。

红线遵守：
    * 无 Qt、无 SQL：SQL 由 `services/query_builder.py` 编译、`engine/store.py` 执行。
    * A6：不触碰 ``METRIC_FIELDS`` / ``_ai_model_signature()``。
"""
from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from engine.store import PhotoStore
from services.query_builder import FIELD_COLUMNS, QueryError, compile_query, compile_scope

# ---------------------------------------------------------------------------
# 分面定义
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FacetDef:
    """一个分面的静态定义。

    ``multi``：是否允许同面多选（相机/镜头/星级可多选；日期区间不是多选而是区间）。
    ``buckets``：可选值的"桶"定义（用于连续量：焦距/光圈/ISO）。
    """

    key: str
    label: str
    field: str
    multi: bool = True
    buckets: tuple[tuple[str, Any], ...] = ()
    """(显示名, 条件值) —— 条件值是编译用的 mini 条件树或标量。空表示用原始去重值。"""


def _range_bucket(field: str, low: float, high: float) -> dict:
    return {"field": field, "op": "between", "value": [low, high]}


# 焦距桶：贴近摄影师的心智（广角/标准/中长/长焦），而不是给一串裸数字。
FOCAL_BUCKETS: tuple[tuple[str, Any], ...] = (
    ("广角 ≤35mm", _range_bucket("focal_length", 0, 35)),
    ("标准 35–70mm", _range_bucket("focal_length", 35.001, 70)),
    ("中长 70–135mm", _range_bucket("focal_length", 70.001, 135)),
    ("长焦 >135mm", _range_bucket("focal_length", 135.001, 100000)),
)

APERTURE_BUCKETS: tuple[tuple[str, Any], ...] = (
    ("大光圈 ≤f/2", _range_bucket("aperture", 0, 2)),
    ("f/2–f/4", _range_bucket("aperture", 2.001, 4)),
    ("f/4–f/8", _range_bucket("aperture", 4.001, 8)),
    ("小光圈 >f/8", _range_bucket("aperture", 8.001, 1000)),
)

ISO_BUCKETS: tuple[tuple[str, Any], ...] = (
    ("低感 ≤400", _range_bucket("iso", 0, 400)),
    ("中感 400–1600", _range_bucket("iso", 400.001, 1600)),
    ("高感 1600–6400", _range_bucket("iso", 1600.001, 6400)),
    ("极高感 >6400", _range_bucket("iso", 6400.001, 10_000_000)),
)

# 画幅（按长边近似归类；只做粗分，够用且稳定）
SIZE_BUCKETS: tuple[tuple[str, Any], ...] = (
    ("横幅", {"and": [{"field": "width", "op": "gt", "value": 0},
                      {"field": "width", "op": "gte", "value": {"__ratio__": 1}}]}),
)

# 默认分面集合 —— 顺序即筛选条从左到右的顺序。
DEFAULT_FACETS: tuple[FacetDef, ...] = (
    FacetDef("camera_model", "相机", "camera_model"),
    FacetDef("lens_model", "镜头", "lens_model"),
    FacetDef("focal_length", "焦距", "focal_length", buckets=FOCAL_BUCKETS),
    FacetDef("aperture", "光圈", "aperture", buckets=APERTURE_BUCKETS),
    FacetDef("iso", "ISO", "iso", buckets=ISO_BUCKETS),
    FacetDef("star", "星级", "star", multi=False),
    FacetDef("label", "标记", "label", multi=False),
    FacetDef("scene", "场景", "scene"),
)


@dataclass(slots=True)
class FacetValue:
    """一个可选值：显示名、编译用条件、计数、是否选中。"""

    label: str
    condition: Any
    count: int = 0
    selected: bool = False
    raw_value: Any = None

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "count": self.count,
            "selected": self.selected,
            "raw_value": self.raw_value,
        }


@dataclass(slots=True)
class Facet:
    """一个分面：定义 + 当前可选值。"""

    definition: FacetDef
    values: list[FacetValue] = field(default_factory=list)

    @property
    def key(self) -> str:
        return self.definition.key

    @property
    def label(self) -> str:
        return self.definition.label

    @property
    def selected_values(self) -> list[FacetValue]:
        return [value for value in self.values if value.selected]

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "multi": self.definition.multi,
            "values": [value.to_dict() for value in self.values],
        }


class FacetModel:
    """分面筛选的会话状态：选中了哪些值 + 每面实时计数。

    用法::

        model = FacetModel(store)
        model.toggle("camera_model", "ILCE-7M4")   # 选一个相机
        rows = model.query()                        # 拿到筛后的照片
        model.refresh()                             # 重算各面计数（供 UI 显示）

    作用域（全部照片 / 集合 / 人物）通过 `set_scope()` 设定，与筛选条件相互独立。
    """

    def __init__(
        self,
        store: PhotoStore,
        *,
        facets: Sequence[FacetDef] | None = None,
        value_limit: int = 50,
    ) -> None:
        self.store = store
        self.facets: list[FacetDef] = list(facets if facets is not None else DEFAULT_FACETS)
        self.value_limit = int(value_limit)
        # key → 选中的条件列表（保持选择顺序）
        self._selected: dict[str, list[FacetValue]] = {}
        self._scope: Mapping[str, Any] | None = None
        self.rebuild()

    # ------------------------------------------------------------------
    # 作用域
    # ------------------------------------------------------------------
    def set_scope(self, scope: Mapping[str, Any] | None) -> None:
        """设定作用域（"在全部照片 / 集合 X / 人物 Y 内"）。"""
        self._scope = scope
        self.rebuild()

    @property
    def scope(self) -> Mapping[str, Any] | None:
        return self._scope

    def scope_label(self) -> str:
        """作用域的人话（筛选条最左侧显示"在：… 内"）。"""
        if not self._scope:
            return "全部照片"
        kind = self._scope.get("kind")
        if kind == "collection":
            return f"集合「{self._scope.get('name') or '未命名'}」"
        if kind == "person":
            return f"人物「{self._scope.get('name') or '未命名'}」"
        return "全部照片"

    # ------------------------------------------------------------------
    # 选择
    # ------------------------------------------------------------------
    def toggle(self, key: str, raw_value: Any, *, condition: Any = None) -> None:
        """切换某个值的选中态。

        ``condition`` 省略时自动解析：分面若定义了桶（焦距/光圈/ISO），
        用桶自带的区间条件；否则用「该字段 = 该值」。

        【为什么必须解析桶条件】桶分面的 ``raw_value`` 是显示名（如
        ``"长焦 >135mm"``），不是列里真实存在的值。若一律按 ``eq`` 编译，
        就变成 ``focal_length = '长焦 >135mm'`` —— 永远 0 张（此前的真实
        bug）。桶的语义是**区间**，必须回查定义。
        """
        facet = self._facet(key)
        if facet is None:
            raise QueryError(f"未知分面：{key!r}")
        if condition is None:
            condition = self._condition_for(facet, raw_value)

        chosen = self._selected.setdefault(key, [])
        existing = next(
            (item for item in chosen if item.raw_value == raw_value and item.condition == condition),
            None,
        )
        if existing is not None:
            chosen.remove(existing)
        else:
            if not facet.multi:
                # 单选面：再选一个就替换 —— 比"必须逐一取消"顺手。
                chosen.clear()
            chosen.append(FacetValue(label=str(raw_value), condition=condition, raw_value=raw_value))
        self.rebuild()

    def _condition_for(self, facet: FacetDef, raw_value: Any) -> Any:
        """把一个可选值解析成可编译的条件。

        桶分面：按显示名回查 ``buckets`` 里的区间条件；
        普通分面：``{field: {op: eq, value: raw_value}}``。
        """
        for label, condition in facet.buckets:
            if label == raw_value:
                return copy.deepcopy(condition)
        return {"field": facet.field, "op": "eq", "value": raw_value}

    def select(self, key: str, raw_value: Any, *, condition: Any = None) -> None:
        """显式选中（幂等）。"""
        facet = self._facet(key)
        if facet is None:
            raise QueryError(f"未知分面：{key!r}")
        chosen = self._selected.setdefault(key, [])
        if any(item.raw_value == raw_value for item in chosen):
            return
        if condition is None:
            condition = self._condition_for(facet, raw_value)
        if not facet.multi:
            chosen.clear()
        chosen.append(FacetValue(label=str(raw_value), condition=condition, raw_value=raw_value))
        self.rebuild()

    def clear(self, key: str | None = None) -> None:
        """清空某个分面（或全部分面）的选择。"""
        if key is None:
            self._selected.clear()
        else:
            self._selected.pop(key, None)
        self.rebuild()

    def clear_all(self) -> None:
        self._selected.clear()
        self.rebuild()

    @property
    def has_filters(self) -> bool:
        return any(chosen for chosen in self._selected.values())

    def selected_conditions(self) -> list[Any]:
        """把选中项摊平成条件列表（同面多选 → OR，不同面之间 → AND）。"""
        return [item.condition for chosen in self._selected.values() for item in chosen]

    def conditions_by_facet(self) -> dict[str, list[Any]]:
        return {
            key: [item.condition for item in chosen]
            for key, chosen in self._selected.items()
            if chosen
        }

    # ------------------------------------------------------------------
    # 编译与查询
    # ------------------------------------------------------------------
    def condition_tree(self) -> dict | None:
        """当前筛选的完整条件树（不同面之间 AND，同面多选 OR）。"""
        clauses: list[dict] = []
        for conditions in self.conditions_by_facet().values():
            if len(conditions) == 1:
                clauses.append(conditions[0])
            else:
                clauses.append({"or": list(conditions)})
        if not clauses:
            return None
        return clauses[0] if len(clauses) == 1 else {"and": clauses}

    def _tree_without(self, facet_key: str | None) -> dict | None:
        """排除某个面的条件树 —— 分面计数的关键（见模块 docstring）。"""
        clauses: list[dict] = []
        for key, conditions in self.conditions_by_facet().items():
            if key == facet_key:
                continue
            clauses.append(conditions[0] if len(conditions) == 1 else {"or": list(conditions)})
        if not clauses:
            return None
        return clauses[0] if len(clauses) == 1 else {"and": clauses}

    def _scope_and(self, tree: dict | None) -> tuple[str, tuple]:
        """把「作用域 + 条件树」合成 (where_sql, args)。"""
        scope_query = compile_scope(self._scope)
        tree_query = compile_query(tree)
        parts = [part.where_sql for part in (scope_query, tree_query) if part.where_sql]
        args = tuple(scope_query.args) + tuple(tree_query.args)
        if not parts:
            return "", ()
        if len(parts) == 1:
            return parts[0], args
        return f"({parts[0]}) AND ({parts[1]})", args

    def where_clause(self) -> tuple[str, tuple]:
        """当前筛选（含作用域）编译后的 (where_sql, args)。"""
        return self._scope_and(self.condition_tree())

    def query(self, *, order_by: str = "comp_score DESC", limit: int | None = None) -> list[dict]:
        where_sql, args = self.where_clause()
        return self.store.query_photos_where(where_sql, args, order_by=order_by, limit=limit)

    def count(self) -> int:
        where_sql, args = self.where_clause()
        return self.store.count_photos_where_query(where_sql, args)

    # ------------------------------------------------------------------
    # 计数
    # ------------------------------------------------------------------
    def rebuild(self) -> None:
        """重算每个面的可选值与计数。

        每个面的计数都在「作用域 + 其它面条件」下统计（排除自身面），
        这样"再多选一个值"的预览数字才是对的。
        """
        self._values: dict[str, list[FacetValue]] = {
            definition.key: self._build_facet(definition)
            for definition in self.facets
        }

    def _build_facet(self, definition: FacetDef) -> list[FacetValue]:
        where_sql, args = self._scope_and(self._tree_without(definition.key))
        chosen = {item.raw_value for item in self._selected.get(definition.key, [])}

        if definition.buckets:
            values = [
                self._build_bucket(definition, label, condition, where_sql, args, chosen)
                for label, condition in definition.buckets
            ]
            return _merge_hidden_selection(values, self._selected.get(definition.key, []))

        column = FIELD_COLUMNS.get(definition.field, definition.field)
        try:
            rows = self.store.distinct_values(
                column, where_sql=where_sql, args=args, limit=self.value_limit
            )
        except Exception:  # noqa: BLE001 —— 列不存在（如 person_id 尚未建表）时该面留空
            rows = []
        values = [
            FacetValue(
                label=_display_value(definition.field, raw),
                condition={"field": definition.field, "op": "eq", "value": raw},
                count=count,
                selected=raw in chosen,
                raw_value=raw,
            )
            for raw, count in rows
        ]
        return _merge_hidden_selection(values, self._selected.get(definition.key, []))

    def _build_bucket(
        self,
        definition: FacetDef,
        label: str,
        condition: Mapping[str, Any],
        where_sql: str,
        args: tuple,
        chosen: set,
    ) -> FacetValue:
        """单个"桶"的计数：在「作用域 + 其它面条件」下，满足该桶的照片数。

        桶条件与其它条件是**并列**关系（都是 WHERE 的一部分），因此这里做的是
        "两段已编译 SQL 再 AND"，而不是把它们塞回同一棵条件树。
        """
        bucket_query = compile_query(condition)
        parts = [part for part in (where_sql, bucket_query.where_sql) if part]
        combined_sql = " AND ".join(f"({part})" for part in parts)
        combined_args = tuple(args) + tuple(bucket_query.args)
        count = self.store.count_photos_where_query(combined_sql, combined_args)
        return FacetValue(
            label=label, condition=condition, count=count,
            selected=label in chosen, raw_value=label,
        )

    def _facet(self, key: str) -> FacetDef | None:
        for definition in self.facets:
            if definition.key == key:
                return definition
        return None

    def values(self, key: str) -> list[FacetValue]:
        return list(getattr(self, "_values", {}).get(key, ()))

    def facets_snapshot(self) -> list[Facet]:
        """给 UI 用的快照：面 + 值 + 计数 + 选中态。"""
        return [
            Facet(definition, self.values(definition.key))
            for definition in self.facets
        ]

    def to_dict(self) -> dict:
        return {
            "scope": self.scope_label(),
            "count": self.count(),
            "has_filters": self.has_filters,
            "facets": [facet.to_dict() for facet in self.facets_snapshot()],
        }


def _merge_hidden_selection(
    values: list[FacetValue], chosen: list[FacetValue]
) -> list[FacetValue]:
    """把"已选中、但当前计数结果里没出现"的值补回列表。

    为什么需要：选了"镜头 X"之后，其它面的计数口径变化可能让 X 不在前 N 个里，
    或者该值在余下照片中已为 0。若它从列表里消失，用户就**没法取消自己刚选的东西**
    —— 那是比"看到 0 张"更糟的体验。补回来并标 selected，用户就能点掉它。
    """
    present = {value.raw_value for value in values}
    for item in chosen:
        if item.raw_value not in present:
            values.append(
                FacetValue(
                    label=item.label, condition=item.condition,
                    count=0, selected=True, raw_value=item.raw_value,
                )
            )
    return values


def _display_value(field_name: str, raw: Any) -> str:
    """把原始值转成人看的样子（空值/长文本要收敛）。"""
    if raw is None:
        return "（未记录）"
    if field_name == "star":
        try:
            return f"{int(raw)} 星"
        except (TypeError, ValueError):
            return str(raw)
    if field_name == "label":
        return {"P": "保留 P", "X": "排除 X", "U": "待定 U"}.get(str(raw), str(raw))
    if field_name == "focal_length":
        try:
            return f"{float(raw):g}mm"
        except (TypeError, ValueError):
            return str(raw)
    if field_name == "aperture":
        try:
            return f"f/{float(raw):g}"
        except (TypeError, ValueError):
            return str(raw)
    text = str(raw)
    return text if len(text) <= 40 else text[:39] + "…"


__all__ = [
    "FacetDef",
    "Facet",
    "FacetValue",
    "FacetModel",
    "DEFAULT_FACETS",
    "FOCAL_BUCKETS",
    "APERTURE_BUCKETS",
    "ISO_BUCKETS",
]
