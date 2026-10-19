from __future__ import annotations

import csv

from scripts.evaluate_scenes import load_manifest, summarize


def test_excel_gbk_labels_and_landscape_alias(tmp_path):
    photo = tmp_path / "sample.jpg"
    photo.write_bytes(b"test")
    manifest = tmp_path / "labels.csv"
    with manifest.open("w", encoding="gbk", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("编号", "path", "scene"))
        writer.writerow((1, str(photo), "风景"))
    rows = load_manifest(manifest, None)
    assert rows[0]["truth"] == "风光"
    report = summarize(rows, [{"scene": "风光", "confidence": 0.9}])
    assert report["three_class_correct"] == 1
    assert report["recall"]["其他"] is None
