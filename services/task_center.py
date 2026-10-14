# -*- coding: utf-8 -*-
"""task_center.py —— 统一任务中心（T-07 / M1-2）

【要解决的问题】导入 / 分析 / 预览 / 人脸 / 导出各自起线程、各自画进度条、
各自实现取消。用户看到的是"四个互不相干的黑盒"，且**取消方式各不相同**。
更糟的是：某些路径取消得"不干净"（数据库停在半途、临时文件残留）。

本模块把"任务"收敛成一个统一对象 + 一个注册表：

    TaskCenter
      ├── register(name, total, cancelable) -> Task
      ├── Task: state / done / total / message / cancel_check()
      └── 生命周期事件发布到 EventBus（task.started / progress / succeeded /
          failed / cancelled）

为什么要发布事件而不是让 UI 直接轮询
------------------------------------
UI 与未来的 Agent 都想知道"现在在干什么"。若 UI 直接读 Task 对象的属性，
就需要定时器轮询；若 Agent 也这样读，就会与 UI 争抢同一条数据路径。
改为**事件推送**后：UI 订阅事件更新界面，Agent 也订阅同一批事件，
两边看到的是**同一份事实**，且不需要 Qt 定时器。

取消的一致性（本任务的核心验收）
--------------------------------
取消**不是**"杀掉线程"——那样会留下半个事务。这里的契约是：

* 取消只是把 ``Task._cancel_requested`` 置位；
* 真正干活的循环在**安全的检查点**调用 ``cancel_check()``，看到已取消就
  **优雅收尾**（把当前这一张/这一批处理完、提交事务、再退出）；
* 于是"取消后库处于一致状态"是**结构上保证**的，而不是靠运气。

因此本模块**不引入锁去强行中断**，也不允许任意的中断 —— 中断点由调用方显式
声明。这让"取消"这件事从"竞态问题"变成"协议问题"。

线程安全
--------
``TaskCenter`` 自己加锁（``threading.Lock``）保护注册表与 Task 状态，因为
worker 线程会调 ``progress()`` / ``finish()``，而 UI 线程会调 ``cancel()`` 与
读列表。**但** ``EventBus.publish`` 仍应从单一（UI）线程调用 —— 故 worker
若需发事件，请经 ``TaskCenter`` 的线程安全入口，由主线程 pump 出去（见
``drain_events``）。本期实现为：worker 调 Task 方法 → Task 记录事件到队列 →
UI 线程调 ``drain_events()`` 一次性发布。这样既线程安全，又保证订阅者
总在 UI 线程被调用（Qt 控件不能跨线程操作）。

红线遵守：
    * N6：本文件不含 SQL。
    * A6：不触碰 ``METRIC_FIELDS`` / ``_ai_model_signature()``。
"""
from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .contracts import EventName, TaskState

# ---------------------------------------------------------------------------
# Task
# ---------------------------------------------------------------------------


@dataclass
class Task:
    """一个可跟踪的后台任务。

    字段刻意保持"足够简单，能被 UI 与 Agent 同时消费"：
    ``state`` / ``done`` / ``total`` / ``message`` 就是状态栏需要的一切。
    """

    id: str
    name: str
    total: int = 0
    cancelable: bool = True
    state: TaskState = TaskState.PENDING
    done: int = 0
    message: str = ""
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    # 由外部（pipeline）在合适时机读取的取消标志
    _cancel_requested: bool = field(default=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # ------------------------------------------------------------------
    # 状态读取
    # ------------------------------------------------------------------
    @property
    def cancel_requested(self) -> bool:
        with self._lock:
            return self._cancel_requested

    @property
    def progress(self) -> float:
        """0.0–1.0；``total`` 未知（0）时返回 0.0。"""
        if self.total <= 0:
            return 0.0
        return min(1.0, max(0.0, self.done / self.total))

    @property
    def percent(self) -> int:
        return int(round(self.progress * 100))

    @property
    def elapsed(self) -> float:
        start = self.started_at or self.created_at
        end = self.finished_at or time.time()
        return max(0.0, end - start)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "state": self.state.value,
            "done": self.done,
            "total": self.total,
            "percent": self.percent,
            "message": self.message,
            "cancelable": self.cancelable,
            "elapsed": round(self.elapsed, 3),
            "error": self.error,
        }

    # ------------------------------------------------------------------
    # 取消（供 pipeline 读取的公开接口）
    # ------------------------------------------------------------------
    def request_cancel(self) -> bool:
        """请求取消。返回**本次调用是否真的改变了状态**（重复调用返回 False）。

        只置位、不中断 —— 真正的收尾由 ``cancel_check()`` 的调用方负责。
        """
        with self._lock:
            if not self.cancelable or self.state.is_terminal:
                return False
            if self._cancel_requested:
                return False
            self._cancel_requested = True
            return True

    def cancel_check(self) -> bool:
        """给 pipeline 的在安全点调用：``True`` = 该收尾退出了。"""
        return self.cancel_requested


# ---------------------------------------------------------------------------
# TaskCenter
# ---------------------------------------------------------------------------


class TaskCenter:
    """任务注册表 + 生命周期事件源。

    用法（worker 侧）::

        task = center.register("分析照片", total=1000)
        center.start(task)
        for i, photo in enumerate(photos):
            if task.cancel_check():
                center.cancel(task)
                return
            process(photo)
            center.progress(task, i + 1, "分析中")
        center.succeed(task, {"analyzed": 1000})

    用法（UI 侧）::

        center.on_change(lambda task: refresh_panel())
        # 或在有自己的事件总线时：
        center.publish_to(bus)          # 转成 task.* 事件
    """

    def __init__(self, *, history: int = 200) -> None:
        self._tasks: dict[str, Task] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        # 待发布事件队列：worker 线程只入队，UI 线程 drain 出去（保证订阅者在 UI 线程）
        self._pending: deque[tuple[str, dict]] = deque()
        self._pending_lock = threading.Lock()
        self._listeners: list[Any] = []
        self._listener_lock = threading.Lock()
        self._history = max(0, int(history))
        self._finished: deque[str] = deque(maxlen=self._history)

    # ------------------------------------------------------------------
    # 注册 / 查询
    # ------------------------------------------------------------------
    def register(
        self,
        name: str,
        *,
        total: int = 0,
        cancelable: bool = True,
        task_id: str | None = None,
    ) -> Task:
        """登记一个新任务（状态 ``PENDING``）。id 自动生成，也可由调用方指定。"""
        task = Task(
            id=task_id or uuid.uuid4().hex[:12],
            name=str(name),
            total=max(0, int(total)),
            cancelable=bool(cancelable),
        )
        with self._lock:
            if task.id in self._tasks:
                raise ValueError(f"任务 id 已存在：{task.id}")
            self._tasks[task.id] = task
            self._order.append(task.id)
        return task

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            return self._tasks.get(task_id)

    def tasks(self) -> list[Task]:
        """按登记顺序返回全部任务（含已结束的）。"""
        with self._lock:
            return [self._tasks[tid] for tid in self._order if tid in self._tasks]

    def active(self) -> list[Task]:
        """仍在运行/等待的任务（UI 的"进行中"列表）。"""
        return [task for task in self.tasks() if not task.state.is_terminal]

    def active_count(self) -> int:
        return len(self.active())

    def finished(self) -> list[Task]:
        return [task for task in self.tasks() if task.state.is_terminal]

    def clear_finished(self) -> int:
        """移除已结束的任务，返回移除个数（进行中的**不动**）。"""
        removed = 0
        with self._lock:
            keep: list[str] = []
            for tid in self._order:
                task = self._tasks.get(tid)
                if task is None:
                    continue
                if task.state.is_terminal:
                    self._tasks.pop(tid, None)
                    removed += 1
                else:
                    keep.append(tid)
            self._order = keep
        return removed

    def any_running(self) -> bool:
        return any(task.state == TaskState.RUNNING for task in self.active())

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self, task: Task, message: str = "") -> bool:
        """置为运行中。已结束的任务不可再启动（返回 False）。"""
        with task._lock:
            if task.state.is_terminal or task.state == TaskState.RUNNING:
                return False
            task.state = TaskState.RUNNING
            task.started_at = task.started_at or time.time()
            if message:
                task.message = message
        self._emit(EventName.TASK_STARTED, task, {})
        return True

    def progress(self, task: Task, done: int | None = None, message: str | None = None) -> None:
        """上报进度。

        【为什么允许 ``done`` 回退】某些 pipeline 会"重算"（例如先粗筛再精算），
        进度条短暂回退比"卡在 99% 不动"更诚实。这里不做单调性限制。
        """
        with task._lock:
            if task.state.is_terminal:
                return
            if done is not None:
                task.done = max(0, int(done))
                if task.total and task.done > task.total:
                    # 提前超出总量说明 total 估小了 —— 就地修正，避免进度条 >100%
                    task.total = task.done
            if message is not None:
                task.message = str(message)
        self._emit(EventName.TASK_PROGRESS, task, {})

    def succeed(self, task: Task, result: Mapping[str, Any] | None = None) -> None:
        """正常结束。"""
        with task._lock:
            if task.state.is_terminal:
                return
            task.state = TaskState.SUCCEEDED
            task.finished_at = time.time()
            if task.total:
                task.done = task.total
        self._remember(task)
        self._emit(EventName.TASK_SUCCEEDED, task, {"result": dict(result or {})})

    def fail(self, task: Task, error: BaseException | str) -> None:
        """异常结束。``error`` 会转成字符串带给界面（完整栈由调用方记日志）。"""
        with task._lock:
            if task.state.is_terminal:
                return
            task.state = TaskState.FAILED
            task.finished_at = time.time()
            task.error = str(error)
        self._remember(task)
        self._emit(EventName.TASK_FAILED, task, {"error": task.error})

    def cancel(self, task: Task, message: str = "已取消") -> bool:
        """把任务标记为已取消（**在 worker 完成安全收尾之后**调用）。

        注意与 ``Task.request_cancel`` 的分工：那个只是"请求"，这个是
        "确认已收尾"。把两者分开，是为了让"取消后库一致"成为可验证的事实
        —— 只要状态到了 ``CANCELLED``，就说明收尾逻辑已经跑过了。
        """
        with task._lock:
            if task.state.is_terminal:
                return False
            task.state = TaskState.CANCELLED
            task.finished_at = time.time()
            task.message = str(message)
        self._remember(task)
        self._emit(EventName.TASK_CANCELLED, task, {})
        return True

    def request_cancel(self, task_or_id: Task | str) -> bool:
        """便捷入口：按任务对象或 id 请求取消。"""
        task = task_or_id if isinstance(task_or_id, Task) else self.get(str(task_or_id))
        if task is None:
            return False
        return task.request_cancel()

    def cancel_all(self) -> int:
        """请求取消所有可取消的进行中任务，返回请求到的个数。"""
        count = 0
        for task in self.active():
            if task.request_cancel():
                count += 1
        return count

    # ------------------------------------------------------------------
    # 事件（线程安全：worker 入队，UI 线程 drain）
    # ------------------------------------------------------------------
    def _emit(self, name: EventName, task: Task, extra: Mapping[str, Any]) -> None:
        payload = {"task_id": task.id, "name": task.name, "state": task.state.value}
        payload.update(task.to_dict())
        payload.update(dict(extra))
        with self._pending_lock:
            self._pending.append((str(name), payload))

    def drain_events(self, publish) -> int:
        """把待发布事件交给 ``publish(name, payload)``（通常是 ``EventBus.publish``）。

        **必须在 UI 线程调用** —— 这样所有订阅者都在 UI 线程被回调，
        Qt 控件可直接更新。返回发布条数。
        """
        with self._pending_lock:
            items = list(self._pending)
            self._pending.clear()
        for name, payload in items:
            publish(name, payload)
        return len(items)

    def pending_event_count(self) -> int:
        with self._pending_lock:
            return len(self._pending)

    # ------------------------------------------------------------------
    # 轻量监听（不依赖 EventBus 的场景，例如单测或纯脚本）
    # ------------------------------------------------------------------
    def add_listener(self, listener) -> None:
        with self._listener_lock:
            self._listeners.append(listener)

    def remove_listener(self, listener) -> None:
        with self._listener_lock:
            try:
                self._listeners.remove(listener)
            except ValueError:
                pass

    def clear(self) -> None:
        """清空全部任务与待发事件（**不触碰任何业务数据**，测试收尾/换项目用）。"""
        with self._lock:
            self._tasks.clear()
            self._order.clear()
            self._finished.clear()
        with self._pending_lock:
            self._pending.clear()

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _remember(self, task: Task) -> None:
        if self._history:
            self._finished.append(task.id)

    def snapshot(self) -> dict:
        """给状态栏/调试用的整体快照（JSON 可序列化）。"""
        tasks = self.tasks()
        return {
            "total": len(tasks),
            "active": sum(1 for t in tasks if not t.state.is_terminal),
            "tasks": [t.to_dict() for t in tasks],
        }


def run_task(
    center: TaskCenter,
    name: str,
    work: Any,
    *,
    total: int = 0,
    cancelable: bool = True,
) -> Task:
    """同步跑一个任务（便利函数，主要给脚本与测试用）。

    ``work`` 接收 ``Task``，返回结果 dict 或 None。内部负责
    start → work → succeed/fail/cancel 的完整收尾 —— 保证**任何路径**都会把
    任务推进到终态，不会留下"永远在运行"的僵尸任务。
    """
    task = center.register(name, total=total, cancelable=cancelable)
    center.start(task)
    try:
        result = work(task)
    except BaseException as error:  # noqa: BLE001 —— 统一转成任务失败，绝不外泄为僵尸
        center.fail(task, error)
        raise
    if task.state == TaskState.CANCELLED:
        return task
    if task.cancel_requested:
        center.cancel(task)
        return task
    center.succeed(task, result if isinstance(result, Mapping) else None)
    return task


__all__ = ["Task", "TaskCenter", "TaskState", "run_task"]
