"""Compare seven CLIP prompts using the photographer's four scene labels."""
from __future__ import annotations

import argparse
import csv
import io
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
PROMPT_LABELS = tuple(aesthetics.SCENE_NAMES)
LABELS = config.SCENE_LABELS
REJECT = "待确认"
RAISE_TRUTH = {
    "people": "人像", "landscape": "风光", "nature": "风光",
    "buildings": "建筑/城市", "objects": "静物/美食",
}


def load_manifest(path: Path, raise_root: Path | None) -> list[dict]:
    raw = path.read_bytes()
    try:
        decoded = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        decoded = raw.decode("gbk")
    rows = []
    for row in csv.DictReader(io.StringIO(decoded)):
        if raise_root is not None:
            seven_truth = RAISE_TRUTH.get(row.get("_cat", ""))
            photo = raise_root / (row["File"] + ".NEF")
            label = seven_truth
        else:
            seven_truth = None
            label = row.get("scene", "").strip()
            photo = Path(row["path"])
        if label and photo.is_file():
            truth = config.scene_label(label)
            if truth not in LABELS:
                raise ValueError(f"unknown label {label!r}: {photo}")
            rows.append({"path": str(photo), "truth": truth,
                         "raise_full_decode": raise_root is not None,
                         "seven_truth": seven_truth})
    return rows


def summarize(rows: list[dict], predictions: list[dict]) -> dict:
    if len(rows) != len(predictions):
        raise ValueError("prediction count differs from manifest")
    matrix = {truth: {pred: 0 for pred in (*LABELS, REJECT)} for truth in LABELS}
    seven_total = seven_hits = 0
    for row, pred in zip(rows, predictions):
        predicted = pred["scene"]
        if predicted not in (*LABELS, REJECT):
            raise ValueError(f"unknown prediction: {predicted}")
        matrix[row["truth"]][predicted] += 1
        if row.get("seven_truth"):
            seven_total += 1
            seven_hits += pred.get("seven_scene") == row["seven_truth"]
    total = len(rows)
    correct = sum(matrix[label][label] for label in LABELS)
    accepted = total - sum(matrix[label][REJECT] for label in LABELS)
    counts = {label: sum(matrix[label].values()) for label in LABELS}
    recall = {label: matrix[label][label] / counts[label] if counts[label] else None
              for label in LABELS}
    precision = {
        label: (matrix[label][label] / predicted if predicted else None)
        for label in LABELS
        for predicted in [sum(matrix[truth][label] for truth in LABELS)]
    }
    return {
        "sample_count": total, "class_counts": counts,
        "four_class_correct": correct,
        "four_class_accuracy": correct / total if total else None,
        "coverage": accepted / total if total else None,
        "accepted_accuracy": correct / accepted if accepted else None,
        "needs_review": total - accepted,
        "seven_label_comparable": seven_total,
        "seven_label_accuracy": seven_hits / seven_total if seven_total else None,
        "confusion": matrix, "recall": recall, "precision": precision,
    }


def predict(rows: list[dict], prompts: tuple[str, ...] | list[str]) -> list[dict]:
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
            seven_scene = PROMPT_LABELS[index]
            results.append({"scene": config.scene_label(seven_scene),
                            "seven_scene": seven_scene,
                            "confidence": float(values[index])})
    return results


def evaluate(rows: list[dict], threshold: float = 0.6) -> dict:
    output = {"model": config.CLIP_MODEL_NAME, "threshold": threshold,
              "count": len(rows), "labels": LABELS,
              "adaptive_weights_enabled": config.SCENE_ADAPTIVE_CLASSES,
              "variants": {}}
    for name, prompts in (("current", config.SCENES),
                          ("english_photo", ENGLISH_PROMPTS)):
        raw = predict(rows, prompts)

        def at(cutoff: float) -> list[dict]:
            return [{**item, "scene": item["scene"] if item["confidence"] >= cutoff
                     else REJECT} for item in raw]

        report = summarize(rows, at(threshold))
        report["raw"] = summarize(rows, at(0.0))
        report["threshold_grid"] = {
            f"{cutoff:.1f}": {
                "four_class_accuracy": sample["four_class_accuracy"],
                "coverage": sample["coverage"],
            }
            for cutoff in (0.3, 0.4, 0.5, 0.6, 0.7, 0.8)
            for sample in [summarize(rows, at(cutoff))]
        }
        output["variants"][name] = report
    output["acceptance_note"] = (
        "开发对照数据不能作为独立验收；每类另取至少 8 张未调参照片，"
        "召回率达到 80% 且整体指标不低于旧版后才可启用自适应权重。"
    )
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
