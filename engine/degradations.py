# -*- coding: utf-8 -*-
"""降级事件收集器（v0.6 S3-6 / P1-11、P1-12）。

背景
----
降级发生在引擎深处 —— GPU 显存不足回退 CPU、闭眼分类器不可用、美学头部缺失 ——
而「对用户可见的汇总」在 pipeline 层。把回调逐层穿透进 aesthetics/faces 会污染
它们的公开签名，因此这里用一个**极小的待取列表**：

  · 引擎任意位置调用 :func:`record` 追加一条降级事件；
  · pipeline 在分析开始时 :func:`drain` 清空残留，返回前再 :func:`drain` 取走合并。

为什么不是「进程级开关」
----------------------
与已被 v0.6 S2 删除的 ``config.INFERENCE_BACKEND`` 不同，这里只做**只追加的事件日志**，
且在分析的**两端都被清空**，绝不会被任何判定逻辑当作状态读取 —— 它只影响给用户的文案。

调用约定：同一进程同一时刻只跑一次分析（``AnalyzePage`` 已有重入守卫）。
"""
from __future__ import annotations

import threading

_lock = threading.Lock()
_pending: list[dict] = []


def record(stage: str, reason: str, effect: str,
           severity: str = "warning") -> None:
    """记录一条降级事件；同 (stage, reason, effect) 只记一次。"""
    if severity not in {"info", "warning", "critical"}:
        raise ValueError(f"invalid severity: {severity}")
    entry = {"stage": stage, "reason": reason, "effect": effect,
             "severity": severity}
    with _lock:
        if entry not in _pending:
            _pending.append(entry)


def drain() -> list[dict]:
    """取出并清空待取的降级事件。"""
    with _lock:
        taken = list(_pending)
        _pending.clear()
    return taken
