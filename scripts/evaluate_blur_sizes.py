"""Reproduce blur scores at several input sizes without changing originals."""
from __future__ import annotations

import argparse
from glob import glob
import json
from pathlib import Path
import sys

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine import config, quality  # noqa: E402


def load(path: Path) -> Image.Image:
    if path.suffix.lower() in config.RAW_EXTS:
        import rawpy
        with rawpy.imread(str(path)) as raw:
            return Image.fromarray(raw.postprocess())
    with Image.open(path) as image:
        return image.convert("RGB").copy()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--glob", required=True)
    parser.add_argument("--limit", type=int, default=6)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    rows = []
    for name in sorted(glob(args.glob))[:args.limit]:
        source = load(Path(name))
        sizes = {}
        for longest in (1024, 512, 256, 128):
            image = source.copy()
            image.thumbnail((longest, longest))
            result = quality.analyze_image_array(np.asarray(image))
            reliable = bool(result["blur_reliable"])
            sizes[str(longest)] = {
                "actual_size": list(image.size),
                "blur_score": round(result["blur_score"], 2),
                "blur_reliable": reliable,
                "auto_blur_waste": reliable and result["blur_score"] < config.BLUR_WASTE,
            }
        rows.append({"source": str(name), "sizes": sizes})
    report = {"quality_rule": config.QUALITY_RULE_VERSION,
              "threshold": config.BLUR_WASTE, "count": len(rows), "rows": rows}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"count": len(rows), "auto_waste_by_size": {
        size: sum(item["sizes"][size]["auto_blur_waste"] for item in rows)
        for size in ("1024", "512", "256", "128")}}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
