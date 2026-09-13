"""Measure 1000-item review loading and five asset filters without image IO."""
from __future__ import annotations

import argparse
import json
import platform
from pathlib import Path
import sys
import tempfile
from time import perf_counter

from PyQt6.QtCore import QT_VERSION_STR

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.store import PhotoStore
from services.review_service import ReviewFilter, ReviewService


FILTERS = (
    ReviewFilter.AI_PICKS,
    ReviewFilter.UNCERTAIN,
    ReviewFilter.REJECTED,
    ReviewFilter.TECHNICAL_ISSUE,
    ReviewFilter.STAR,
)


def run_benchmark(items: int) -> dict:
    if items < 1:
        raise ValueError("items must be positive")
    with tempfile.TemporaryDirectory(prefix="lumina-review-benchmark-") as directory:
        store = PhotoStore(str(Path(directory) / "review.db"), enable_wal=False)
        try:
            store.upsert_photos_batch([
                {
                    "path": f"/benchmark/{index:06d}.jpg",
                    "fname": f"{index:06d}.jpg",
                    "asset_pair_id": f"asset-{index:06d}",
                    "asset_role": "jpeg",
                    "is_best": int(index % 5 == 0),
                    "is_uncertain": int(index % 7 == 0),
                    "is_waste": int(index % 11 == 0),
                    "blur_score": 10.0 if index % 13 == 0 else 80.0,
                    "star": 5 if index % 5 == 0 else 0,
                }
                for index in range(items)
            ])
            start = perf_counter()
            service = ReviewService(store)
            load_seconds = perf_counter() - start
            filters: dict[str, float] = {}
            for name in FILTERS:
                start = perf_counter()
                service.set_filter(name)
                filters[name.value] = perf_counter() - start
        finally:
            store.close()
    return {
        "items": items,
        "loaded_items": len(service.items),
        "machine": platform.platform(),
        "python": platform.python_version(),
        "qt": QT_VERSION_STR,
        "load_seconds": load_seconds,
        "filters_seconds": filters,
        "thresholds_seconds": {"load": 3.0, "filter": 0.3},
        "passed": len(service.items) == items and load_seconds < 3.0
        and all(elapsed < 0.3 for elapsed in filters.values()),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--items", type=int, default=1000)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)
    result = run_benchmark(args.items)
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.json:
        args.json.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
