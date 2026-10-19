"""Compare scene prompts and report seven-label and scoring-class metrics."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine import aesthetics, config, loader  # noqa: E402


ENGLISH_PROMPTS = (
    "A portrait photograph of a person",
    "A landscape photograph of mountains, trees, sea or sky",
    "An architecture and city photograph",
    "A documentary street photography scene",
    "A photograph of a pet animal",
    "A still life photograph of food or objects",
    "A photograph of another subject",
)
LABELS = tuple(aesthetics.SCENE_NAMES)
SCORING = ("人像", "风光", "其他")
RAISE_TRUTH = {
    "people": "人像", "landscape": "风光", "nature": "风光",
    "buildings": "建筑/城市", "objects": "静物/美食",
}


def scoring_class(label: str) -> str:
    return label if label in ("人像", "风光") else "其他"


def load_manifest(path: Path, raise_root: Path | None) -> list[dict]:
    raw = path.read_bytes()
    try:
        decoded = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        # Excel on Chinese Windows may save the filled CSV as GBK.
        decoded = raw.decode("gbk")
    import io
    source = list(csv.DictReader(io.StringIO(decoded)))
    rows = []
    for row in source:
        if raise_root is not None:
            label = RAISE_TRUTH.get(row.get("_cat", ""))
            photo = raise_root / (row["File"] + ".NEF")
        else:
            label = row.get("scene", "").strip()
            if label == "风景":
                label = "风光"
            photo = Path(row["path"])
        if label and photo.is_file():
            if label not in LABELS and label not in SCORING:
                raise ValueError(f"unknown label {label!r}: {photo}")
            rows.append({"path": str(photo), "truth": label,
                         "raise_full_decode": raise_root is not None,
                         "seven_truth": raise_root is not None})
    return rows


def summarize(rows: list[dict], predictions: list[dict]) -> dict:
    matrix = {truth: {pred: 0 for pred in SCORING} for truth in SCORING}
    seven_total = seven_hits = 0
    for row, pred in zip(rows, predictions):
        truth = row["truth"]
        matrix[scoring_class(truth)][scoring_class(pred["scene"])] += 1
        if row.get("seven_truth"):
            seven_total += 1
            seven_hits += pred["scene"] == truth
    total = sum(sum(line.values()) for line in matrix.values())
    correct = sum(matrix[label][label] for label in SCORING)
    recall = {label: (matrix[label][label] / sum(matrix[label].values())
                      if sum(matrix[label].values()) else None)
              for label in SCORING}
    return {"sample_count": total, "three_class_correct": correct,
            "three_class_accuracy": correct / total if total else None,
            "seven_label_comparable": seven_total,
            "seven_label_accuracy": seven_hits / seven_total if seven_total else None,
            "confusion": matrix, "recall": recall}


def predict(rows: list[dict], prompts: tuple[str, ...] | list[str],
            threshold: float) -> list[dict]:
    from PIL import Image

    results = []
    texts = aesthetics.GOOD + aesthetics.BAD + list(prompts)
    for start in range(0, len(rows), 8):
        images: list[Image.Image] = []
        for row in rows[start:start + 8]:
            if row.get("raise_full_decode"):
                import rawpy
                with rawpy.imread(row["path"]) as raw:
                    image = Image.fromarray(raw.postprocess())
                # Match the archived Stage E regression exactly; production
                # preview decoding uses CLIP_LOAD_SIZE (256) instead.
                image.thumbnail((512, 512))
            else:
                image = loader.load_review_preview(row["path"], max_size=config.CLIP_LOAD_SIZE)
            if image is None:
                raise ValueError(f"cannot decode {row['path']}")
            images.append(image)
        scores, _ = aesthetics._forward_and_score(
            images, texts, len(aesthetics.GOOD), len(aesthetics.BAD), len(prompts))
        for values in scores:
            index = max(range(len(values)), key=values.__getitem__)
            confidence = float(values[index])
            results.append({"scene": LABELS[index] if confidence >= threshold else "其他",
                            "confidence": confidence})
    return results


def evaluate(rows: list[dict], threshold: float = 0.6) -> dict:
    output = {"model": config.CLIP_MODEL_NAME, "threshold": threshold,
              "count": len(rows), "variants": {}}
    for name, prompts in (("current", config.SCENES),
                          ("english_photo", ENGLISH_PROMPTS)):
        raw = predict(rows, prompts, 0.0)
        def at(cutoff: float) -> list[dict]:
            return [{**item, "scene": item["scene"] if item["confidence"] >= cutoff
                     else "其他"} for item in raw]
        report = summarize(rows, at(threshold))
        report["threshold_grid"] = {
            f"{cutoff:.1f}": summarize(rows, at(cutoff))["three_class_accuracy"]
            for cutoff in (0.3, 0.4, 0.5, 0.6, 0.7, 0.8)
        }
        output["variants"][name] = report
    baseline = output["variants"]["current"]
    for report in output["variants"].values():
        report["eligible_classes"] = [label for label in SCORING
                                      if report["recall"][label] is not None
                                      and report["recall"][label] >= 0.8
                                      and report["three_class_accuracy"] >=
                                      baseline["three_class_accuracy"]]
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--raise-root", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--threshold", type=float, default=0.6)
    args = parser.parse_args()
    rows = load_manifest(args.manifest, args.raise_root)
    if not rows:
        raise SystemExit("no labeled, readable photos in manifest")
    result = evaluate(rows, args.threshold)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
