"""智能集合（T-09）—— 把一次性筛选变成可复用的工作流。

为什么存"条件树"而不是"照片清单"
--------------------------------
存清单看起来简单，但它**会过期**：分析完新照片后，旧集合里没有它们，
用户却以为"我的『人像精选』集合"会自动包含新的人像照。存条件树则天然
"活"着 —— 每次打开都重新求值，计数随之变化。

代价是计数必须实时算（不能缓存成一个数字）。这也是 PLAN 验收里
"计数与 SELECT COUNT(*) 一致"这条的由来。

负向条件
--------
"排除低于 N 星或未评级"这类需求用 ``not`` + ``lt`` / ``isnull`` 表达，
:mod:`services.query_builder` 已支持；本模块只负责把它们**拼对**并给出
人话描述，方便用户确认自己存的是什么。
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from engine.store import PhotoStore
from services.query_builder import (
    QueryError,
    compile_query,
    compile_scope,
    describe_query,
)

MAX_COLLECTIONS = 200


class CollectionError(ValueError):
    """集合相关的可预期错误（重名、条件非法等）。"""


def _dump(condition: Mapping[str, Any]) -> str:
    return json.dumps(condition, ensure_ascii=False, sort_keys=True)


def _load(raw: str) -> dict:
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise CollectionError(f"集合条件不是合法 JSON：{exc}") from exc
    if not isinstance(parsed, dict):
        raise CollectionError("集合条件必须是一个对象（and/or/not 或叶子条件）")
    return parsed


def validate_condition(condition: Mapping[str, Any]) -> None:
    """编译一遍以确认条件合法 —— **不查库**，纯粹是语法校验。

    存一个坏条件进去，用户每次点开都报错，那比存的时候就拦下糟糕得多。
    """
    try:
        compile_query(condition)
    except QueryError as exc:
        raise CollectionError(f"集合条件无法编译：{exc}") from exc


def describe_condition(condition: Mapping[str, Any]) -> str:
    """条件的人话描述（给用户确认"我存的是什么"）。"""
    try:
        return describe_query(condition) or "（无条件：全部照片）"
    except QueryError:
        return "（条件无法解析）"


# ---------------------------------------------------------------------------
# 条件构造助手
# ---------------------------------------------------------------------------
def star_at_least(star: int) -> dict:
    return {"field": "star", "op": "gte", "value": int(star)}


def exclude_below_star_or_unrated(star: int) -> dict:
    """PLAN 明确要求的负向条件：**排除低于 N 星或未评级**的照片。

    等价于 ``NOT (star < N OR star IS NULL)`` —— 注意必须把"未评级"
    一起排除，否则 NULL 会因 SQL 三值逻辑被悄悄留下（``NULL < 3`` 既不是
    真也不是假，直接写 ``NOT star < 3`` 会把未评级的照片留下来）。

    组合节点用的是 :mod:`services.query_builder` 的**原生形状**
    （``{"and": [...]} / {"or": [...]} / {"not": {...}}``），不是
    ``{"op": ..., "conditions": ...}`` —— 两者不通用，写错会静默编出废 SQL。
    """
    return {
        "not": {
            "or": [
                {"field": "star", "op": "lt", "value": int(star)},
                {"field": "star", "op": "isnull"},
            ]
        }
    }


def flagged(flag: str) -> dict:
    return {"field": "label", "op": "eq", "value": str(flag)}


def facet_conditions(values: Sequence[Mapping[str, Any]]) -> dict | None:
    """把若干分面/筛选条件合成一棵 AND 树；空列表返回 ``None``（= 全部照片）。"""
    nodes = [dict(node) for node in values if node]
    if not nodes:
        return None
    if len(nodes) == 1:
        return nodes[0]
    # 注意用 query_builder 的原生形状 {"and": [...]}，不是 {"op": "and", ...}
    return {"and": nodes}


class CollectionService:
    """集合的读、写与计数。写操作由命令表调用（N1）。"""

    def __init__(self, store: PhotoStore) -> None:
        self.store = store

    def list(self) -> list[dict]:
        rows = self.store.collections()
        for row in rows:
            try:
                row["condition_tree"] = _load(row["condition"])
                row["description_text"] = describe_condition(row["condition_tree"])
            except CollectionError:
                row["condition_tree"] = {}
                row["description_text"] = "（条件损坏）"
        return rows

    def count(self, collection_id: int) -> int:
        """实时计数 —— 与 ``SELECT COUNT(*)`` 必须一致（PLAN 验收）。"""
        row = self.store.get_collection(collection_id)
        if row is None:
            return 0
        try:
            condition = _load(row["condition"])
            query = compile_query(condition)
        except (CollectionError, QueryError):
            return 0
        return int(self.store.count_photos_where_query(query.where_sql, query.args))

    def paths(self, collection_id: int, *, limit: int | None = None) -> list[str]:
        """集合内的照片路径（供作用域 / 导出用）。"""
        row = self.store.get_collection(collection_id)
        if row is None:
            return []
        try:
            query = compile_query(_load(row["condition"]))
        except (CollectionError, QueryError):
            return []
        rows = self.store.query_photos_where(
            query.where_sql, query.args, order_by="path", limit=limit
        )
        return [str(item["path"]) for item in rows if item.get("path")]

    def scope(self, collection_id: int) -> dict | None:
        """把集合表达成一个**作用域**（供 FacetModel 在其内计数）。"""
        row = self.store.get_collection(collection_id)
        if row is None:
            return None
        return {
            "kind": "collection",
            "id": int(row["id"]),
            "name": str(row["name"]),
            "paths": self.paths(collection_id),
        }

    def save(
        self,
        name: str,
        condition: Mapping[str, Any] | None,
        *,
        description: str = "",
        overwrite: bool = True,
    ) -> dict:
        """保存集合。同名默认**覆盖**（用户是"再存一次"，不是"新建第二个"）。

        返回 ``{"id", "name", "count", "created"}``；``created`` 标明是新建还是覆盖，
        撤销据此决定"删掉"还是"改回去"。
        """
        cleaned = str(name).strip()
        if not cleaned:
            raise CollectionError("集合名不能为空")
        tree = dict(condition) if condition else {}
        if tree:
            validate_condition(tree)
        if len(self.store.collections()) >= MAX_COLLECTIONS and not self.store.get_collection_by_name(cleaned):
            raise CollectionError(f"集合数量已达上限 {MAX_COLLECTIONS}")

        existing = self.store.get_collection_by_name(cleaned)
        if existing is not None and not overwrite:
            raise CollectionError(f"已存在同名集合「{cleaned}」")
        if existing is None:
            collection_id = self.store.create_collection(
                cleaned, _dump(tree), description=description
            )
            created = True
        else:
            collection_id = int(existing["id"])
            self.store.update_collection(
                collection_id, condition=_dump(tree), description=description
            )
            created = False
        return {
            "id": collection_id,
            "name": cleaned,
            "count": self.count(collection_id),
            "created": created,
        }

    def delete(self, collection_id: int) -> dict | None:
        return self.store.delete_collection(collection_id)

    def restore(self, row: Mapping[str, Any]) -> None:
        self.store.restore_collection(row)


def count_for_condition(
    store: PhotoStore, condition: Mapping[str, Any] | None, *, scope=None
) -> int:
    """给定条件（可选作用域）算照片数 —— 供 UI 在保存前预览"会存进多少张"。"""
    scope_query = compile_scope(scope)
    if not condition:
        return int(store.count_photos_where_query(scope_query.where_sql, scope_query.args))
    query = compile_query(condition)
    if scope_query.where_sql:
        combined_sql = f"({scope_query.where_sql}) AND ({query.where_sql})"
        combined_args = tuple(scope_query.args) + tuple(query.args)
    else:
        combined_sql, combined_args = query.where_sql, query.args
    return int(store.count_photos_where_query(combined_sql, combined_args))


__all__ = [
    "CollectionError",
    "CollectionService",
    "MAX_COLLECTIONS",
    "count_for_condition",
    "describe_condition",
    "exclude_below_star_or_unrated",
    "facet_conditions",
    "flagged",
    "star_at_least",
    "validate_condition",
]
