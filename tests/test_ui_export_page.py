from __future__ import annotations

from pathlib import Path
from threading import Event

from PyQt6.QtWidgets import QMessageBox

from engine.store import PhotoStore
from services.asset_pairing import pair_assets
from ui.pages.export_page import ExportPage
from ui.main_window import MainWindow


def _setup(tmp_path, names=("IMG_0001.CR3", "IMG_0001.JPG")):
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
                store.upsert_photo({
                    "path": str(path), "fname": path.name,
                    "asset_pair_id": asset.asset_id,
                    "asset_role": "raw" if path.suffix.casefold() == ".cr3" else "jpeg",
                    "star": 5, "label": "P", "decision_source": "manual",
                })
    return store, assets


def test_default_mode_and_preview_counts(qtbot, tmp_path):
    store, assets = _setup(tmp_path)
    page = ExportPage(store, assets=assets)
    qtbot.addWidget(page)
    try:
        assert page.write_mode == "copy"
        assert page.asset_mode == "raw+jpeg"
        assert not page.in_place_check.isChecked()
        page.target_edit.setText(str(tmp_path / "delivery"))
        plan = page.refresh_plan()
        assert (plan.asset_count, plan.file_count, plan.xmp_count) == (1, 2, 2)
        assert plan.total_bytes == sum(path.stat().st_size for path in (tmp_path / "source").iterdir())
        assert "2 个文件" in page.preview_label.text()
    finally:
        store.close()


def test_nonempty_target_warning_and_in_place_impact(qtbot, tmp_path, monkeypatch):
    store, assets = _setup(tmp_path)
    delivery = tmp_path / "delivery"
    delivery.mkdir()
    (delivery / "unrelated.txt").write_text("keep", encoding="utf-8")
    page = ExportPage(store, assets=assets)
    qtbot.addWidget(page)
    page.target_edit.setText(str(delivery))
    try:
        page.refresh_plan()
        assert "非空" in page.warning_label.text()
        assert page.impact_label.isHidden()
        page.in_place_check.setChecked(True)
        page.refresh_plan()
        assert "IMG_0001.xmp" in page.impact_label.text()
        confirmations = []
        monkeypatch.setattr(
            QMessageBox, "question",
            lambda *_args, **_kwargs: confirmations.append(True) or QMessageBox.StandardButton.No,
        )
        page.start_export()
        assert confirmations == [True]
        assert page.worker is None
        assert not (tmp_path / "source" / "IMG_0001.xmp").exists()
    finally:
        store.close()


def test_background_export_result_and_open_button(qtbot, tmp_path):
    store, assets = _setup(tmp_path, names=("IMG_0001.CR3",))
    page = ExportPage(store, assets=assets)
    qtbot.addWidget(page)
    page.target_edit.setText(str(tmp_path / "delivery"))
    page.asset_mode_combo.setCurrentText("仅 RAW")
    try:
        with qtbot.waitSignal(page.export_finished, timeout=5000):
            page.start_export()
            assert not page.start_button.isEnabled()
            assert page.cancel_button.isEnabled()
        assert (tmp_path / "delivery" / "IMG_0001.CR3").exists()
        assert (tmp_path / "delivery" / "IMG_0001.xmp").exists()
        assert "成功 1" in page.result_label.text()
        assert page.open_button.isEnabled()
        assert not page.retry_button.isEnabled()
    finally:
        page.shutdown()
        store.close()


def test_retry_button_appears_only_for_failures(qtbot, tmp_path):
    store, assets = _setup(tmp_path, names=("IMG_0001.CR3",))
    delivery = tmp_path / "delivery"
    delivery.mkdir()
    (delivery / "IMG_0001.CR3").write_bytes(b"conflict")
    page = ExportPage(store, assets=assets)
    qtbot.addWidget(page)
    page.target_edit.setText(str(delivery))
    page.asset_mode_combo.setCurrentText("仅 RAW")
    try:
        with qtbot.waitSignal(page.export_finished, timeout=5000):
            page.start_export()
        assert "失败 1" in page.result_label.text()
        assert page.retry_button.isEnabled()
        assert not page.open_button.isEnabled()
    finally:
        page.shutdown()
        store.close()


def test_retry_button_retries_only_failed_item(qtbot, tmp_path):
    store, assets = _setup(tmp_path, names=("IMG_0001.CR3", "IMG_0002.CR3"))
    delivery = tmp_path / "delivery"
    delivery.mkdir()
    conflict = delivery / "IMG_0001.CR3"
    conflict.write_bytes(b"conflict")
    page = ExportPage(store, assets=assets)
    qtbot.addWidget(page)
    page.target_edit.setText(str(delivery))
    page.asset_mode_combo.setCurrentText("仅 RAW")
    try:
        with qtbot.waitSignal(page.export_finished, timeout=5000):
            page.start_export()
        assert page.retry_button.isEnabled()
        completed = delivery / "IMG_0002.CR3"
        assert completed.exists()
        original_bytes = completed.read_bytes()
        conflict.unlink()

        with qtbot.waitSignal(page.export_finished, timeout=5000) as finished:
            page.retry_button.click()
        assert len(finished.args[0]) == 1
        assert finished.args[0][0].status == "success"
        assert conflict.exists()
        assert completed.read_bytes() == original_bytes
        assert not page.retry_button.isEnabled()
    finally:
        page.shutdown()
        store.close()


def test_reload_sees_assets_added_after_first_empty_visit(qtbot, tmp_path):
    store = PhotoStore(str(tmp_path / "review.db"), enable_wal=False)
    page = ExportPage(store)
    qtbot.addWidget(page)
    page.target_edit.setText(str(tmp_path / "delivery"))
    try:
        page.reload()
        assert page.refresh_plan().asset_count == 0
        source = tmp_path / "new.CR3"
        source.write_bytes(b"new-raw")
        store.upsert_photo({
            "path": str(source), "fname": source.name,
            "asset_pair_id": "new-asset", "asset_role": "raw",
            "star": 5, "label": "P", "decision_source": "manual",
        })
        page.reload()
        assert page.refresh_plan().asset_count == 1
    finally:
        store.close()


def test_rejected_close_keeps_review_workspace_alive(qtbot, tmp_path):
    window = MainWindow(project_db=tmp_path / "review.db")
    qtbot.addWidget(window)

    class UnfinishedWorker:
        cancelled = False

        def isRunning(self):
            return True

        def cancel(self):
            self.cancelled = True

        def wait(self, _timeout):
            return False

    worker = UnfinishedWorker()
    window.export_page.worker = worker
    window.close()

    assert worker.cancelled
    assert window.review_page._stopped is False
    window.export_page.worker = None
    window.close()
