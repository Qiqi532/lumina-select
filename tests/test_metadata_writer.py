from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from services.metadata_writer import MetadataWriter
from services.xmp_service import XmpSelection, write_sidecar


def _selection() -> XmpSelection:
    return XmpSelection(
        rating=4,
        label="Green",
        keywords=("旅行", "精选"),
        app_version="0.5.0",
        backend="heuristic",
        reasons=("best-in-group",),
    )


class FakeRunner:
    def __init__(self, *, write_returncode=0, readback=None, timeout=False):
        self.calls = []
        self.args_file_text = None
        self.write_returncode = write_returncode
        self.readback = readback if readback is not None else {
            "Rating": 4,
            "Label": "Green",
            "Subject": ["旅行", "精选"],
        }
        self.timeout = timeout

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        if "-@" in args:
            args_path = Path(kwargs["cwd"]) / args[args.index("-@") + 1]
            self.args_file_text = args_path.read_text(encoding="utf-8")
        if self.timeout:
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])
        if "-j" in args:
            return subprocess.CompletedProcess(args, 0, json.dumps([self.readback]), "")
        return subprocess.CompletedProcess(args, self.write_returncode, "", "write failed")


@pytest.mark.parametrize("suffix", [".jpg", ".tiff", ".psd", ".dng"])
def test_writes_internal_xmp_only_to_delivery_copy(tmp_path, suffix):
    delivery = tmp_path / "交付目录"
    delivery.mkdir()
    target = delivery / f"照片一号{suffix}"
    target.write_bytes(b"original-export-bytes")
    runner = FakeRunner()
    writer = MetadataWriter(tmp_path / "tools" / "exiftool.exe", delivery, runner=runner)

    result = writer.write(target, _selection())

    assert result.success is True
    assert target.read_bytes() == b"original-export-bytes"
    assert len(runner.calls) == 2
    write_args, write_kwargs = runner.calls[0]
    assert write_args[0].endswith("exiftool.exe")
    assert "-charset" in write_args and "filename=utf8" in write_args
    assert "-@" in write_args
    args_file = Path(write_kwargs["cwd"]) / write_args[write_args.index("-@") + 1]
    assert args_file.name.isascii()
    assert write_kwargs["cwd"] == delivery.resolve()
    assert "交付目录" in str(target)
    assert "照片一号" not in runner.args_file_text
    assert runner.args_file_text.splitlines()[-1].isascii()
    assert "旅行" in runner.args_file_text
    assert "-XMP-xmp:Rating=4" in runner.args_file_text
    assert write_kwargs["shell"] is False
    assert write_kwargs["timeout"] == 30
    assert isinstance(write_kwargs["creationflags"], int)
    assert not args_file.exists()


def test_raw_sidecar_and_jpeg_internal_metadata_do_not_collide(tmp_path):
    source = tmp_path / "source"
    delivery = tmp_path / "delivery"
    source.mkdir()
    delivery.mkdir()
    raw = source / "IMG_0001.CR3"
    jpeg_copy = delivery / "IMG_0001.JPG"
    raw.write_bytes(b"raw")
    jpeg_copy.write_bytes(b"jpeg")

    xmp_result = write_sidecar(delivery / "IMG_0001.xmp", _selection())
    writer = MetadataWriter(tmp_path / "exiftool.exe", delivery, runner=FakeRunner())
    jpeg_result = writer.write(jpeg_copy, _selection())

    assert xmp_result.success and jpeg_result.success
    assert sorted(path.name for path in delivery.glob("*.xmp")) == ["IMG_0001.xmp"]
    assert raw.read_bytes() == b"raw"


def test_rejects_in_place_non_raw_without_modifying_source(tmp_path):
    source = tmp_path / "source.jpg"
    original = b"source-image"
    source.write_bytes(original)
    writer = MetadataWriter(tmp_path / "exiftool.exe", tmp_path / "delivery", runner=FakeRunner())

    result = writer.write(source, _selection(), in_place=True)

    assert result.success is False
    assert result.error_code == "unsupported_in_place_non_raw"
    assert source.read_bytes() == original


@pytest.mark.parametrize(
    ("runner", "error_code"),
    [
        (FakeRunner(timeout=True), "timeout"),
        (FakeRunner(write_returncode=2), "exiftool_failed"),
        (FakeRunner(readback={"Rating": 1, "Label": "Red", "Subject": []}), "verification_failed"),
    ],
)
def test_failures_preserve_existing_delivery_copy(tmp_path, runner, error_code):
    delivery = tmp_path / "delivery"
    delivery.mkdir()
    target = delivery / "photo.jpg"
    original = b"existing-delivery-copy"
    target.write_bytes(original)
    writer = MetadataWriter(tmp_path / "exiftool.exe", delivery, runner=runner)

    result = writer.write(target, _selection())

    assert result.success is False
    assert result.error_code == error_code
    assert target.read_bytes() == original
    assert not list(delivery.glob(".lumina-meta-*"))


def test_rejects_file_outside_delivery_root(tmp_path):
    delivery = tmp_path / "delivery"
    delivery.mkdir()
    source = tmp_path / "source.jpg"
    source.write_bytes(b"source")
    writer = MetadataWriter(tmp_path / "exiftool.exe", delivery, runner=FakeRunner())

    result = writer.write(source, _selection())

    assert result.success is False
    assert result.error_code == "outside_delivery_root"
    assert source.read_bytes() == b"source"
