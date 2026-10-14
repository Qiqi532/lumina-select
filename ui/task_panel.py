# -*- coding: utf-8 -*-
"""task_panel.py —— 底部统一任务面板（T-07）

【为什么统一到一条底栏】以前导入/分析/预览/人脸/导出各画各的进度条，
用户学四遍才知道怎么取消。统一到一个面板后：

    ┌─ 任务 ───────────────────────────────────────────────┐
    │ ⣾ 分析照片   487/1000  49%  [====      ]  [取消]      │
    │ ✓ 导入        120 张   已用 2.3s                      │
    └───────────────────────────────────────────────────────┘

本组件**只订阅** TaskCenter 的快照，不自己算进度、不自己起线程。
它会顺带在 1 秒级空闲时清掉早已结束的任务，避免面板越堆越长。

线程边界：本组件只在 UI 线程被更新（`refresh()` 由主窗口的主线程定时器
或任务事件触发调用），因此内部不做加锁。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from services.contracts import TaskState
from services.task_center import Task, TaskCenter

# 任务行：图标 + 名称 + 进度条 + 取消按钮
_STATE_ICON = {
    TaskState.PENDING: "…",
    TaskState.RUNNING: "▶",
    TaskState.SUCCEEDED: "✓",
    TaskState.FAILED: "✕",
    TaskState.CANCELLED: "⊘",
}
_STATE_COLOR_NAME = {
    TaskState.PENDING: "",
    TaskState.RUNNING: "TaskRunning",
    TaskState.SUCCEEDED: "TaskOk",
    TaskState.FAILED: "TaskFail",
    TaskState.CANCELLED: "TaskMuted",
}


class TaskRow(QFrame):
    """一行任务。取消按钮只在"运行中且可取消"时出现。"""

    cancel_requested = Signal(str)

    def __init__(self, task: Task, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("TaskRow")
        self.task_id = task.id
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(8)

        self.icon = QLabel(_STATE_ICON.get(task.state, "•"))
        self.icon.setObjectName(_STATE_COLOR_NAME.get(task.state, "") or "TaskMuted")
        self.icon.setFixedWidth(16)
        layout.addWidget(self.icon)

        self.name_label = QLabel(task.name)
        self.name_label.setMinimumWidth(90)
        self.name_label.setMaximumWidth(180)
        layout.addWidget(self.name_label)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(14)
        layout.addWidget(self.progress, 1)

        self.detail = QLabel("")
        self.detail.setObjectName("TaskMuted")
        self.detail.setMinimumWidth(150)
        layout.addWidget(self.detail)

        self.cancel_button = QPushButton("取消")
        self.cancel_button.setObjectName("TaskCancel")
        self.cancel_button.setFixedWidth(52)
        self.cancel_button.clicked.connect(
            lambda: self.cancel_requested.emit(self.task_id)
        )
        layout.addWidget(self.cancel_button)

        self.update_from(task)

    def update_from(self, task: Task) -> None:
        terminal = task.state.is_terminal
        self.icon.setText(_STATE_ICON.get(task.state, "•"))
        self.icon.setObjectName(_STATE_COLOR_NAME.get(task.state, "") or "TaskMuted")
        self.icon.style().unpolish(self.icon)
        self.icon.style().polish(self.icon)

        if task.state == TaskState.RUNNING:
            if task.total:
                self.progress.setRange(0, 100)
                self.progress.setValue(task.percent)
                self.detail.setText(f"{task.done}/{task.total}  {task.percent}%"
                                    + (f"  ·  {task.message}" if task.message else ""))
            else:
                # 总量未知：跑循环条，别假装知道百分比
                self.progress.setRange(0, 0)
                self.detail.setText(task.message or "进行中…")
        elif task.state == TaskState.PENDING:
            self.progress.setRange(0, 100)
            self.progress.setValue(0)
            self.detail.setText("等待中…")
        elif task.state == TaskState.SUCCEEDED:
            self.progress.setRange(0, 100)
            self.progress.setValue(100)
            self.detail.setText(f"完成 · {task.elapsed:.1f}s")
        elif task.state == TaskState.CANCELLED:
            self.progress.setRange(0, 100)
            self.progress.setValue(task.percent)
            self.detail.setText(task.message or "已取消")
        else:  # FAILED
            self.progress.setRange(0, 100)
            self.progress.setValue(task.percent)
            self.detail.setText(f"失败 · {(task.error or '')[:60]}")

        self.cancel_button.setVisible(task.state == TaskState.RUNNING and task.cancelable)
        self.cancel_button.setEnabled(not task.cancel_requested)


class TaskPanel(QWidget):
    """任务面板：订阅 TaskCenter，展示进行中与最近完成的任务。

    对外只暴露 ``selecting``：内部点击取消会通过信号抛给主窗口，
    由主窗口经 TaskCenter 处理（面板不直接操作 worker）。
    """

    cancel_requested = Signal(str)

    def __init__(self, center: TaskCenter, *, keep_finished: int = 2, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("TaskPanel")
        self.center = center
        self.keep_finished = max(0, int(keep_finished))
        self._rows: dict[str, TaskRow] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 4, 8, 4)
        root.setSpacing(2)

        head = QHBoxLayout()
        title = QLabel("任务")
        title.setObjectName("SectionTitle")
        head.addWidget(title)
        self.summary = QLabel("空闲")
        self.summary.setObjectName("TaskMuted")
        head.addWidget(self.summary)
        head.addStretch(1)
        self.clear_button = QPushButton("清空已完成")
        self.clear_button.setObjectName("LinkButton")
        self.clear_button.clicked.connect(self._on_clear)
        head.addWidget(self.clear_button)
        root.addLayout(head)

        self.rows_host = QWidget()
        self.rows_layout = QVBoxLayout(self.rows_host)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(2)
        root.addWidget(self.rows_host)
        self.setVisible(False)

        # 空闲时清理过期行：只动 UI 呈现，不影响 TaskCenter 的事实数据。
        self._gc_timer = QTimer(self)
        self._gc_timer.setInterval(4000)
        self._gc_timer.timeout.connect(self.refresh)
        self._gc_timer.start()

    # ------------------------------------------------------------------
    def refresh(self) -> None:
        """按 TaskCenter 当前状态重建行（行少，重建成本可忽略）。"""
        tasks = self.center.tasks()
        self._prune_gone(tasks)
        ordered = self._ordered(tasks)

        for task in ordered:
            row = self._rows.get(task.id)
            if row is None:
                row = TaskRow(task)
                row.cancel_requested.connect(self.cancel_requested)
                self._rows[task.id] = row
                self.rows_layout.addWidget(row)
            row.update_from(task)

        # 把行按 desired 顺序重排（Qt 里靠重新 addWidget 调整位置）
        for index, task in enumerate(ordered):
            row = self._rows.get(task.id)
            if row is not None:
                self.rows_layout.insertWidget(index, row)

        active = self.center.active()
        if active:
            first = active[0]
            extra = f"（+{len(active) - 1}）" if len(active) > 1 else ""
            self.summary.setText(f"进行中：{first.name}{extra}")
        else:
            self.summary.setText("空闲")
        self.setVisible(bool(ordered) or self.center.pending_event_count() > 0)

    def _ordered(self, tasks: list[Task]) -> list[Task]:
        """进行中按登记顺序在前；已完成只保留最近 ``keep_finished`` 条。"""
        active = [t for t in tasks if not t.state.is_terminal]
        done = [t for t in tasks if t.state.is_terminal]
        if self.keep_finished:
            done = done[-self.keep_finished:]
        else:
            done = []
        return active + done

    def _prune_gone(self, tasks: list[Task]) -> None:
        keep_ids = {t.id for t in self._ordered(tasks)}
        # keep_finished 之外的终态任务也要从 UI 移除
        for task in tasks:
            if not task.state.is_terminal:
                keep_ids.add(task.id)
        for task_id in list(self._rows):
            if task_id not in keep_ids:
                row = self._rows.pop(task_id)
                self.rows_layout.removeWidget(row)
                row.deleteLater()

    def _on_clear(self) -> None:
        self.center.clear_finished()
        self.refresh()


__all__ = ["TaskPanel", "TaskRow"]
