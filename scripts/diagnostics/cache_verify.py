"""离线校验 HF 缓存是否足以加载运行期模型（不联网、不下载）。

用法：
    ./.venv/Scripts/python.exe scripts/diagnostics/cache_verify.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# 强制离线：任何需要联网的行为都会直接报错，从而证明缓存自足
os.environ["HF_HOME"] = str(ROOT / ".hf_cache")
os.environ["HF_HUB_CACHE"] = str(ROOT / ".hf_cache" / "hub")
os.environ["TRANSFORMERS_CACHE"] = str(ROOT / ".hf_cache" / "hub")
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

results: list[tuple[str, bool, str]] = []

# 1) 运行期模型 A：CLIP 美学/场景底座
try:
    from transformers import CLIPModel  # noqa: PLC0415

    model = CLIPModel.from_pretrained("openai/clip-vit-base-patch32")
    n = sum(p.numel() for p in model.parameters())
    dim = model.config.projection_dim
    results.append(("CLIPModel openai/clip-vit-base-patch32", True, f"params={n:,} projection_dim={dim}"))
except Exception as exc:  # noqa: BLE001
    results.append(("CLIPModel openai/clip-vit-base-patch32", False, f"{type(exc).__name__}: {exc}"))

# 2) 运行期模型 B：闭眼 ViT
try:
    from transformers import AutoModelForImageClassification  # noqa: PLC0415

    eye = AutoModelForImageClassification.from_pretrained("dima806/closed_eyes_image_detection")
    n2 = sum(p.numel() for p in eye.parameters())
    results.append(("AutoModel dima806/closed_eyes_image_detection", True, f"params={n2:,} labels={eye.config.num_labels}"))
except Exception as exc:  # noqa: BLE001
    results.append(("AutoModel dima806/closed_eyes_image_detection", False, f"{type(exc).__name__}: {exc}"))

ok_all = True
print("=" * 68)
print("HF 缓存离线自足性校验（HF_HUB_OFFLINE=1 / TRANSFORMERS_OFFLINE=1）")
print("=" * 68)
for name, ok, detail in results:
    ok_all = ok_all and ok
    print(f"[{'PASS' if ok else 'FAIL'}] {name}\n        {detail}")
print("-" * 68)
print("结论：", "缓存自足，离线可加载全部运行期模型" if ok_all else "缓存不完整，存在联网依赖")
sys.exit(0 if ok_all else 1)
