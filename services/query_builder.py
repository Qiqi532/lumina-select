# -*- coding: utf-8 -*-
"""query_builder.py —— 条件树 → 参数化 SQL（分面筛选的 SQL 组装层）

【T-08 / M1-3 目标】让**已经在库里的 EXIF** 可用起来。以前筛选是写死的 6 个按钮，
现在用户要能按"相机 = A7M4 且 焦距 >= 85 且 星级 >= 4"这类组合收窄。

为什么单独一层
--------------
* N6 规定 SQL 只准出现在 `services/` 或 `engine/store.py`。本模块是**唯一**
  负责"把用户的选择翻译成 SQL 片段"的地方，换成别的查询后端时也只改这里。
* **翻译逻辑可单测**：不碰数据库就能验证"条件树 → (where, args)"是否正确，
  这比"跑一遍库再看结果"更快也更准。

安全底线（最重要）
------------------
用户值**一律走参数占位符 `?`**，绝不拼接进 SQL 字符串。字段名来自**白名单**，
不是用户输入 —— 因为占位符只能绑值，绑不了列名，列名必须靠白名单收口。
两条合起来才杜绝注入。

条件树形状（与 `contracts` 一致，可直接被未来 Agent 生成）
--------------------------------------------------------
叶子::

    {"field": "camera_model", "op": "eq", "value": "ILCE-7M4"}
    {"field": "iso", "op": "gte", "value": 1600}
    {"field": "fname", "op": "contains", "value": "IMG_01"}

组合::

    {"and": [<节点>, <节点>, ...]}
    {"or":  [<节点>, ...]}
    {"not": <节点>}

红线遵守：
    * N6：SQL 在本文件（services/）内组装，执行仍走 store 的查询原语。
    * A6：不触碰 ``METRIC_FIELDS`` / ``_ai_model_signature()``。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

# ---------------------------------------------------------------------------
# 字段白名单：列名 → SQL 列表达式
# ---------------------------------------------------------------------------
# 只允许出现在 WHERE 里的列。用户输入碰不到这张表之外的东西。
# 「人工场景」有一个微妙点：查询时应当**优先用 scene_manual**（人工修正），
# 没有时才回落到自动识别的 scene —— 故用 COALESCE 表达，避免用户看到
# "自己刚改成婚礼、筛选'婚礼'却查不到"。
FIELD_COLUMNS: dict[str, str] = {
    # EXIF 六项（T-08 的核心卖点）
    "camera_model": "camera_model",
    "lens_model": "lens_model",
    "focal_length": "focal_length",
    "aperture": "aperture",
    "iso": "iso",
    "shutter_speed": "shutter_speed",
    # 时间
    "ts": "ts",
    # 人工与派生状态
    "star": "star",
    "label": "label",
    "scene": "COALESCE(scene_manual, scene)",
    "scene_manual": "scene_manual",
    # 画幅
    "width": "width",
    "height": "height",
    # 全文
    "path": "path",
    "fname": "fname",
    # 归属（T-15 建表后由 store 决定列是否存在）
    "person_id": "person_id",
    # 技术/AI 状态（沿用既有列，供分面扩展）
    "is_waste": "is_waste",
    "is_best": "is_best",
    "is_uncertain": "is_uncertain",
}

# 排序白名单：排序列必须是固定表达式，不能由用户自由指定（ORDER BY 无法参数化）。
ORDER_COLUMNS: dict[str, str] = {
    "comp_score": "comp_score DESC",
    "score": "comp_score DESC",
    "ts": "ts ASC",
    "ts_desc": "ts DESC",
    "star": "star DESC, comp_score DESC",
    "iso": "iso ASC",
    "focal_length": "focal_length ASC",
    "aperture": "aperture ASC",
    "fname": "fname COLLATE NOCASE ASC",
    "path": "path ASC",
}

# 操作符表：op → SQL 模板（{} 处填列名；值一律用占位符）
_OPS: dict[str, str] = {
    "eq": "{col} = ?",
    "ne": "{col} IS NOT ?",
    "gt": "{col} > ?",
    "gte": "{col} >= ?",
    "lt": "{col} < ?",
    "lte": "{col} <= ?",
    # 字符串包含（不区分大小写：LIKE 对 ASCII 天然不区分）
    "contains": "{col} LIKE ? ESCAPE '\\'",
    "startswith": "{col} LIKE ? ESCAPE '\\'",
    "endswith": "{col} LIKE ? ESCAPE '\\'",
    # 集合
    "in": "{col} IN ({placeholders})",
    "isnull": "{col} IS NULL",
    "notnull": "{col} IS NOT NULL",
    # 区间（含端点）
    "between": "{col} BETWEEN ? AND ?",
}

# 需要字符串模糊匹配的操作符（值要包 % 并转义通配符）
_LIKE_OPS = {"contains", "startswith", "endswith"}

# 只接受 NULL 的操作符（不带 value）
_NULL_OPS = {"isnull", "notnull"}


class QueryError(ValueError):
    """条件树非法（字段/操作符不在白名单、缺值等）。

    与 `services.commands` 的取舍一致：调用方（UI/未来 Agent）应把错误
    展示给用户或记日志，而不是让它冒到事件循环里。
    """

    def __init__(self, message: str, *, node: Any = None) -> None:
        super().__init__(message)
        self.node = node


@dataclass(frozen=True, slots=True)
class Query:
    """编译结果：一段 WHERE 片段 + 对应参数。

    ``where_sql`` 为空串表示"无任何条件"（调用方应跳过 WHERE 子句）。
    """

    where_sql: str
    args: tuple[Any, ...]

    def __bool__(self) -> bool:
        return bool(self.where_sql)


def _escape_like(value: str) -> str:
    """转义 LIKE 的通配符，避免用户输入的 % / _ 改变匹配语义。"""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _leaf(node: Mapping[str, Any]) -> Query:
    field = node.get("field")
    if field not in FIELD_COLUMNS:
        raise QueryError(f"不支持的筛选字段：{field!r}", node=node)
    column = FIELD_COLUMNS[str(field)]
    op = str(node.get("op", "eq")).lower()
    template = _OPS.get(op)
    if template is None:
        raise QueryError(f"不支持的操作符：{op!r}", node=node)

    if op in _NULL_OPS:
        return Query(template.format(col=column), ())

    if "value" not in node:
        raise QueryError(f"操作符 {op!r} 需要 value", node=node)
    value = node["value"]

    if op == "in":
        if not isinstance(value, (list, tuple, set, frozenset)):
            raise QueryError("IN 的 value 必须是列表", node=node)
        items = list(value)
        if not items:
            # 空集合 = 永远为假。用 1=0 表达，避免生成 `IN ()` 这种非法 SQL。
            return Query("1 = 0", ())
        placeholders = ", ".join("?" for _ in items)
        return Query(template.format(col=column, placeholders=placeholders), tuple(items))

    if op == "between":
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise QueryError("BETWEEN 的 value 必须是两个元素的列表", node=node)
        low, high = value
        # 顺序颠倒不是用户错误，是意图明确（"85-35mm" 明显是想 35–85）。自动纠正。
        if low is not None and high is not None and low > high:
            low, high = high, low
        return Query(template.format(col=column), (low, high))

    if op in _LIKE_OPS:
        text = "" if value is None else str(value)
        escaped = _escape_like(text)
        if op == "contains":
            pattern = f"%{escaped}%"
        elif op == "startswith":
            pattern = f"{escaped}%"
        else:
            pattern = f"%{escaped}"
        return Query(template.format(col=column), (pattern,))

    return Query(template.format(col=column), (value,))


def compile_query(node: Mapping[str, Any] | None) -> Query:
    """把条件树编译成 `Query`。``None`` / 空 dict → 无条件。"""
    if not node:
        return Query("", ())

    if not isinstance(node, Mapping):
        raise QueryError(f"条件节点必须是对象，收到 {type(node).__name__}", node=node)

    # 组合节点
    if "and" in node:
        return _combine(node["and"], " AND ", node)
    if "or" in node:
        return _combine(node["or"], " OR ", node)
    if "not" in node:
        inner = compile_query(node["not"])
        if not inner:
            # NOT(无条件) 在语义上是"什么都不匹配"，但更可能是调用方的错误。
            # 明确拒绝，避免悄悄返回空结果让人以为"筛选生效了"。
            raise QueryError("NOT 的子条件不能为空", node=node)
        return Query(f"NOT ({inner.where_sql})", inner.args)

    # 叶子节点
    return _leaf(node)


def _combine(children: Any, joiner: str, node: Mapping[str, Any]) -> Query:
    if not isinstance(children, (list, tuple)):
        raise QueryError("and/or 的子节点必须是列表", node=node)
    if not children:
        # 空 AND = 真（无条件）；空 OR = 假。二者语义不同，必须分别处理。
        return Query("" if joiner.strip() == "AND" else "1 = 0", ())

    parts: list[str] = []
    args: list[Any] = []
    for child in children:
        compiled = compile_query(child)
        if not compiled:
            # 子条件为空（例如 and 里夹了个空 dict）：AND 下可安全忽略；
            # OR 下"忽略"会把整个 OR 变成无条件，必须显式拒绝。
            if joiner.strip() == "OR":
                raise QueryError("OR 的子条件不能为空", node=child)
            continue
        parts.append(compiled.where_sql)
        args.extend(compiled.args)

    if not parts:
        return Query("", ())
    if len(parts) == 1:
        return Query(parts[0], tuple(args))
    return Query(f"({f' {joiner} '.join(parts)})", tuple(args))


def compile_scope(scope: Mapping[str, Any] | None) -> Query:
    """编译**作用域**（"在全部照片 / 集合 X / 人物 Y 内"）。

    作用域与筛选是两件事：作用域回答"在哪个范围里数"，筛选回答"范围里要哪些"。
    分开表达才能让分面计数显示"当前作用域内各值有多少张"。
    """
    if not scope:
        return Query("", ())
    kind = scope.get("kind", "all")
    if kind in (None, "all"):
        return Query("", ())
    if kind == "collection":
        paths = scope.get("paths")
        if not paths:
            return Query("1 = 0", ())
        placeholders = ", ".join("?" for _ in paths)
        return Query(f"path IN ({placeholders})", tuple(paths))
    if kind == "person":
        person_id = scope.get("person_id")
        if person_id is None:
            return Query("1 = 0", ())
        return Query("person_id = ?", (person_id,))
    raise QueryError(f"未知的作用域类型：{kind!r}", node=scope)


def resolve_order(order_by: str | None) -> str:
    """把排序名解析为安全 SQL（白名单外的名字回落到默认，不报错）。

    排序不参与安全边界（它不是数据可见性），因此"未知值回落默认"比"抛错"
    更友好 —— 用户不该因为排序写错就看不到照片。
    """
    if not order_by:
        return ORDER_COLUMNS["comp_score"]
    return ORDER_COLUMNS.get(order_by, ORDER_COLUMNS["comp_score"])


def describe_query(node: Mapping[str, Any] | None) -> str:
    """把条件树翻成人类可读的一句话（供分面筛选条与状态栏显示）。"""
    if not node:
        return "全部照片"
    if "and" in node:
        parts = [describe_query(child) for child in node["and"]]
        parts = [part for part in parts if part and part != "全部照片"]
        return " 且 ".join(parts) if parts else "全部照片"
    if "or" in node:
        parts = [describe_query(child) for child in node["or"]]
        return " 或 ".join(part for part in parts if part) or "全部照片"
    if "not" in node:
        return f"非（{describe_query(node['not'])}）"
    field = node.get("field")
    op = str(node.get("op", "eq")).lower()
    value = node.get("value")
    label = _FIELD_LABELS.get(str(field), str(field))
    if op in _NULL_OPS:
        return f"{label} {'为空' if op == 'isnull' else '非空'}"
    op_text = _OP_LABELS.get(op, op)
    if op == "between" and isinstance(value, (list, tuple)) and len(value) == 2:
        return f"{label} {value[0]}–{value[1]}"
    if op == "in" and isinstance(value, (list, tuple, set)):
        return f"{label} ∈ {{{', '.join(str(item) for item in value)}}}"
    return f"{label} {op_text} {value}"


_FIELD_LABELS = {
    "camera_model": "相机", "lens_model": "镜头", "focal_length": "焦距",
    "aperture": "光圈", "iso": "ISO", "shutter_speed": "快门",
    "star": "星级", "label": "标记", "scene": "场景", "scene_manual": "人工场景",
    "width": "宽", "height": "高", "path": "路径", "fname": "文件名",
    "person_id": "人物", "ts": "时间",
}

_OP_LABELS = {
    "eq": "=", "ne": "≠", "gt": ">", "gte": "≥", "lt": "<", "lte": "≤",
    "contains": "含", "startswith": "以…开头", "endswith": "以…结尾",
}


__all__ = [
    "FIELD_COLUMNS",
    "ORDER_COLUMNS",
    "Query",
    "QueryError",
    "compile_query",
    "compile_scope",
    "resolve_order",
    "describe_query",
]
