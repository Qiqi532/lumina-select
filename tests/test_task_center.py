# -*- coding: utf-8 -*-
"""T-07 验收测试：统一任务中心 + 全局可取消。

【核心验收】"任意任务进行中可取消，取消后**库处于一致状态**（有回归用例）"。
这条分两半，本文件都测：

1. **可取消**：状态机、进度、取消请求语义（幂等、终态不可取消、不可取消任务）；
2. **库一致**：用真实的 `analyze_directory` 跑到一半取消，然后断言
   * 取消返回 `cancelled=True`；
   * 重新打开库能正常读取（连接没坏、事务没停在半途）；
   * **人工决策零丢失**（取消不得回滚或破坏用户已做的标星/标记）。

G2 门禁：不引用历史跑分，只断言本次运行的输出。
"""
from __future__ import annotations

import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from engine.store import PhotoStore
from services.contracts import EventName, TaskState
from services.task_center import Task, TaskCenter, run_task


# ---------------------------------------------------------------------------
# 1. 状态机与生命周期
# ---------------------------------------------------------------------------


class TestTaskLifecycle:
    def test_register_defaults(self):
        center = TaskCenter()
        task = center.register("分析", total=100)
        assert task.state == TaskState.PENDING
        assert task.total == 100
        assert task.cancelable is True
        assert task.done == 0
        assert task.percent == 0
        assert not task.state.is_terminal

    def test_start_sets_running(self):
        center = TaskCenter()
        task = center.register("分析", total=10)
        assert center.start(task) is True
        assert task.state == TaskState.RUNNING
        assert task.started_at is not None

    def test_start_twice_is_rejected(self):
        center = TaskCenter()
        task = center.register("分析")
        center.start(task)
        assert center.start(task) is False

    def test_progress_and_percent(self):
        center = TaskCenter()
        task = center.register("分析", total=200)
        center.start(task)
        center.progress(task, 50)
        assert task.done == 50
        assert task.percent == 25
        assert task.progress == pytest.approx(0.25)

    def test_progress_without_total_is_zero_percent(self):
        center = TaskCenter()
        task = center.register("分析")  # total 未知
        center.start(task)
        center.progress(task, 17)
        assert task.done == 17
        assert task.percent == 0  # 不假装知道百分比

    def test_progress_corrects_undersized_total(self):
        center = TaskCenter()
        task = center.register("分析", total=5)
        center.start(task)
        center.progress(task, 9)
        assert task.total == 9
        assert task.percent == 100

    def test_percent_never_exceeds_100(self):
        center = TaskCenter()
        task = center.register("分析", total=10)
        center.start(task)
        task.done = 999
        assert task.percent == 100

    def test_succeed_is_terminal(self):
        center = TaskCenter()
        task = center.register("分析", total=10)
        center.start(task)
        center.succeed(task, {"n": 10})
        assert task.state == TaskState.SUCCEEDED
        assert task.state.is_terminal
        assert task.done == 10
        assert task.finished_at is not None

    def test_fail_records_error(self):
        center = TaskCenter()
        task = center.register("分析")
        center.start(task)
        center.fail(task, RuntimeError("磁盘满了"))
        assert task.state == TaskState.FAILED
        assert "磁盘满了" in task.error

    def test_terminal_task_ignores_later_transitions(self):
        center = TaskCenter()
        task = center.register("分析", total=1)
        center.start(task)
        center.succeed(task)
        # 之后任何操作都不该改变终态
        center.progress(task, 999)
        center.fail(task, "x")
        center.cancel(task)
        assert task.state == TaskState.SUCCEEDED


# ---------------------------------------------------------------------------
# 2. 取消语义
# ---------------------------------------------------------------------------


class TestCancellation:
    def test_request_cancel_is_idempotent(self):
        center = TaskCenter()
        task = center.register("分析")
        center.start(task)
        assert task.request_cancel() is True
        assert task.request_cancel() is False  # 第二次不再改变状态

    def test_cancel_check_reflects_request(self):
        center = TaskCenter()
        task = center.register("分析")
        center.start(task)
        assert task.cancel_check() is False
        task.request_cancel()
        assert task.cancel_check() is True

    def test_non_cancelable_task_rejects_cancel(self):
        center = TaskCenter()
        task = center.register("快速任务", cancelable=False)
        center.start(task)
        assert task.request_cancel() is False
        assert task.cancel_check() is False

    def test_terminal_task_rejects_cancel(self):
        center = TaskCenter()
        task = center.register("分析")
        center.start(task)
        center.succeed(task)
        assert task.request_cancel() is False

    def test_cancel_marks_terminal(self):
        center = TaskCenter()
        task = center.register("分析")
        center.start(task)
        task.request_cancel()
        assert center.cancel(task) is True
        assert task.state == TaskState.CANCELLED
        assert task.state.is_terminal

    def test_cancel_all(self):
        center = TaskCenter()
        for i in range(3):
            center.start(center.register(f"任务{i}"))
        assert center.cancel_all() == 3
        assert center.active_count() == 3  # 只置位，尚未终态
        for task in center.active():
            center.cancel(task)
        assert center.active_count() == 0

    def test_cancel_by_id(self):
        center = TaskCenter()
        task = center.register("分析")
        center.start(task)
        assert center.request_cancel(task.id) is True
        assert center.request_cancel("no-such-id") is False


# ---------------------------------------------------------------------------
# 3. 注册表 / 查询 / 清理
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_duplicate_id_rejected(self):
        center = TaskCenter()
        center.register("A", task_id="fixed")
        with pytest.raises(ValueError):
            center.register("B", task_id="fixed")

    def test_order_is_registration_order(self):
        center = TaskCenter()
        for name in ("A", "B", "C"):
            center.register(name)
        assert [t.name for t in center.tasks()] == ["A", "B", "C"]

    def test_active_vs_finished(self):
        center = TaskCenter()
        a = center.register("A")
        b = center.register("B")
        center.start(a)
        center.start(b)
        center.succeed(a)
        assert [t.name for t in center.active()] == ["B"]
        assert [t.name for t in center.finished()] == ["A"]

    def test_clear_finished_keeps_active(self):
        center = TaskCenter()
        a = center.register("A")
        b = center.register("B")
        center.start(a)
        center.start(b)
        center.succeed(a)
        assert center.clear_finished() == 1
        assert [t.name for t in center.tasks()] == ["B"]

    def test_clear_removes_everything(self):
        center = TaskCenter()
        center.start(center.register("A"))
        center.clear()
        assert center.tasks() == []
        assert center.pending_event_count() == 0

    def test_snapshot_is_serializable(self):
        import json

        center = TaskCenter()
        center.start(center.register("A", total=10))
        payload = json.dumps(center.snapshot())
        assert '"tasks"' in payload

    def test_any_running(self):
        center = TaskCenter()
        assert center.any_running() is False
        center.start(center.register("A"))
        assert center.any_running() is True


# ---------------------------------------------------------------------------
# 4. 事件（线程安全队列 + drain）
# ---------------------------------------------------------------------------


class TestTaskEvents:
    def test_lifecycle_emits_expected_events(self):
        center = TaskCenter()
        published: list[tuple[str, dict]] = []
        task = center.register("分析", total=10)
        center.start(task)
        center.progress(task, 5, "进行中")
        center.succeed(task, {"n": 10})
        assert center.drain_events(lambda n, p: published.append((n, p))) == 3
        assert [n for n, _ in published] == [
            EventName.TASK_STARTED,
            EventName.TASK_PROGRESS,
            EventName.TASK_SUCCEEDED,
        ]

    def test_cancel_emits_cancelled(self):
        center = TaskCenter()
        published: list[str] = []
        task = center.register("分析")
        center.start(task)
        task.request_cancel()
        center.cancel(task)
        center.drain_events(lambda n, p: published.append(n))
        assert EventName.TASK_CANCELLED in published

    def test_fail_emits_error_payload(self):
        center = TaskCenter()
        published: list[tuple[str, dict]] = []
        task = center.register("分析")
        center.start(task)
        center.fail(task, "boom")
        center.drain_events(lambda n, p: published.append((n, p)))
        name, payload = published[-1]
        assert name == EventName.TASK_FAILED
        assert payload["error"] == "boom"

    def test_payload_carries_task_identity(self):
        center = TaskCenter()
        published: list[dict] = []
        task = center.register("分析", total=4)
        center.start(task)
        center.drain_events(lambda n, p: published.append(p))
        assert published[0]["task_id"] == task.id
        assert published[0]["name"] == "分析"

    def test_drain_clears_queue(self):
        center = TaskCenter()
        center.start(center.register("A"))
        assert center.pending_event_count() == 1
        center.drain_events(lambda n, p: None)
        assert center.pending_event_count() == 0

    def test_worker_thread_can_emit_safely(self):
        """worker 线程并发上报进度不得丢事件或崩（队列由锁保护）。"""
        center = TaskCenter()
        task = center.register("并发", total=200)
        center.start(task)

        def worker():
            for i in range(200):
                center.progress(task, i + 1, "并发")

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # 1 个 started + 800 个 progress
        assert center.pending_event_count() == 801
        published: list[str] = []
        center.drain_events(lambda n, p: published.append(n))
        assert len(published) == 801


# ---------------------------------------------------------------------------
# 5. run_task 便利函数
# ---------------------------------------------------------------------------


class TestRunTask:
    def test_success_path(self):
        center = TaskCenter()
        task = run_task(center, "测试", lambda t: {"value": 42})
        assert task.state == TaskState.SUCCEEDED

    def test_cancel_path(self):
        center = TaskCenter()

        def work(task: Task):
            task.request_cancel()
            return None

        task = run_task(center, "测试", work)
        assert task.state == TaskState.CANCELLED

    def test_failure_propagates_and_marks_failed(self):
        center = TaskCenter()

        def work(task: Task):
            raise RuntimeError("炸了")

        with pytest.raises(RuntimeError):
            run_task(center, "测试", work)
        # 即便抛异常，也绝不能留下"永远运行中"的僵尸任务
        assert center.tasks()[-1].state == TaskState.FAILED
        assert center.active_count() == 0

    def test_never_leaves_zombie(self):
        center = TaskCenter()

        def work(task: Task):
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            run_task(center, "测试", work)
        assert center.active_count() == 0


# ---------------------------------------------------------------------------
# 6. ★ 取消后库一致性（核心验收）
# ---------------------------------------------------------------------------


class TestCancelKeepsStoreConsistent:
    def test_cancel_midway_leaves_store_readable_and_decisions_intact(
        self, full_data_dir, tmp_path
    ):
        """真实 pipeline 跑到中途取消 → 库仍可读、人工决策零丢失。

        【为什么这是最关键的用例】"能取消"很容易做到（杀线程就行），
        "取消后数据不坏"才是产品可信度的底线。这里用真的 analyze_directory，
        在第 N 次进度回调时请求取消，然后：
          1. 断言返回 cancelled=True；
          2. 用一个新连接重新打开库（旧连接若状态损坏会在此暴露）；
          3. 断言取消前写入的人工决策仍在。
        """
        from engine.pipeline import analyze_directory

        directory, _paths = full_data_dir
        db = str(tmp_path / "cancel.db")

        # 先塞一条人工决策（模拟用户已复核过）—— 取消不得动它。
        store = PhotoStore(db)
        marker_path = str(directory / "sharp.jpg")
        store.upsert_photo(
            {"path": marker_path, "fname": "sharp.jpg", "star": 5, "label": "P",
             "decision_source": "manual"}
        )
        before = store.get_photo(marker_path)
        assert before["star"] == 5 and before["label"] == "P"
        store.close()

        cancel_seen = {"fired": False}

        def cancel_check() -> bool:
            if cancel_seen["fired"]:
                return True
            return False

        def progress_cb(phase: str, done: int, total: int) -> None:
            # 跑到第 3 张附近就请求取消 —— 保证是在中途，不是开始/结束
            if not cancel_seen["fired"] and done >= min(3, total):
                cancel_seen["fired"] = True

        result = analyze_directory(
            directory,
            db,
            include_raw=True,
            use_faces=False,
            progress_cb=progress_cb,
            cancel_check=cancel_check,
        )
        assert cancel_seen["fired"], "取消没有被触发（测试前提不成立）"
        assert result.get("cancelled") is True

        # ① 库仍可正常打开（事务没停在半途）
        reopened = PhotoStore(db)
        try:
            photos = reopened.all_photos()
            assert isinstance(photos, list)
            # ② 人工决策零丢失
            after = reopened.get_photo(marker_path)
            assert after is not None, "取消后人工决策所在行丢失"
            assert after["star"] == 5
            assert after["label"] == "P"
            assert after["decision_source"] == "manual"
        finally:
            reopened.close()

    def test_cancel_task_maps_to_cancelled_state(self, full_data_dir, tmp_path):
        """把真实 pipeline 的取消接进 TaskCenter，断言状态落到 CANCELLED。"""
        from engine.pipeline import analyze_directory

        directory, _paths = full_data_dir
        db = str(tmp_path / "cancel2.db")
        center = TaskCenter()
        task = center.register("分析照片", cancelable=True)
        center.start(task)

        fired = {"n": 0}

        def progress_cb(phase: str, done: int, total: int) -> None:
            center.progress(task, done, phase)
            if fired["n"] == 0 and done >= min(3, total):
                task.request_cancel()
                fired["n"] = 1

        def work(t: Task):
            return analyze_directory(
                directory, db, include_raw=True, use_faces=False,
                progress_cb=progress_cb, cancel_check=t.cancel_check,
            )

        result = work(task)
        if result.get("cancelled"):
            center.cancel(task)
        else:
            center.succeed(task, result)
        assert task.state == TaskState.CANCELLED
        assert task.state.is_terminal

    def test_import_smoke_no_qt_required(self, full_data_dir, tmp_path):
        """任务中心是纯 Python（无 Qt）—— 可在任何线程/脚本里用。"""
        import services.task_center as module

        source = open(module.__file__, encoding="utf-8").read()
        assert "PySide6" not in source
        assert "import sqlite3" not in source
