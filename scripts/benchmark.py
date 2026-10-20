# -*- coding: utf-8 -*-
"""
scripts/benchmark.py —— 性能分段基准（MVP 验收 B）

在指定目录上跑一次完整分析（首轮，含模型加载+缩略图+推理），再跑一次增量分析
（仅聚类，验证增量索引），输出各阶段耗时表。也可配合 scripts/generate/make_perf_data.py 生成的
1000 张 data/perf 使用。

用法：
    python scripts/benchmark.py [图片目录] [DB路径] [--count N]
示例：
    python scripts/benchmark.py data/perf data/bench_perf.db
"""
from __future__ import annotations

import argparse
import os
import platform
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from engine import config, loader  # noqa: E402
from engine.pipeline import analyze_directory  # noqa: E402


def _device_desc() -> str:
    try:
        import torch
        if torch.cuda.is_available():
            return f"GPU {torch.cuda.get_device_name(0)}"
        return "CPU（无 GPU，自动降级）"
    except Exception:
        return "unknown"


def _fmt_ms(v: float) -> str:
    return f"{v * 1000:.0f} ms" if v < 1 else f"{v:.2f} s"


def main():
    ap = argparse.ArgumentParser(description="性能分段基准")
    ap.add_argument("root", nargs="?", default="data/perf", help="图片目录")
    ap.add_argument("db", nargs="?", default=None, help="DB 路径")
    ap.add_argument("--count", type=int, default=None, help="限定张数（便于快速验证）")
    args = ap.parse_args()

    root = os.path.join(_ROOT, args.root) if not os.path.isabs(args.root) else args.root
    if not os.path.isdir(root):
        print(f"[错误] 目录不存在：{root}")
        sys.exit(1)
    db = args.db or os.path.join(config.DATA_DIR, "bench_perf.db")
    if not os.path.isabs(db):
        db = os.path.join(_ROOT, db)

    paths = loader.scan_directory(root)
    n = len(paths)
    if args.count:
        n = min(n, args.count)
        paths = paths[:n]
    print("=" * 62)
    print("光影选片助手 · 性能基准")
    print(f"  平台        : {platform.system()} {platform.release()} / Python {platform.python_version()}")
    print(f"  设备        : {_device_desc()}")
    print(f"  数据集      : {root}（{n} 张）")
    print(f"  DB          : {db}")
    print("=" * 62)

    # ---- 首轮完整分析 ----
    print("\n[1/2] 首轮完整分析（含模型加载 + 缩略图 + 推理）…")
    bar = {"last": 0}

    def cb(phase, done, total):
        pass  # 命令行下不刷进度，避免刷屏；耗时在汇总表体现

    t0 = time.time()
    res = analyze_directory(root, db, use_faces=True, progress_cb=cb)
    wall = time.time() - t0

    print(f"\n首轮完成：{res.get('total', 0)} 张 → {res.get('groups', 0)} 组 | "
          f"废片 {res.get('waste', 0)} | 推荐组 {res.get('best_groups', 0)} | "
          f"待甄选组 {res.get('uncertain_groups', 0)}")
    print(f"真实耗时（墙钟）: {wall:.1f} s")
    print("\n阶段耗时（首轮，含首次模型加载）:")
    print(f"  {'阶段':<12}{'耗时':>12}    {'占比':>6}")
    print("  " + "-" * 34)
    pt = res.get("phase_timing", {})
    total_inner = sum(pt.values())
    for k, v in pt.items():
        pct = f"{v / total_inner * 100:5.1f}%" if total_inner else "-"
        print(f"  {k:<12}{_fmt_ms(v):>12}    {pct:>6}")
    print("  " + "-" * 34)
    print(f"  {'合计(段和)':<12}{_fmt_ms(total_inner):>12}")

    # ---- 第二轮增量分析 ----
    print("\n[2/2] 第二轮增量分析（mtime 未变 → 应只做聚类/评分）…")
    t1 = time.time()
    res2 = analyze_directory(root, db, use_faces=True, progress_cb=cb)
    wall2 = time.time() - t1
    print(f"增量完成：新增重算 {res2.get('new_analyzed', 0)} 张，耗时 {wall2:.2f} s")
    print(f"  → 增量索引{'有效' if res2.get('new_analyzed', 0) == 0 else '异常（有重算）'}")

    # ---- 缩略图缓存（5000 目录启动耗时参考）----
    print("\n缩略图缓存预热（首次生成前 200 张缩略图）…")
    t2 = time.time()
    made = 0
    for p in paths[:200]:
        if loader.make_thumbnail(p, config.THUMB_DEFAULT_SIZE):
            made += 1
    print(f"  生成 {made} 张缩略图：{time.time() - t2:.2f} s")
    t3 = time.time()
    for p in paths[:200]:
        loader.make_thumbnail(p, config.THUMB_DEFAULT_SIZE)
    print(f"  二次访问（缓存命中）：{time.time() - t3:.2f} s")

    # 追加一行汇总到 README 可用的 Markdown
    print("\n" + "=" * 62)
    print("完成。")


if __name__ == "__main__":
    main()
