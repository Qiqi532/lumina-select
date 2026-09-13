from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil

import pytest

from engine.store import PhotoStore
from services.asset_pairing import pair_assets
from services.export_service import ExportRequest, ExportService


@dataclass
class _WriteResult:
    success: bool
    error_code: str | None = None
    message: str | None = None


class _MetadataWriter:
    def __init__(self, *, fail: bool = False):
        self.calls: list[Path] = []
        self.fail = fail

    def write(self, path, _selection, *, in_place=False):
        self.calls.append(Path(path))
        if self.fail:
            return _WriteResult(False, "xmp_invalid", "metadata write failed")
        return _WriteResult(True)


def _setup(tmp_path, names=("IMG_0001.CR3", "IMG_0001.JPG"), *, label="P", star=5):
    source = tmp_path / "source"
    source.mkdir()
    paths = []
    for name in names:
        path = source / name
        path.write_bytes(name.encode("ascii"))
        paths.append(path)
    assets = pair_assets(paths, lambda _path: 1000.0)
    store = PhotoStore(str(tmp_path / "review.db"), enable_wal=False)
    for asset in assets:
        for path in (asset.raw_path, asset.jpeg_path):
            if path is not None:
                store.upsert_photo(
                    {
                        "path": str(path),
                        "fname": path.name,
                        "asset_pair_id": asset.asset_id,
                        "star": star,
                        "label": label,
                        "decision_source": "manual" if label == "P" else None,
                    }
                )
    return source, assets, store


def _request(tmp_path, assets, mode="raw+jpeg", write_mode="copy"):
    return ExportRequest(
        target_dir=tmp_path / "delivery",
        asset_mode=mode,
        write_mode=write_mode,
        selected_asset_ids=tuple(asset.asset_id for asset in assets),
    )


@pytest.mark.parametrize(
    ("mode", "expected_names"),
    [
        ("raw", {"IMG_0001.CR3", "IMG_0001.xmp"}),
        ("jpeg", {"IMG_0001.JPG"}),
        ("raw+jpeg", {"IMG_0001.CR3", "IMG_0001.JPG", "IMG_0001.xmp"}),
    ],
)
def test_default_export_copies_only_requested_members(tmp_path, mode, expected_names):
    source, assets, store = _setup(tmp_path)
    metadata = _MetadataWriter()
    service = ExportService(store, assets, metadata_writer=metadata)
    try:
        results = service.run(_request(tmp_path, assets, mode))
        assert {item.status for item in results} == {"success"}
        assert {path.name for path in (tmp_path / "delivery").iterdir()} == expected_names
        assert {path.name for path in source.iterdir()} == {"IMG_0001.CR3", "IMG_0001.JPG"}
        assert len(metadata.calls) == (0 if mode == "raw" else 1)
        assert not list((tmp_path / "delivery").glob("*.lumina-part"))
    finally:
        store.close()


@pytest.mark.parametrize(
    ("label", "star", "decision_source", "expected"),
    [
        (None, 0, None, "not_reviewed"),
        ("X", 5, "manual", "excluded"),
        (None, 4, None, "not_reviewed"),
        ("P", 1, "manual", None),
    ],
)
def test_selection_requires_explicit_review_and_never_exports_x(
    tmp_path, label, star, decision_source, expected
):
    _, assets, store = _setup(tmp_path, names=("IMG_0001.CR3",), label=label, star=star)
    store.update_photo(str(assets[0].raw_path), decision_source=decision_source)
    try:
        results = ExportService(store, assets).run(_request(tmp_path, assets, "raw"))
        assert len(results) == 1
        if expected is None:
            assert results[0].status == "success"
        else:
            assert results[0].status == "skipped"
            assert results[0].error_code == expected
            assert not (tmp_path / "delivery" / "IMG_0001.CR3").exists()
    finally:
        store.close()


def test_insufficient_space_fails_before_any_copy(tmp_path):
    _, assets, store = _setup(tmp_path)
    copied = []

    def copy(source, target):
        copied.append(source)
        return shutil.copy2(source, target)

    service = ExportService(
        store,
        assets,
        metadata_writer=_MetadataWriter(),
        disk_usage=lambda _path: shutil._ntuple_diskusage(100, 99, 1),
        copier=copy,
    )
    try:
        results = service.run(_request(tmp_path, assets))
        assert copied == []
        assert {item.error_code for item in results} == {"disk_full"}
        assert not (tmp_path / "delivery" / "IMG_0001.CR3").exists()
    finally:
        store.close()


def test_existing_target_is_reported_without_overwrite(tmp_path):
    _, assets, store = _setup(tmp_path, names=("IMG_0001.CR3",))
    delivery = tmp_path / "delivery"
    delivery.mkdir()
    existing = delivery / "IMG_0001.CR3"
    existing.write_bytes(b"user-existing-file")
    try:
        results = ExportService(store, assets).run(_request(tmp_path, assets, "raw"))
        assert results[0].error_code == "target_conflict"
        assert results[0].status == "failed"
        assert existing.read_bytes() == b"user-existing-file"
    finally:
        store.close()


def test_single_copy_failure_does_not_stop_other_items(tmp_path):
    _, assets, store = _setup(tmp_path, names=("IMG_0001.CR3", "IMG_0002.CR3"))

    def copy(source, target):
        if source.name == "IMG_0001.CR3":
            raise PermissionError("blocked")
        return shutil.copy2(source, target)

    try:
        results = ExportService(store, assets, copier=copy).run(
            _request(tmp_path, assets, "raw")
        )
        assert {item.source.name: item.status for item in results} == {
            "IMG_0001.CR3": "failed",
            "IMG_0002.CR3": "success",
        }
        assert next(item for item in results if item.status == "failed").error_code == "permission_denied"
        assert (tmp_path / "delivery" / "IMG_0002.CR3").exists()
    finally:
        store.close()


def test_xmp_failure_removes_new_copy_and_is_recorded(tmp_path):
    _, assets, store = _setup(tmp_path, names=("IMG_0001.CR3",))

    def fail_xmp(path, _selection, **_kwargs):
        return _WriteResult(False, "xmp_invalid", "invalid sidecar")

    try:
        results = ExportService(store, assets, xmp_writer=fail_xmp).run(
            _request(tmp_path, assets, "raw")
        )
        assert results[0].status == "failed"
        assert results[0].error_code == "xmp_invalid"
        assert not (tmp_path / "delivery" / "IMG_0001.CR3").exists()
        assert not (tmp_path / "delivery" / "IMG_0001.xmp").exists()
        rows = store.failed_export_items(str(tmp_path / "delivery"))
        assert len(rows) == 1
        assert rows[0]["error"] == "xmp_invalid"
    finally:
        store.close()


def test_progress_is_monotonic_and_retry_only_failed_member(tmp_path):
    _, assets, store = _setup(tmp_path, names=("IMG_0001.CR3", "IMG_0002.CR3"))
    calls = []
    failed_once = {"IMG_0001.xmp"}

    def flaky_xmp(path, selection, **kwargs):
        calls.append(Path(path).name)
        if Path(path).name in failed_once:
            failed_once.clear()
            return _WriteResult(False, "xmp_invalid", "transient")
        from services.xmp_service import write_sidecar

        return write_sidecar(path, selection, **kwargs)

    progress = []
    request = _request(tmp_path, assets, "raw")
    service = ExportService(store, assets, xmp_writer=flaky_xmp)
    try:
        first = service.run(request, progress=lambda done, total: progress.append((done, total)))
        assert [item.status for item in first].count("failed") == 1
        assert [done for done, _total in progress] == sorted(done for done, _total in progress)
        assert progress[-1] == (2, 2)

        retry = service.retry_failed(request)
        assert len(retry) == 1
        assert retry[0].status == "success"
        assert calls.count("IMG_0002.xmp") == 1
        assert store.failed_export_items(str(request.target_dir)) == []
    finally:
        store.close()


def test_in_place_rejects_non_raw_without_touching_source(tmp_path):
    source, assets, store = _setup(tmp_path)
    before = (source / "IMG_0001.JPG").read_bytes()
    try:
        results = ExportService(store, assets).run(
            _request(tmp_path, assets, "jpeg", "in-place-xmp")
        )
        assert results[0].error_code == "unsupported_in_place_non_raw"
        assert (source / "IMG_0001.JPG").read_bytes() == before
    finally:
        store.close()


def test_failed_embedded_xmp_does_not_leave_delivery_copy(tmp_path):
    source, assets, store = _setup(tmp_path, names=("IMG_0001.JPG",))
    original = (source / "IMG_0001.JPG").read_bytes()
    try:
        results = ExportService(
            store, assets, metadata_writer=_MetadataWriter(fail=True)
        ).run(_request(tmp_path, assets, "jpeg"))
        assert results[0].error_code == "xmp_invalid"
        assert not (tmp_path / "delivery" / "IMG_0001.JPG").exists()
        assert (source / "IMG_0001.JPG").read_bytes() == original
    finally:
        store.close()


def test_in_place_raw_failure_can_retry_same_request(tmp_path):
    source, assets, store = _setup(tmp_path, names=("IMG_0001.CR3",))
    failed_once = True

    def flaky_xmp(path, selection, **kwargs):
        nonlocal failed_once
        if failed_once:
            failed_once = False
            return _WriteResult(False, "xmp_invalid", "transient")
        from services.xmp_service import write_sidecar

        return write_sidecar(path, selection, **kwargs)

    request = _request(tmp_path, assets, "raw", "in-place-xmp")
    service = ExportService(store, assets, xmp_writer=flaky_xmp)
    try:
        first = service.run(request)
        assert first[0].status == "failed"
        assert not (source / "IMG_0001.xmp").exists()
        retry = service.retry_failed(request)
        assert len(retry) == 1
        assert retry[0].status == "success"
        assert (source / "IMG_0001.xmp").exists()
    finally:
        store.close()


def test_export_plan_reports_preview_and_nonempty_target(tmp_path):
    _, assets, store = _setup(tmp_path)
    delivery = tmp_path / "delivery"
    delivery.mkdir()
    (delivery / "unrelated.txt").write_bytes(b"existing")
    try:
        plan = ExportService(store, assets, metadata_writer=_MetadataWriter()).plan(
            _request(tmp_path, assets)
        )
        assert (plan.asset_count, plan.file_count, plan.xmp_count) == (1, 2, 2)
        assert plan.total_bytes > 0
        assert plan.nonempty_target is True
    finally:
        store.close()


def test_cancel_after_atomic_item_skips_remainder(tmp_path):
    _, assets, store = _setup(tmp_path, names=("IMG_0001.CR3", "IMG_0002.CR3"))
    cancel = False

    def on_progress(done, _total):
        nonlocal cancel
        if done == 1:
            cancel = True

    try:
        results = ExportService(store, assets).run(
            _request(tmp_path, assets, "raw"),
            progress=on_progress,
            cancel_check=lambda: cancel,
        )
        assert [item.status for item in results] == ["success", "skipped"]
        assert results[1].error_code == "cancelled"
        assert sum(path.suffix == ".CR3" for path in (tmp_path / "delivery").iterdir()) == 1
    finally:
        store.close()
