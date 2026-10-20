from __future__ import annotations

import csv

from scripts.evaluate_scenes import REJECT, load_manifest, summarize


def test_excel_gbk_labels_and_landscape_alias(tmp_path):
    photo = tmp_path / "sample.jpg"
    photo.write_bytes(b"test")
    manifest = tmp_path / "labels.csv"
    with manifest.open("w", encoding="gbk", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("编号", "path", "scene"))
        writer.writerow((1, str(photo), "风景"))
    rows = load_manifest(manifest, None)
    assert rows[0]["truth"] == "风景"
    report = summarize(rows, [{"scene": "风景", "confidence": 0.9}])
    assert report["four_class_correct"] == 1
    assert report["recall"]["其他"] is None


def test_four_class_confusion_keeps_abstention_out_of_other():
    rows = [{"truth": "人像"}, {"truth": "其他"}]
    report = summarize(rows, [
        {"scene": REJECT, "confidence": 0.4},
        {"scene": "其他", "confidence": 0.8},
    ])
    assert report["four_class_correct"] == 1
    assert report["coverage"] == 0.5
    assert report["confusion"]["人像"][REJECT] == 1
    assert report["confusion"]["人像"]["其他"] == 0
    assert report["recall"]["其他"] == 1.0
