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
from engine.inference import DEFAULT_BACKEND


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
        XmpSelection(5, "Green", (), "0.5.0", "torch", ()),
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
        XmpSelection(5, "Green", ("旅行",), "0.5.0", "torch", ()),
    )

    assert result.success, result
    assert target.is_file()
    assert not list(delivery.glob(".lumina-*"))


def test_smoke_payload_starts_analyzes_and_exports(tmp_path, monkeypatch):
    """【v0.6 S2-1】冒烟走**发行默认后端**（注入参数已随轻量版下线）。

    单版本之后，冒烟要验证的就是用户实际会跑到的那条路径，因此不再把后端钉成轻量版，
    report["backend"] 必须等于 inference.DEFAULT_BACKEND。本用例在 conftest 强制的
    HF_HUB_OFFLINE 下运行，因此它同时是"断网首启可完成分析"（G3）的一条现场验证。
    """
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    report = run_smoke_payload(tmp_path)
    assert report["started"]
    assert report["backend"] == DEFAULT_BACKEND
    assert report["analyzed"] == 1
    # 本机模型缓存完整 ⇒ 不应出现任何降级（这条同时守住"本地齐全即只读本地"的接线）
    assert report["degradations"] == []
    assert report["face_detection_available"]
    assert report["exported"] == 1
    assert report["source_unchanged"]


def test_bundle_audit_requires_torch_and_rejects_pyiqa(tmp_path):
    """【v0.6 S2-1】只剩一套产物规则：torch/transformers 必须存在，pyiqa 绝不出现。

    【T-02】audit_bundle 现在同时审计 LGPLv3 物料，因此夹具需补齐
    `_internal/` 下的 LGPL-3.0.txt / GPL-3.0.txt / RELINKING.md，
    否则光靠 torch/transformers 无法清零。
    """
    internal = tmp_path / "_internal"
    internal.mkdir()
    for name in ("LGPL-3.0.txt", "GPL-3.0.txt", "RELINKING.md"):
        (internal / name).write_text("text", encoding="utf-8")
    assert audit_bundle(tmp_path) == ["missing:torch", "missing:transformers"]

    (internal / "torch").mkdir()
    (internal / "transformers").mkdir()
    assert audit_bundle(tmp_path) == []

    (internal / "pyiqa").mkdir()
    assert audit_bundle(tmp_path) == ["pyiqa"]


def test_bundle_audit_flags_missing_lgpl_materials(tmp_path):
    """【T-02】缺任一 LGPL 物料（全文/替换说明）都必须被判失败。"""
    internal = tmp_path / "_internal"
    internal.mkdir()
    (internal / "torch").mkdir()
    (internal / "transformers").mkdir()

    issues = audit_bundle(tmp_path)

    assert issues == [
        "missing_lgpl:LGPL-3.0.txt",
        "missing_lgpl:GPL-3.0.txt",
        "missing_lgpl:RELINKING.md",
    ]


def test_bundle_audit_accepts_lgpl_materials_in_release_root(tmp_path):
    """【T-02】Inno Setup 安装后物料在根目录（非 `_internal`）也应通过。"""
    internal = tmp_path / "_internal"
    internal.mkdir()
    (internal / "torch").mkdir()
    (internal / "transformers").mkdir()
    for name in ("LGPL-3.0.txt", "GPL-3.0.txt", "RELINKING.md"):
        (tmp_path / name).write_text("text", encoding="utf-8")

    assert audit_bundle(tmp_path) == []
