# -*- coding: utf-8 -*-
"""events.py —— 进程内事件总线（纯内存、无 IO、无网络）

【T-06 / M1-1】本期用途有两个：
    1. 驱动 UI 的任务中心（T-07 会订阅它）；
    2. 为未来 Agent 提供"正在发生什么"的上下文 —— Agent 订阅事件即可知道
       用户/系统刚刚做了什么，无需轮询数据库。

设计要点
--------
* **纯内存**：事件不落盘、不发送、不订阅任何网络。进程退出即消失。
* **同步派发**：`publish()` 当场调用订阅者。这**简化了心智模型**（不用考虑
  线程切换与顺序），代价是订阅者必须快速返回 —— 慢活请自己起线程（T-07 再议）。
* **隔离故障**：某个订阅者抛异常**不得**影响其它订阅者，也不得让 `publish()`
  失败。事件是"广播通知"，不是"事务钩子"；一个坏的监听器不能拖垮整个动作。
* **可回放**：保留有界历史（环形缓冲），测试与调试可查"刚才发生了什么"。

线程安全：与 `PhotoStore` 一致，按**单线程使用**设计（Qt 主线程）。
若未来跨线程发布，调用方需自行串行化 —— 这里不引入锁，避免给出"安全"的假象。

红线遵守：
    * N6：本文件不含 SQL。
    * A6：不触碰 ``METRIC_FIELDS`` / ``_ai_model_signature()``。
"""
from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable, Iterable
from typing import Any

from .contracts import EventRecord

# 订阅者签名：接收一条不可变事件记录。返回 None；异常会被吞掉并记录。
Subscriber = Callable[[EventRecord], None]

# 历史保留条数：够 T-07 的任务中心回看，又不至于长期占内存。
DEFAULT_HISTORY = 512


class EventBus:
    """进程内发布订阅总线。

    用法::

        bus = EventBus()
        bus.subscribe("export.finished", lambda e: print(e.payload))
        bus.publish("export.finished", {"count": 3})

    * 支持精确订阅（``"export.finished"``）；
    * 支持通配订阅（``"*"`` 收到全部事件，``"person.*"`` 收到该前缀下全部）；
    * 订阅返回**取消函数**，便于在 widget 销毁时解绑（避免悬挂引用）。
    """

    def __init__(self, *, history: int = DEFAULT_HISTORY) -> None:
        self._subscribers: dict[str, list[Subscriber]] = {}
        self._history: deque[EventRecord] = deque(maxlen=max(1, int(history)))
        self._seq = 0
        self._on_error: Callable[[str, BaseException], None] | None = None

    # ------------------------------------------------------------------
    # 订阅
    # ------------------------------------------------------------------
    def subscribe(self, name: str, handler: Subscriber) -> Callable[[], None]:
        """订阅事件，返回**取消订阅**的函数。

        ``name`` 可为精确名、``"*"``（全部）或 ``"前缀.*"``。取消函数幂等：
        重复调用不会出错。
        """
        if not callable(handler):
            raise TypeError("handler 必须可调用")
        key = str(name)
        self._subscribers.setdefault(key, []).append(handler)
        unsubscribed = False

        def unsubscribe() -> None:
            nonlocal unsubscribed
            if unsubscribed:
                return
            unsubscribed = True
            handlers = self._subscribers.get(key)
            if not handlers:
                return
            try:
                handlers.remove(handler)
            except ValueError:  # 已被清空/重复取消
                pass
            if not handlers:
                self._subscribers.pop(key, None)

        return unsubscribe

    def on_error(self, handler: Callable[[str, BaseException], None] | None) -> None:
        """设置"订阅者抛异常"时的回调（默认静默）。

        生产环境默认静默是为了不让一个坏监听器打断主流程；测试可挂上它来断言
        "确实有订阅者出错"，避免缺陷被悄悄吞掉。
        """
        self._on_error = handler

    # ------------------------------------------------------------------
    # 发布
    # ------------------------------------------------------------------
    def publish(self, name: str, payload: Mapping[str, Any] | None = None) -> EventRecord:
        """发布一条事件并**同步**派发；返回入库的那条记录。

        事件名不做白名单校验 —— 允许业务自定义事件；但标准事件名请登记在
        `contracts.EventName`，避免拼写漂移。
        """
        self._seq += 1
        record = EventRecord(
            seq=self._seq,
            name=str(name),
            payload=dict(payload or {}),
            ts=time.time(),
        )
        self._history.append(record)
        for handler in self._matching(record.name):
            try:
                handler(record)
            except Exception as error:  # noqa: BLE001 —— 广播不得因单个订阅者失败而中断
                if self._on_error is not None:
                    self._on_error(record.name, error)
        return record

    def _matching(self, name: str) -> tuple[Subscriber, ...]:
        """收集匹配的订阅者，顺序固定为：**精确 → 前缀通配 → 全局通配**。

        顺序固定是有意的：订阅者可能互相依赖（精确订阅者常在通配订阅者之前
        把状态准备好），可预测的顺序让行为可复现，而不是依赖 dict 插入序。
        """
        matched: list[Subscriber] = list(self._subscribers.get(name, ()))
        # "person.*" 匹配 "person.confirmed"，但不匹配裸 "person"
        for pattern, handlers in self._subscribers.items():
            if pattern.endswith(".*") and name.startswith(pattern[:-1]):
                matched.extend(handlers)
        matched.extend(self._subscribers.get("*", ()))
        return tuple(matched)

    # ------------------------------------------------------------------
    # 历史（测试 / 调试用）
    # ------------------------------------------------------------------
    def history(self, name: str | None = None) -> list[EventRecord]:
        """返回事件历史；``name`` 给定时只返回该名称（精确匹配）的事件。"""
        records = list(self._history)
        if name is None:
            return records
        return [record for record in records if record.name == name]

    def last(self, name: str | None = None) -> EventRecord | None:
        """最近一条事件（可限定名称）。"""
        records = self.history(name)
        return records[-1] if records else None

    def clear(self) -> None:
        """清空历史与订阅者（测试收尾用；**不影响已发生的业务状态**）。"""
        self._history.clear()
        self._subscribers.clear()
        self._seq = 0

    def subscriber_count(self, name: str | None = None) -> int:
        if name is not None:
            return len(self._subscribers.get(name, ()))
        return sum(len(handlers) for handlers in self._subscribers.values())


def publish_all(bus: EventBus, names: Iterable[str], payload: Mapping[str, Any] | None = None) -> None:
    """便利函数：把同一份 payload 依次发布到多个事件名。"""
    for name in names:
        bus.publish(name, payload)


__all__ = ["EventBus", "Subscriber", "publish_all", "DEFAULT_HISTORY"]
