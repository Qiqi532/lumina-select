from __future__ import annotations

import hashlib
from pathlib import Path
import zipfile
import pytest
from PIL import Image

from scripts.benchmark_review import run_benchmark
from scripts.verify_exiftool import verify_archive
from services.metadata_writer import MetadataWriter
from services.xmp_service import XmpSelection
from services.distribution_smoke import run_smoke_payload
from scripts.smoke_dist import audit_bundle


def test_exiftool_archive_rejects_wrong_hash(tmp_path):
    archive = tmp_path / "exiftool.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("exiftool.exe", b"MZ")
        bundle.writestr("exiftool_files/readme.txt", b"runtime")
    lock = {"archive_size": archive.stat().st_size, "archive_sha256": "0" * 64}
    assert not verify_archive(archive, lock)
    lock["archive_sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
    assert verify_archive(archive, lock)


def test_review_benchmark_outputs_machine_readable_thresholds(tmp_path):
    result = run_benchmark(20)
    assert result["items"] == 20
    assert result["loaded_items"] == 20
    assert len(result["filters_seconds"]) == 5
    assert result["passed"]
    assert result["python"] and result["qt"] and result["machine"]


def test_staged_exiftool_writes_delivery_jpeg(tmp_path):
    executable = Path(__file__).resolve().parents[1] / "release/exiftool/exiftool.exe"
    config = executable.parent.parent / "exiftool-lumina.config"
    if not executable.is_file():
        pytest.skip("locked ExifTool runtime not staged")
    delivery = tmp_path / "delivery"
    delivery.mkdir()
    target = delivery / "selected.jpg"
    Image.new("RGB", (32, 32), "green").save(target)
    result = MetadataWriter(executable, delivery, config_path=config).write(
        target,
        XmpSelection(5, "Green", (), "0.5.0", "heuristic", ()),
    )
    assert result.success, result
    assert target.is_file()


def test_staged_exiftool_writes_jpeg_in_chinese_delivery_path(tmp_path):
    executable = Path(__file__).resolve().parents[1] / "release/exiftool/exiftool.exe"
    config = executable.parent.parent / "exiftool-lumina.config"
    if not executable.is_file():
        pytest.skip("locked ExifTool runtime not staged")
    delivery = tmp_path / "中文交付"
    delivery.mkdir()
    target = delivery / "精选照片.jpg"
    Image.new("RGB", (32, 32), "green").save(target)

    result = MetadataWriter(executable, delivery, config_path=config).write(
        target,
        XmpSelection(5, "Green", ("旅行",), "0.5.0", "heuristic", ()),
    )

    assert result.success, result
    assert target.is_file()
    assert not list(delivery.glob(".lumina-*"))


def test_smoke_payload_starts_analyzes_and_exports(tmp_path, monkeypatch):
    from engine import config

    monkeypatch.setenv("LUMINA_INFERENCE_BACKEND", "heuristic")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setattr(config, "INFERENCE_BACKEND", "heuristic")
    report = run_smoke_payload(tmp_path, backend="heuristic")
    assert report["started"]
    assert report["analyzed"] == 1
    assert report["face_detection_available"]
    assert report["exported"] == 1
    assert report["source_unchanged"]


def test_bundle_audit_rejects_forbidden_runtime(tmp_path):
    internal = tmp_path / "_internal"
    (internal / "torch").mkdir(parents=True)
    assert "torch" in audit_bundle(tmp_path, "heuristic")
    (internal / "torch").rmdir()
    assert audit_bundle(tmp_path, "heuristic") == []
