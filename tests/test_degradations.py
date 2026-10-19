# -*- coding: utf-8 -*-
"""engine/degradations.py 单测（v0.6 S3-6 / P1-11、P1-12）。

这个模块的**全部**职责是把引擎深处的降级（GPU 回退 CPU、闭眼分类器不可用、
美学头缺失）如实带到分析汇总里。它最容易出错的两种方式是：

  1. 把同一件事重复记很多遍（旧实现每张照片刷一条 warning）；
  2. 变成**跨轮状态**——上一轮的残留被当成这一轮的结果（这正是被删除的
     ``config.INFERENCE_BACKEND`` 踩过的坑）。

因此这里逐条锁定：去重、取走即清空、返回的是快照而非内部列表、线程安全。
"""
from __future__ import annotations

import copy
import threading

import pytest

from engine import degradations


@pytest.fixture(autouse=True)
def _isolated_pending():
    """每个用例前后都清空，避免用例间互相污染。"""
    degradations.drain()
    yield
    degradations.drain()


def test_record_then_drain_returns_entry_and_clears():
    degradations.record("美学与场景", "CUDA OOM", "改用 CPU")
    assert degradations.drain() == [
        {"stage": "美学与场景", "reason": "CUDA OOM", "effect": "改用 CPU",
         "severity": "warning"}
    ]
    assert degradations.drain() == [], "drain 必须取走即清空"


def test_drain_on_empty_pending_is_empty():
    assert degradations.drain() == []


def test_identical_entries_are_deduplicated():
    """同 (stage, reason, effect) 只记一次 —— 数千张照片共用一个失败模型时不许刷屏。"""
    for _ in range(200):
        degradations.record("闭眼分类器", "boom", "只用 EAR")
    assert len(degradations.drain()) == 1


def test_entries_differing_in_any_field_are_kept_separately():
    degradations.record("闭眼分类器", "boom", "只用 EAR")
    degradations.record("闭眼分类器", "boom2", "只用 EAR")     # 只有 reason 不同
    degradations.record("闭眼分类器", "boom", "改用 CPU")      # 只有 effect 不同
    degradations.record("美学评分", "boom", "只用 EAR")        # 只有 stage 不同
    assert len(degradations.drain()) == 4


def test_entries_keep_insertion_order():
    degradations.record("a", "r1", "e1")
    degradations.record("b", "r2", "e2")
    degradations.record("c", "r3", "e3")
    assert [e["stage"] for e in degradations.drain()] == ["a", "b", "c"]


def test_drain_returns_a_snapshot_not_live_state():
    """改动返回值不得影响待取列表（否则调用方一个 .clear() 就能吞掉降级）。"""
    degradations.record("美学与场景", "r", "e")
    taken = degradations.drain()
    taken.clear()
    taken.append({"stage": "假的", "reason": "r", "effect": "e"})
    assert degradations.drain() == [], "返回值不是内部列表的引用"


def test_recorded_entry_is_not_mutated_by_later_edits():
    """record 传入的 entry 是被复制的，调用方事后改动字典不影响已记录内容。"""
    payload = {"stage": "闭眼分类器", "reason": "boom", "effect": "只用 EAR"}
    expected = copy.deepcopy(payload)
    expected["severity"] = "warning"
    degradations.record(**payload)
    payload["reason"] = "变了"
    assert degradations.drain() == [expected]


def test_drain_is_safe_under_concurrent_record():
    """stage1 走线程池、多线程同时上报：不许丢事件、不许抛异常。"""
    per_thread = 50
    n_threads = 4

    def worker(tag: int) -> None:
        for i in range(per_thread):
            degradations.record(f"stage{tag}", f"reason{i}", "effect")

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(degradations.drain()) == per_thread * n_threads
    assert degradations.drain() == []


def test_severity_is_explicit_and_validated():
    degradations.record("CLIP", "missing", "review", severity="critical")
    assert degradations.drain()[0]["severity"] == "critical"
    with pytest.raises(ValueError, match="severity"):
        degradations.record("CLIP", "missing", "review", severity="silent")
