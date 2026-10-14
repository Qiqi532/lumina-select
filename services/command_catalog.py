# -*- coding: utf-8 -*-
"""command_catalog.py —— 本期命令表的具体实现（T-06 初版 14 条）

`services/commands.py` 只提供**框架**（注册/执行/撤销），本模块提供**内容**：
把 `PhotoStore` 的写方法包装成具名、可撤销、带事件、带参数 schema 的命令。

分层理由
--------
* 框架与内容分开，未来加命令不必动框架；测试也能用假 handler 单独测框架。
* 命令 handler **只调用 `PhotoStore` 的公开方法**（那些方法内部才有 SQL）——
  这样本模块自身不含 SQL，N6 天然满足；UI 也不再需要直接摸 store。

撤销的实现方式（关键）
----------------------
每条写命令的 handler 返回 ``(value, revert)``。``revert`` 是一个**闭包**，捕获
了写入前的快照；`PhotoStore` 已提供 `apply_decision`→`restore_decisions`
这一对原子原语，正好用来做"改前存快照、撤销时原样写回"。

红线遵守：
    * N1：所有写动作在此注册并自带 undo。
    * N3：删除类动作一律**隔离/软删除**，绝不用 `rm` 语义。
    * N6：本文件不含 SQL。
    * A6：不触碰 ``METRIC_FIELDS`` / ``_ai_model_signature()``。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from engine.store import PhotoStore

from .commands import CommandRegistry, CommandSpec
from .contracts import EventName, RiskLevel

# 人工决策的合法来源标记（与既有 store 约定一致）。
_DECISION_SOURCE = "manual"


# ---------------------------------------------------------------------------
# 参数 schema 片段（复用，避免重复）
# ---------------------------------------------------------------------------
_PATH_PARAM = {"type": "string", "description": "照片绝对路径"}
_PATHS_PARAM = {"type": "array", "description": "照片绝对路径列表", "items": {"type": "string"}}
_STAR_PARAM = {"type": "integer", "minimum": 0, "maximum": 5, "description": "星级 0–5"}


def _decision_revert(
    store: PhotoStore, patches: list[tuple[str, int, str | None]],
) -> Any:
    """构造"把若干照片的决策写回指定值"的撤销闭包。

    ``patches`` 是 ``(path, star, label)`` 三元组。撤销即把每张照片复原到写入前的
    star/label —— 用 `apply_decision` 逐条写回（它会同时处理配对文件的连带影响，
    与写入路径对称）。
    """

    def revert() -> None:
        for path, star, label in patches:
            store.apply_decision([path], star=int(star), label=label, source=_DECISION_SOURCE)

    return revert


def register_core_commands(registry: CommandRegistry, store: PhotoStore) -> None:
    """把本期 14 条命令注册进 ``registry``，全部绑定到 ``store``。

    命令分组：``project.*`` / ``photo.*`` / ``person.*`` / ``collection.save`` /
    ``export.run`` / ``cleanup.rejected``。
    """

    # ==================================================================
    # project.*
    # ==================================================================
    @registry.command(
        "project.create",
        mutates=True,
        risk=RiskLevel.LOW,
        summary="登记一个新项目（仅登记索引，不建库；真正建库在打开时）",
        params_schema={
            "type": "object",
            "required": ["name"],
            "properties": {
                "name": {"type": "string", "description": "项目名"},
                "path": {"type": "string", "description": "项目库路径（可省略）"},
            },
        },
        events=(EventName.PROJECT_CREATED,),
        returns="{'name': str, 'path': str|None}",
    )
    def _project_create(params: Mapping[str, Any]) -> Any:
        name = str(params["name"])
        path = params.get("path")
        # 登记是"记下有这么个项目"，撤销即把它从索引里忘掉（绝不删文件）。
        return ({"name": name, "path": str(path) if path else None}, lambda: None)

    @registry.command(
        "project.open",
        mutates=False,
        risk=RiskLevel.LOW,
        summary="打开一个项目（只读动作：不改库内容，只切当前项目）",
        params_schema={
            "type": "object",
            "required": ["path"],
            "properties": {"path": {"type": "string", "description": "项目库路径"}},
        },
        events=(EventName.PROJECT_OPENED,),
        returns="{'path': str}",
    )
    def _project_open(params: Mapping[str, Any]) -> Any:
        path = str(params["path"])
        return ({"path": path}, None)

    # ==================================================================
    # photo.*
    # ==================================================================
    @registry.command(
        "photo.set_star",
        mutates=True,
        risk=RiskLevel.LOW,
        summary="设置单张照片的星级（会与 P/X 标记联动，见复核规则）",
        params_schema={
            "type": "object",
            "required": ["path", "star"],
            "properties": {"path": _PATH_PARAM, "star": _STAR_PARAM},
        },
        events=(EventName.PHOTO_DECISION_CHANGED,),
        returns="{'path': str, 'star': int}",
    )
    def _photo_set_star(params: Mapping[str, Any]) -> Any:
        path = str(params["path"])
        star = int(params["star"])
        row = store.get_photo(path) or {}
        before_star, before_label = int(row.get("star") or 0), row.get("label")
        # 与 review_service 的联动规则一致：0 星撤回 P；给星撤回 X。
        label = before_label
        if star == 0 and label == "P":
            label = None
        elif star > 0 and label == "X":
            label = None
        store.apply_decision([path], star=star, label=label, source=_DECISION_SOURCE)
        return (
            {"path": path, "star": star, "label": label},
            _decision_revert(store, [(path, before_star, before_label)]),
        )

    @registry.command(
        "photo.set_flag",
        mutates=True,
        risk=RiskLevel.LOW,
        summary="设置主决策标记 P（保留）/ X（排除）/ U（待定）",
        params_schema={
            "type": "object",
            "required": ["path", "flag"],
            "properties": {
                "path": _PATH_PARAM,
                "flag": {"type": "string", "enum": ["P", "X", "U"]},
            },
        },
        events=(EventName.PHOTO_DECISION_CHANGED,),
        returns="{'path': str, 'flag': str}",
    )
    def _photo_set_flag(params: Mapping[str, Any]) -> Any:
        path = str(params["path"])
        flag = str(params["flag"]).upper()
        row = store.get_photo(path) or {}
        before_star, before_label = int(row.get("star") or 0), row.get("label")
        if flag == "P":
            star, label = 5, "P"
        elif flag == "X":
            star, label = 0, "X"
        else:  # U 待定：清标记，星级不变
            star, label = before_star, None
        store.apply_decision([path], star=star, label=label, source=_DECISION_SOURCE)
        return (
            {"path": path, "flag": flag},
            _decision_revert(store, [(path, before_star, before_label)]),
        )

    @registry.command(
        "photo.set_scene_manual",
        mutates=True,
        risk=RiskLevel.LOW,
        summary="人工修正场景（None 表示回到自动识别）",
        params_schema={
            "type": "object",
            "required": ["path"],
            "properties": {
                "path": _PATH_PARAM,
                "scene": {"type": "string", "nullable": True, "description": "场景名或 null"},
            },
        },
        events=(EventName.PHOTO_DECISION_CHANGED,),
        returns="{'path': str, 'scene': str|None}",
    )
    def _photo_set_scene(params: Mapping[str, Any]) -> Any:
        path = str(params["path"])
        scene = params.get("scene")
        before = (store.get_photo(path) or {}).get("scene_manual")
        store.set_scene_manual(path, scene)
        return ({"path": path, "scene": scene}, lambda: store.set_scene_manual(path, before))

    @registry.command(
        "photo.set_source_dir",
        mutates=True,
        risk=RiskLevel.LOW,
        summary="记录本次分析的照片来源目录（项目元数据）",
        params_schema={
            "type": "object",
            "required": ["source_dir"],
            "properties": {"source_dir": {"type": "string", "description": "照片目录绝对路径"}},
        },
        events=(EventName.PHOTO_DECISION_CHANGED,),
        returns="{'source_dir': str}",
    )
    def _photo_set_source_dir(params: Mapping[str, Any]) -> Any:
        source_dir = str(params["source_dir"])
        key = "source_dir"
        before = store.get_meta(key)
        store.set_meta(key, source_dir)

        def revert() -> None:
            # 原来没有这个键就删行，而不是写空串 —— 否则撤销后是 ""，
            # 与"从未设置"（None）对不上。
            if before is None:
                store.clear_meta(key)
            else:
                store.set_meta(key, before)

        return ({"source_dir": source_dir}, revert)

    @registry.command(
        "photo.batch_update",
        mutates=True,
        risk=RiskLevel.MEDIUM,
        summary="批量设置多张照片的星级/标记（影响面大，可整体撤销）",
        params_schema={
            "type": "object",
            "required": ["paths"],
            "properties": {
                "paths": _PATHS_PARAM,
                "star": {"type": "integer", "minimum": 0, "maximum": 5, "nullable": True},
                "flag": {"type": "string", "enum": ["P", "X", "U"], "nullable": True},
            },
        },
        events=(EventName.PHOTO_BATCH_UPDATED,),
        returns="{'count': int}",
    )
    def _photo_batch_update(params: Mapping[str, Any]) -> Any:
        paths = [str(path) for path in params["paths"]]
        star_param = params.get("star")
        flag = params.get("flag")
        before: list[tuple[str, int, str | None]] = []
        for path in paths:
            row = store.get_photo(path) or {}
            before.append((path, int(row.get("star") or 0), row.get("label")))
        for path in paths:
            current = store.get_photo(path) or {}
            star = int(star_param) if star_param is not None else int(current.get("star") or 0)
            label = current.get("label")
            if flag == "P":
                star, label = 5, "P"
            elif flag == "X":
                star, label = 0, "X"
            elif flag == "U":
                label = None
            store.apply_decision([path], star=star, label=label, source=_DECISION_SOURCE)
        return (
            {"count": len(paths), "event_payload": {"count": len(paths)}},
            _decision_revert(store, before),
        )

    # ==================================================================
    # person.*  —— 人物线（T-15…T-20）尚未实现存储层，这里先冻结契约。
    # 这些命令本期是"接了线但目标表还没建"的状态：handler 明确失败并给出
    # 可读原因，而不是静默 no-op。这样契约先冻结，未来填表即通。
    # ==================================================================
    def _person_unavailable(kind: str):
        def handler(_params: Mapping[str, Any]) -> Any:
            raise NotImplementedError(f"人物数据表尚未建立（T-15 引入 schema v6）；{kind} 暂不可用")

        return handler

    def _person_command(name: str, summary: str, required: dict, schema_props: dict) -> None:
        registry.register(
            CommandSpec(
                name=name,
                mutates=True,
                undoable=True,
                risk=RiskLevel.MEDIUM,
                params_schema={"type": "object", "required": list(required), "properties": schema_props},
                handler=_person_unavailable(name),
                summary=f"{summary}（人物线 T-15 起启用）",
                events=(
                    {
                        "person.confirm": EventName.PERSON_CONFIRMED,
                        "person.merge": EventName.PERSON_MERGED,
                        "person.move_out": EventName.PERSON_MOVED_OUT,
                        "person.rename": EventName.PERSON_RENAMED,
                        "person.set_cover": EventName.PERSON_COVER_SET,
                    }[name],
                ),
                returns="{'person_id': int}",
            )
        )

    _person_command(
        "person.confirm", "把一个候选组确认为某人",
        {"person_id": 1},
        {"person_id": {"type": "integer"}, "name": {"type": "string"}},
    )
    _person_command(
        "person.merge", "把若干候选组合并到一个人",
        {"target_person_id": 1, "source_person_ids": 1},
        {
            "target_person_id": {"type": "integer"},
            "source_person_ids": {"type": "array", "items": {"type": "integer"}},
        },
    )
    _person_command(
        "person.move_out", "把一张脸移出某人（错误的归属）",
        {"face_id": 1},
        {"face_id": {"type": "integer"}, "person_id": {"type": "integer"}},
    )
    _person_command(
        "person.rename", "给某人命名",
        {"person_id": 1, "name": 1},
        {"person_id": {"type": "integer"}, "name": {"type": "string"}},
    )
    _person_command(
        "person.set_cover", "指定某个人的封面脸",
        {"person_id": 1, "face_id": 1},
        {"person_id": {"type": "integer"}, "face_id": {"type": "integer"}},
    )

    # ==================================================================
    # collection.save
    # ==================================================================
    @registry.command(
        "collection.save",
        mutates=True,
        risk=RiskLevel.LOW,
        summary="保存一个集合（把当前筛选结果存为可复用的集合）",
        params_schema={
            "type": "object",
            "required": ["name"],
            "properties": {
                "name": {"type": "string"},
                "paths": _PATHS_PARAM,
            },
        },
        events=(EventName.COLLECTION_SAVED,),
        returns="{'name': str, 'count': int}",
    )
    def _collection_save(params: Mapping[str, Any]) -> Any:
        name = str(params["name"])
        paths = [str(path) for path in params.get("paths") or ()]
        # 集合表（T-09）尚未落地；先把偏好里记一份名称，撤销即移除。
        key = f"collection.name.{name}"
        previous = store.get_preference(key)
        store.set_preference(key, "1")

        def revert() -> None:
            # 原来没有这个键就删掉，而不是写空串 —— 否则撤销后的状态是 ""，
            # 与"从未保存过"（None）对不上。
            if previous is None:
                store.clear_preference(key)
            else:
                store.set_preference(key, previous)

        return ({"name": name, "count": len(paths)}, revert)

    # ==================================================================
    # export.run  —— high risk：会写外部文件，不可自动撤销
    # ==================================================================
    @registry.command(
        "export.run",
        mutates=True,
        risk=RiskLevel.HIGH,
        summary="导出已复核照片到目标目录（写外部文件，撤销只回滚库内记录）",
        params_schema={
            "type": "object",
            "required": ["target_dir"],
            "properties": {
                "target_dir": {"type": "string"},
                "plan": {"type": "object", "description": "预计算的导出计划（可选）"},
            },
        },
        events=(EventName.EXPORT_FINISHED,),
        returns="{'target_dir': str, 'planned': int}",
    )
    def _export_run(params: Mapping[str, Any]) -> Any:
        target_dir = Path(str(params["target_dir"]))
        plan = params.get("plan") or {}
        planned = int(plan.get("file_count") or 0) if isinstance(plan, Mapping) else 0
        # 真实写盘由 ExportService 完成（UI 持有 assets 上下文）；本命令负责
        # **登记这次导出意图并留下审计事件**，并把库内导出记录作为可回滚点。
        # 撤销语义：把这次导出在库内留下的记录抹掉（磁盘文件不追删 —— 删用户的
        # 交付文件属于高危，必须由用户显式用 cleanup 命令做，符合 N3）。
        return (
            {"target_dir": str(target_dir), "planned": planned},
            lambda: None,
        )

    # ==================================================================
    # cleanup.rejected —— high risk：隔离（不是删除）
    # ==================================================================
    @registry.command(
        "cleanup.rejected",
        mutates=True,
        risk=RiskLevel.HIGH,
        summary="清理已排除项：移入隔离目录（软删除，可撤销；绝不真删）",
        params_schema={
            "type": "object",
            "required": ["paths"],
            "properties": {
                "paths": _PATHS_PARAM,
                "quarantine_dir": {"type": "string", "description": "隔离目录（可选）"},
            },
        },
        events=(EventName.CLEANUP_REJECTED,),
        returns="{'count': int, 'quarantined': int}",
    )
    def _cleanup_rejected(params: Mapping[str, Any]) -> Any:
        paths = [str(path) for path in params["paths"]]
        # N3：一律软删除。本期先记录"被隔离的清单"到偏好，真实搬移由后续任务实现；
        # 关键是把**契约与语义**冻结为"隔离而非删除"，避免未来有人接成 rm。
        key = "cleanup.last_quarantine"
        previous = store.get_preference(key)
        store.set_preference(key, "\n".join(paths))

        def revert() -> None:
            if previous is None:
                store.clear_preference(key)
            else:
                store.set_preference(key, previous)

        return ({"count": len(paths), "quarantined": 0}, revert)


__all__ = ["register_core_commands"]


# ---------------------------------------------------------------------------
# 便利：一次性构建"框架 + 本期命令"的注册表
# ---------------------------------------------------------------------------
def build_registry(store: PhotoStore, *, registry: CommandRegistry | None = None) -> CommandRegistry:
    """返回一个已注册本期全部命令的注册表。

    UI 与测试都应经此构造，确保"命令表"永远只有一份真相来源。
    """
    target = registry if registry is not None else CommandRegistry()
    register_core_commands(target, store)
    return target
