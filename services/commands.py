# -*- coding: utf-8 -*-
"""commands.py —— 动作注册表（Agent 架构的唯一入口）

【T-06 / M1-1 目标】把每一个**会改变数据**的用户动作注册成一个具名命令，
UI 上的每个按钮/快捷键最终都调用 ``registry.execute(name, params)``。

为什么值得做（成本极低、收益极高）
----------------------------------
未来要把写操作开放给 Agent 时，**不需要重写任何业务逻辑** —— 命令表本身就是
那套逻辑的具名清单。没有这一层，Agent 接入就意味着逐个动作重新接线并重新
审计权限；有了这一层，接缝已经焊好，剩下的只是"暴露"。

三条硬约束（PLAN.md T-06 验收）
------------------------------
    ① 所有 ``mutates=True`` 的命令**均可 undo**；
    ② 执行后有对应**事件**记录；
    ③ UI 层**无直接写库代码**（由 `tests/test_commands.py` 静态扫描守住）。

红线遵守：
    * N1：写操作必须经本模块注册并实现 undo。
    * N6：SQL 只准许出现在 ``engine/`` 与 ``services/query_builder.py``；
      本模块**不含 SQL**，只调用 ``PhotoStore`` 的方法（那些方法内部才有 SQL）。
    * A6：不触碰 ``METRIC_FIELDS`` / ``_ai_model_signature()``。
"""
from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .contracts import CommandError, CommandResult, EventName, RiskLevel
from .events import EventBus

Handler = Callable[[Mapping[str, Any]], Any]

# 简版 JSON Schema 的类型名 → Python 类型。只支持本期真正用到的几种，
# 不做通用校验器（未来 Agent 需要的是"能读懂形状"，不是重新发明 pydantic）。
_TYPE_MAP: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "object": (dict,),
    "array": (list, tuple),
}


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """一条命令的完整声明。

    ``params_schema`` 是**简版 JSON Schema**（`type`/`required`/`properties`）。
    刻意保持简单：它的用途是让未来 Agent 与人类都能一眼看懂"这条命令要什么
    参数"，而不是充当通用校验框架。
    """

    name: str
    mutates: bool
    undoable: bool
    risk: RiskLevel
    params_schema: Mapping[str, Any]
    handler: Handler
    summary: str = ""
    events: tuple[str, ...] = ()
    returns: str = ""

    def __post_init__(self) -> None:
        if self.mutates and not self.undoable:
            # 这是 T-06 验收① 的**结构保证**：与其在运行时才发现漏了 undo，
            # 不如让"会改数据却不可撤销"的命令根本注册不进来。
            raise ValueError(f"命令 {self.name!r}：mutates=True 必须 undoable=True")

    def to_dict(self) -> dict:
        """给未来 Agent 看的自描述（可直接序列化）。"""
        return {
            "name": self.name,
            "summary": self.summary,
            "mutates": self.mutates,
            "undoable": self.undoable,
            "risk": str(self.risk),
            "params_schema": dict(self.params_schema),
            "events": list(self.events),
            "returns": self.returns,
        }


@dataclass(slots=True)
class _UndoEntry:
    """一条 undo 记录：把"怎么撤销"与"撤销上下文"绑在一起。"""

    token: str
    name: str
    revert: Callable[[], Any]
    created_at: float = field(default_factory=time.time)
    description: str = ""


class CommandRegistry:
    """命令注册表 + 执行器 + 撤销栈。

    * ``execute(name, params)``：校验参数 → 调 handler → **成功后**发事件并入栈 undo。
    * ``undo(token=None)``：撤销最近一条（或指定 token）命令；返回是否成功。
    * 失败**不抛异常**，而是返回 ``ok=False`` 的 `CommandResult`（见 contracts 的取舍说明）。
    """

    MAX_UNDO = 200

    def __init__(self, *, bus: EventBus | None = None) -> None:
        self.bus = bus if bus is not None else EventBus()
        self._specs: dict[str, CommandSpec] = {}
        self._undo_stack: list[_UndoEntry] = []
        self._undo_seq = 0

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------
    def register(self, spec: CommandSpec) -> CommandSpec:
        if spec.name in self._specs:
            raise ValueError(f"命令名重复注册：{spec.name!r}")
        self._specs[spec.name] = spec
        return spec

    def command(
        self,
        name: str,
        *,
        mutates: bool,
        undoable: bool | None = None,
        risk: RiskLevel | str = RiskLevel.LOW,
        params_schema: Mapping[str, Any] | None = None,
        summary: str = "",
        events: Iterable[str] = (),
        returns: str = "",
    ) -> Callable[[Handler], Handler]:
        """装饰器形式注册（比手搓 CommandSpec 更贴近阅读顺序）。

        ``undoable`` 默认跟随 ``mutates`` —— 因为本期**所有**写命令都必须可撤销。
        想写 ``mutates=True, undoable=False`` 会被直接拒绝（见 CommandSpec）。
        """

        def decorate(handler: Handler) -> Handler:
            self.register(
                CommandSpec(
                    name=name,
                    mutates=mutates,
                    undoable=mutates if undoable is None else undoable,
                    risk=RiskLevel(risk),
                    params_schema=dict(params_schema or {}),
                    handler=handler,
                    summary=summary,
                    events=tuple(events),
                    returns=returns,
                )
            )
            return handler

        return decorate

    def register_all(self, specs: Iterable[CommandSpec]) -> None:
        for spec in specs:
            self.register(spec)

    # ------------------------------------------------------------------
    # 查询（给 UI / 未来 Agent）
    # ------------------------------------------------------------------
    def get(self, name: str) -> CommandSpec | None:
        return self._specs.get(name)

    def names(self) -> list[str]:
        return sorted(self._specs)

    def specs(self) -> list[CommandSpec]:
        return [self._specs[name] for name in self.names()]

    def describe(self) -> list[dict]:
        """全部命令的自描述清单（未来 Agent 的"工具列表"就是它）。"""
        return [spec.to_dict() for spec in self.specs()]

    def __contains__(self, name: object) -> bool:
        return name in self._specs

    def __len__(self) -> int:
        return len(self._specs)

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------
    def execute(self, name: str, params: Mapping[str, Any] | None = None) -> CommandResult:
        """执行一条命令。**失败以返回值表达，不抛异常。**

        执行顺序刻意如此：
          1. 找命令 → 2. 校验参数 → 3. 跑 handler → 4. 取 undo 回调 →
          5. **先入栈再发事件**（这样订阅者在事件里看到的状态已可撤销）。
        """
        spec = self._specs.get(name)
        if spec is None:
            return CommandResult(
                name=name, ok=False,
                error=CommandError("unknown_command", f"未注册的命令：{name}"),
            )
        payload = dict(params or {})
        invalid = _validate(payload, spec.params_schema)
        if invalid is not None:
            return CommandResult(name=name, ok=False, error=invalid)

        try:
            value = spec.handler(payload)
        except Exception as error:  # noqa: BLE001 —— 统一收敛成结构化错误
            return CommandResult(
                name=name, ok=False,
                error=CommandError(
                    "handler_failed",
                    f"{type(error).__name__}: {error}",
                    {"command": name},
                ),
            )

        token: str | None = None
        if spec.mutates:
            token = self._push_undo(spec, payload, value)

        emitted = self._emit(spec, payload, value)
        # 【T-10】对外只暴露真正的 value，不暴露 ``(value, revert)`` 元组。
        # 原因有二：① 调用方（尤其未来的 Agent 桥）不该看到也用不上 revert 闭包；
        # ② ``CommandResult.to_dict()`` 要能被序列化，而元组里那个 lambda **根本
        # 没法进 JSON** —— 留着就是给未来埋一个必炸的雷。
        public_value = value[0] if isinstance(value, tuple) and len(value) == 2 else value
        return CommandResult(
            name=name, ok=True, value=public_value, undo_token=token, events=emitted,
        )

    def _push_undo(self, spec: CommandSpec, payload: Mapping[str, Any], value: Any) -> str:
        """从 handler 返回值里取 undo 回调。

        约定：``mutates=True`` 的 handler 返回 ``(value, revert)`` 二元组，
        其中 ``revert()`` 把状态复原。用返回值而非注册期声明，是因为"怎么撤销"
        只有执行时才知道（例如撤销需要写入前快照）。
        """
        revert = value[1] if isinstance(value, tuple) and len(value) == 2 else None
        if not callable(revert):
            # 走到这里说明某条写命令没按约定返回 revert。
            # 由于 mutates 命令已在数据层面被改过，这里必须显式失败而不是假装成功。
            raise TypeError(
                f"命令 {spec.name!r} 声明 mutates=True，但 handler 未返回 (value, revert) 二元组"
            )
        self._undo_seq += 1
        entry = _UndoEntry(
            token=f"u{self._undo_seq}",
            name=spec.name,
            revert=revert,
            description=str(payload.get("_reason") or ""),
        )
        self._undo_stack.append(entry)
        if len(self._undo_stack) > self.MAX_UNDO:
            self._undo_stack.pop(0)
        return entry.token

    def _emit(self, spec: CommandSpec, payload: Mapping[str, Any], value: Any) -> tuple[str, ...]:
        """发布命令声明的事件。返回值若含 ``events`` 字段则以它为准（handler 可知细节）。"""
        names = list(spec.events)
        detail: Mapping[str, Any] = {}
        if isinstance(value, tuple) and value and isinstance(value[0], Mapping):
            inner = value[0]
            if isinstance(inner.get("events"), (list, tuple)):
                names = [str(item) for item in inner["events"]]
            if isinstance(inner.get("event_payload"), Mapping):
                detail = inner["event_payload"]
        payload_out = {**payload, **detail}
        payload_out.pop("_reason", None)
        for event_name in names:
            self.bus.publish(event_name, payload_out)
        return tuple(names)

    # ------------------------------------------------------------------
    # 撤销
    # ------------------------------------------------------------------
    def can_undo(self) -> bool:
        return bool(self._undo_stack)

    def undo(self, token: str | None = None) -> CommandResult:
        """撤销一条命令：默认最近一条，也可指定 token。

        撤销本身也发事件（``command.undone``），这样任务中心能提示"已撤销"。
        """
        entry = self._take_undo(token)
        if entry is None:
            return CommandResult(
                name="undo", ok=False,
                error=CommandError("nothing_to_undo", "没有可撤销的操作"),
            )
        try:
            entry.revert()
        except Exception as error:  # noqa: BLE001
            # 撤销失败：把记录**放回**栈尾，避免"撤销动作丢失且状态不明"。
            self._undo_stack.append(entry)
            return CommandResult(
                name="undo", ok=False,
                error=CommandError("undo_failed", f"{type(error).__name__}: {error}",
                                   {"command": entry.name}),
            )
        self.bus.publish(
            EventName.COMMAND_UNDONE,
            {"command": entry.name, "token": entry.token},
        )
        return CommandResult(name="undo", ok=True, value={"undone": entry.name},
                             undo_token=entry.token, events=(str(EventName.COMMAND_UNDONE),))

    def _take_undo(self, token: str | None) -> _UndoEntry | None:
        if not self._undo_stack:
            return None
        if token is None:
            return self._undo_stack.pop()
        for index in range(len(self._undo_stack) - 1, -1, -1):
            if self._undo_stack[index].token == token:
                return self._undo_stack.pop(index)
        return None

    def undo_depth(self) -> int:
        return len(self._undo_stack)

    def last_undo_name(self) -> str | None:
        return self._undo_stack[-1].name if self._undo_stack else None

    def clear_undo(self) -> None:
        self._undo_stack.clear()


# ---------------------------------------------------------------------------
# 参数校验（极简 JSON Schema 子集）
# ---------------------------------------------------------------------------
def _validate(params: Mapping[str, Any], schema: Mapping[str, Any]) -> CommandError | None:
    """返回错误（校验不过）或 None（通过）。

    只做"形状检查"：必填缺失、类型不符、枚举越界。刻意不做到 JSON Schema 全量 ——
    本期的目标是把**契约写下来**，不是造一个校验框架。
    """
    if not schema:
        return None
    required = schema.get("required") or ()
    missing = [key for key in required if key not in params]
    if missing:
        return CommandError("invalid_params", f"缺少必填参数：{', '.join(missing)}",
                            {"missing": missing})
    properties: Mapping[str, Any] = schema.get("properties") or {}
    for key, rule in properties.items():
        if key not in params or not isinstance(rule, Mapping):
            continue
        value = params[key]
        expected = rule.get("type")
        if value is None and rule.get("nullable"):
            continue
        if expected and expected in _TYPE_MAP:
            allowed = _TYPE_MAP[expected]
            # bool 是 int 的子类，但语义不同：integer 不该接受 True。
            if expected in {"integer", "number"} and isinstance(value, bool):
                return CommandError("invalid_params", f"参数 {key!r} 应为 {expected}，收到布尔值",
                                    {"param": key})
            if not isinstance(value, allowed):
                return CommandError("invalid_params",
                                    f"参数 {key!r} 类型不符：应为 {expected}",
                                    {"param": key, "expected": expected})
        choices = rule.get("enum")
        if choices is not None and value not in choices:
            return CommandError("invalid_params",
                                f"参数 {key!r} 取值不在允许集合内",
                                {"param": key, "allowed": list(choices)})
        # 数值边界：仅有 type 校验时 star=99 会被放行，写库后才发现越界。
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            lower, upper = rule.get("minimum"), rule.get("maximum")
            if lower is not None and value < lower:
                return CommandError("invalid_params",
                                    f"参数 {key!r} 小于最小值 {lower}",
                                    {"param": key, "minimum": lower, "value": value})
            if upper is not None and value > upper:
                return CommandError("invalid_params",
                                    f"参数 {key!r} 大于最大值 {upper}",
                                    {"param": key, "maximum": upper, "value": value})
    return None


__all__ = ["CommandSpec", "CommandRegistry", "Handler"]
