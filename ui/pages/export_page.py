"""Lightroom delivery preview and background export workflow."""
from __future__ import annotations

from pathlib import Path
import sys

from PyQt6.QtCore import QThread, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from engine.store import PhotoStore
from services.asset_pairing import AssetPair, assets_from_rows
from services.export_service import ExportPlan, ExportRequest, ExportService
from services.metadata_writer import MetadataWriter


MODE_LABELS = {
    "仅 RAW": "raw",
    "RAW+JPEG": "raw+jpeg",
    "仅 JPEG": "jpeg",
}


def _metadata_writer(target_dir: Path) -> MetadataWriter:
    if getattr(sys, "frozen", False):
        runtime_root = Path(getattr(sys, "_MEIPASS"))
        tool = runtime_root / "exiftool" / "exiftool.exe"
        config = runtime_root / "exiftool-lumina.config"
    else:
        runtime_root = Path(__file__).resolve().parents[2] / "release"
        tool = runtime_root / "exiftool" / "exiftool.exe"
        config = runtime_root / "exiftool-lumina.config"
    return MetadataWriter(tool, target_dir, config_path=config)


class ExportWorker(QThread):
    progress = pyqtSignal(int, int)
    completed = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(
        self,
        db_path: str,
        assets: list[AssetPair],
        request: ExportRequest,
        *,
        retry: bool = False,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.db_path = db_path
        self.assets = list(assets)
        self.request = request
        self.retry = retry
        self._cancel_requested = False

    def cancel(self) -> None:
        self._cancel_requested = True
        self.requestInterruption()

    def run(self) -> None:
        try:
            with PhotoStore(self.db_path) as store:
                service = ExportService(
                    store,
                    self.assets,
                    metadata_writer=_metadata_writer(self.request.target_dir),
                )
                progress = lambda done, total: self.progress.emit(done, total)
                cancel_check = lambda: self._cancel_requested or self.isInterruptionRequested()
                if self.retry:
                    results = service.retry_failed(
                        self.request, progress=progress, cancel_check=cancel_check
                    )
                else:
                    results = service.run(
                        self.request, progress=progress, cancel_check=cancel_check
                    )
            self.completed.emit(tuple(results))
        except Exception as error:
            self.failed.emit(str(error))


class ExportPage(QWidget):
    export_finished = pyqtSignal(object)

    def __init__(
        self,
        store: PhotoStore,
        *,
        assets: list[AssetPair] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.store = store
        self._assets_injected = assets is not None
        self.assets = list(assets) if assets is not None else None
        self.worker: ExportWorker | None = None
        self._request: ExportRequest | None = None
        self._last_plan: ExportPlan | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)
        title = QLabel("导出到 Lightroom Classic")
        title.setObjectName("PageTitle")
        layout.addWidget(title)
        subtitle = QLabel("默认复制到新的交付目录；相机 RAW 使用 sidecar，JPEG/TIFF/PSD/DNG 仅写导出副本。")
        subtitle.setObjectName("PageSub")
        layout.addWidget(subtitle)
        target_row = QHBoxLayout()
        self.target_edit = QLineEdit()
        self.target_edit.setPlaceholderText("选择新的或空的交付目录")
        self.target_edit.textChanged.connect(lambda _text: self.refresh_plan())
        browse_button = QPushButton("浏览…")
        browse_button.clicked.connect(self._browse)
        target_row.addWidget(self.target_edit, 1)
        target_row.addWidget(browse_button)
        layout.addLayout(target_row)

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("资产模式"))
        self.asset_mode_combo = QComboBox()
        self.asset_mode_combo.addItems(MODE_LABELS)
        self.asset_mode_combo.setCurrentText("RAW+JPEG")
        self.asset_mode_combo.currentTextChanged.connect(lambda _text: self.refresh_plan())
        mode_row.addWidget(self.asset_mode_combo)
        self.in_place_check = QCheckBox("高级：原地写入相机 RAW 的 XMP sidecar")
        self.in_place_check.toggled.connect(lambda _checked: self.refresh_plan())
        mode_row.addWidget(self.in_place_check)
        mode_row.addStretch(1)
        layout.addLayout(mode_row)

        self.preview_label = QLabel("请选择交付目录以预览")
        self.warning_label = QLabel("")
        self.impact_label = QLabel("")
        self.impact_label.hide()
        layout.addWidget(self.preview_label)
        layout.addWidget(self.warning_label)
        layout.addWidget(self.impact_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        layout.addWidget(self.progress_bar)
        controls = QHBoxLayout()
        self.start_button = QPushButton("开始导出")
        self.start_button.setObjectName("Primary")
        self.start_button.clicked.connect(self.start_export)
        self.cancel_button = QPushButton("安全取消")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_export)
        self.retry_button = QPushButton("只重试失败项")
        self.retry_button.setEnabled(False)
        self.retry_button.clicked.connect(lambda: self.start_export(retry=True))
        self.open_button = QPushButton("打开交付目录")
        self.open_button.setEnabled(False)
        self.open_button.clicked.connect(self._open_target)
        for button in (
            self.start_button, self.cancel_button, self.retry_button, self.open_button
        ):
            controls.addWidget(button)
        controls.addStretch(1)
        layout.addLayout(controls)
        self.result_label = QLabel("尚未导出")
        layout.addWidget(self.result_label)
        layout.addStretch(1)

    @property
    def write_mode(self) -> str:
        return "in-place-xmp" if self.in_place_check.isChecked() else "copy"

    @property
    def asset_mode(self) -> str:
        return MODE_LABELS[self.asset_mode_combo.currentText()]

    def reload(self) -> None:
        if not self._assets_injected:
            self.assets = assets_from_rows(self.store.review_snapshot()["photos"])
        self.refresh_plan()

    def _request_from_ui(self) -> ExportRequest:
        target_text = self.target_edit.text().strip()
        return ExportRequest(
            target_dir=Path(target_text) if target_text else Path(),
            asset_mode=self.asset_mode,
            write_mode=self.write_mode,
            selected_asset_ids=tuple(asset.asset_id for asset in self.assets or ()),
        )

    def refresh_plan(self) -> ExportPlan | None:
        if self.assets is None:
            self.assets = assets_from_rows(self.store.review_snapshot()["photos"])
        if not self.target_edit.text().strip() and self.write_mode == "copy":
            self.preview_label.setText("请选择交付目录以预览")
            self.warning_label.clear()
            self.impact_label.hide()
            return None
        request = self._request_from_ui()
        plan = ExportService(self.store, self.assets).plan(request)
        self._last_plan = plan
        self.preview_label.setText(
            f"{plan.asset_count} 个资产 · {plan.file_count} 个文件 · "
            f"{plan.xmp_count} 项 XMP · {plan.total_bytes} 字节"
        )
        warnings = []
        if plan.nonempty_target:
            warnings.append("目标目录非空")
        if plan.conflicts:
            warnings.append(f"{len(plan.conflicts)} 个同名目标冲突")
        self.warning_label.setText("；".join(warnings))
        if self.write_mode == "in-place-xmp":
            paths = "、".join(str(path) for path in plan.affected_paths[:5])
            self.impact_label.setText(f"将影响 RAW sidecar：{paths or '无可写 RAW'}")
            self.impact_label.show()
        else:
            self.impact_label.hide()
        return plan

    def start_export(self, *, retry: bool = False) -> None:
        if self.worker is not None and self.worker.isRunning():
            return
        if self.write_mode == "copy" and not self.target_edit.text().strip():
            self.result_label.setText("请选择交付目录")
            return
        request = self._request_from_ui()
        if self.write_mode == "in-place-xmp":
            response = QMessageBox.question(
                self,
                "确认原地 sidecar 写入",
                "将先备份已有 RAW sidecar，再原子写入源文件旁。非 RAW 源文件不会修改。继续吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if response != QMessageBox.StandardButton.Yes:
                return
        self._request = request
        self.progress_bar.setValue(0)
        self.start_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.retry_button.setEnabled(False)
        self.open_button.setEnabled(False)
        self.result_label.setText("正在导出…")
        worker = ExportWorker(self.store.db_path, self.assets or [], request, retry=retry, parent=self)
        self.worker = worker
        worker.progress.connect(self._on_progress)
        worker.completed.connect(self._on_completed)
        worker.failed.connect(self._on_failed)
        worker.start()

    def cancel_export(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            self.worker.cancel()
            self.result_label.setText("正在当前文件完成后安全取消…")

    def _on_progress(self, done: int, total: int) -> None:
        self.progress_bar.setValue(round(done * 100 / total) if total else 0)

    def _on_completed(self, results: tuple) -> None:
        successes = sum(item.status == "success" for item in results)
        skipped = sum(item.status == "skipped" for item in results)
        failures = sum(item.status == "failed" for item in results)
        self.result_label.setText(
            f"成功 {successes} · 跳过 {skipped} · 失败 {failures}"
        )
        self.start_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.retry_button.setEnabled(failures > 0)
        self.open_button.setEnabled(
            bool(self._request)
            and self._request.write_mode == "copy"
            and any(item.status == "success" and item.target and item.target.exists() for item in results)
        )
        self.export_finished.emit(results)

    def _on_failed(self, message: str) -> None:
        self.result_label.setText(f"导出失败：{message}")
        self.start_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.export_finished.emit(())

    def _browse(self) -> None:
        target = QFileDialog.getExistingDirectory(self, "选择交付目录")
        if target:
            self.target_edit.setText(target)

    def _open_target(self) -> None:
        if self.open_button.isEnabled() and self._request is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._request.target_dir)))

    def shutdown(self) -> bool:
        worker = self.worker
        if worker is not None and worker.isRunning():
            worker.cancel()
            return worker.wait(5000)
        return True
