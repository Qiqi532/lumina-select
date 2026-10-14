# -*- coding: utf-8 -*-
"""contracts.py —— 稳定的数据契约层（DTO / 枚举）

【T-06 / M1-1 目标】为未来的 Agent 预留**唯一入口**所需的第一件事：一份
不会随 UI 重构而漂移的数据契约。

为什么需要它
------------
UI 里的内部类型（`ReviewItem`、各种 QWidget 属性、私有 dict）是"实现细节"，
随时可能改。如果未来 Agent 直接依赖它们，UI 一改 Agent 就碎。本模块把这些
**对外可承诺**的形状收敛成稳定 DTO：命令的入参/出参、错误、事件、风险等级。

边界（本期只做接缝，不做 Agent 功能）
------------------------------------
* **不做** MCP server、**不做** HTTP / JSON-RPC、**不做**网络监听。
* 纯 Python 数据类，无 Qt 依赖（UI 可 import 它，它绝不 import UI）。
* **不写库、不持连接**：本模块只有数据形状，没有行为。

红线遵守：
    * N6：本文件不含 SQL。
    * A6：不触碰 ``METRIC_FIELDS`` / ``_ai_model_signature()``。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping


class RiskLevel(StrEnum):
    """命令风险等级 —— 供未来 Agent 决定"要不要先问用户"。

    * ``LOW``：可逆的日常操作（改星级、改场景标注）。
    * ``MEDIUM``：影响面较大或涉及身份的操作（批量改、人物合并）。
    * ``HIGH``：删除文件 / 写外部产物 / 不可逆动作（导出、清理已排除项）。
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class PhotoFlag(StrEnum):
    """主决策标记（P / X / U）。

    `P` 保留、`X` 排除、`U` 待定。**与星级的联动规则不在本层**（属业务，
    见 `services/review_service.py`），这里只定义取值，避免各处硬编码字符串。
    """

    PICK = "P"
    REJECT = "X"
    UNCERTAIN = "U"


class ReviewFilterName(StrEnum):
    """复核筛选口径 —— UI 与未来 Agent 共用的稳定名称。

    注意：这是**对外契约**。`services/review_service.ReviewFilter` 是内部实现，
    两者取值保持一致由测试守住；将来内部改名不应影响这里。
    """

    ALL = "all"
    AI_PICKS = "ai-picks"
    UNCERTAIN = "uncertain"
    REJECTED = "rejected"
    TECHNICAL_ISSUE = "technical-issue"
    STAR = "star"


@dataclass(frozen=True, slots=True)
class CommandError:
    """命令失败的结构化原因（**不抛异常给未来 Agent**，见 CommandResult）。"""

    code: str
    message: str
    detail: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "detail": dict(self.detail)}


@dataclass(frozen=True, slots=True)
class CommandResult:
    """``registry.execute()`` 的统一返回值。

    设计取舍：**用返回值表达失败，而不是抛异常**。未来 Agent 拿到的应当是一个
    可判定的结构（``ok`` / ``error.code``），而不是需要 try/except 的异常流——
    异常在跨进程/跨语言（未来暴露为工具）时会被压平成字符串，丢失结构化信息。
    """

    name: str
    ok: bool
    value: Any = None
    error: CommandError | None = None
    undo_token: str | None = None
    events: tuple[str, ...] = ()

    def __bool__(self) -> bool:  # 便于 `if result:` 快速判断
        return self.ok

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "ok": self.ok,
            "value": self.value,
            "error": self.error.to_dict() if self.error else None,
            "undo_token": self.undo_token,
            "events": list(self.events),
        }


@dataclass(frozen=True, slots=True)
class EventRecord:
    """事件总线里的一条记录（不可变快照，便于测试与回放）。"""

    seq: int
    name: str
    payload: Mapping[str, Any]
    ts: float

    def to_dict(self) -> dict:
        return {"seq": self.seq, "name": self.name, "payload": dict(self.payload), "ts": self.ts}


# ---------------------------------------------------------------------------
# 事件名常量 —— 字符串不散落在各处，改一处即可
# ---------------------------------------------------------------------------
class EventName(StrEnum):
    """标准事件名。**新增事件请在此登记**，避免各处写裸字符串。"""

    PROJECT_CREATED = "project.created"
    PROJECT_OPENED = "project.opened"
    PHOTO_DECISION_CHANGED = "photo.decision_changed"
    PHOTO_BATCH_UPDATED = "photo.batch_updated"
    PERSON_CONFIRMED = "person.confirmed"
    PERSON_MERGED = "person.merged"
    PERSON_MOVED_OUT = "person.moved_out"
    PERSON_RENAMED = "person.renamed"
    PERSON_COVER_SET = "person.cover_set"
    COLLECTION_SAVED = "collection.saved"
    EXPORT_FINISHED = "export.finished"
    CLEANUP_REJECTED = "cleanup.rejected"
    COMMAND_UNDONE = "command.undone"
    # 【T-07】任务中心生命周期事件 —— 让 UI 与未来 Agent 都能订阅"在干什么"。
    TASK_STARTED = "task.started"
    TASK_PROGRESS = "task.progress"
    TASK_SUCCEEDED = "task.succeeded"
    TASK_FAILED = "task.failed"
    TASK_CANCELLED = "task.cancelled"


class TaskState(StrEnum):
    """任务生命周期状态。

    【为什么是一个显式状态机而不是几个 bool】"运行中/已完成/已取消"若有多个
    bool 表达，就会出现矛盾组合（``running=True, done=True``）——调用方必须
    自己判断哪个优先。单一枚举让非法状态**无法表示**。
    """

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        """是否已结束（不再变化）。UI 据此决定要不要把任务移出"进行中"列表。"""
        return self in (TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED)


__all__ = [
    "RiskLevel",
    "PhotoFlag",
    "ReviewFilterName",
    "CommandError",
    "CommandResult",
    "EventRecord",
    "EventName",
    "TaskState",
]
