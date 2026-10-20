"""G3 离线门禁探针：断网 + 本地无模型缓存 时，分析必须"降级完成"而不是崩溃。

场景（真实且高概率）：用户把程序装到笔记本，外拍/出差现场无网，且从未联网下载过
模型权重。旧实现在这里直接抛 OSError —— 整次分析失败，阶段一的质量/哈希/人脸
结果白算，用户拿不到任何结果。

用法：
    ./.venv/Scripts/python.exe scripts/diagnostics/offline_probe.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# 指向一个**空**缓存目录 + 强制离线：等价于「首次安装 + 无网络」
_EMPTY = ROOT / "data" / "_offline_probe_cache"
os.environ["HF_HOME"] = str(_EMPTY)
os.environ["HF_HUB_CACHE"] = str(_EMPTY / "hub")
os.environ["TRANSFORMERS_CACHE"] = str(_EMPTY / "hub")
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from engine import pipeline  # noqa: E402


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="lumina_offline_probe_"))
    photos = work / "photos"
    photos.mkdir()
    for i in range(3):
        Image.fromarray(
            np.random.default_rng(i).integers(0, 255, (96, 96, 3), dtype=np.uint8)
        ).save(photos / f"p{i}.jpg")

    print("=" * 68)
    print("G3 探针：断网 + 无模型缓存 下的分析行为")
    print(f"  空缓存目录：{_EMPTY}")
    print(f"  照片目录  ：{photos}")
    print("=" * 68)
    try:
        result = pipeline.analyze_directory(
            str(photos), str(work / "probe.db"), use_faces=False
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] 分析抛异常：{type(exc).__name__}: {exc}")
        print("结论：断网且无缓存时分析直接失败 —— 阶段一结果白算，用户拿不到任何结果。")
        return 1

    degraded = result.get("degradations") or []
    print(f"[OK] 分析完成：total={result['total']} new_analyzed={result['new_analyzed']} "
          f"groups={result['groups']} waste={result['waste']}")
    print(f"     降级项：{len(degraded)}")
    for item in degraded:
        print(f"       · {item['stage']} —— {item['reason']}")
        print(f"         影响：{item['effect']}")
    if degraded:
        print("结论：断网 + 无缓存时降级完成（阶段一结果可用），并如实告知缺项。")
        return 0
    print("[FAIL] 预期降级但未记录 —— 空缓存下仍拿到了美学/场景，说明隔离没生效。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
