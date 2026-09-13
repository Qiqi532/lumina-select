"""Background analysis progress, cancellation, and explicit backend fallback."""
from __future__ import annotations

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class AnalyzeWorker(QThread):
    progress = pyqtSignal(str, int, int)
    succeeded = pyqtSignal(dict)
    failed = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(self, source: str, db_path: str, *, backend: str | None = None, parent=None):
        super().__init__(parent)
        self.source = source
        self.db_path = db_path
        self.backend = backend
        self._cancel_requested = False

    def cancel(self) -> None:
        self._cancel_requested = True
        self.requestInterruption()

    def run(self) -> None:
        try:
            from engine import config
            from engine.pipeline import analyze_directory

            if self.backend is not None:
                config.INFERENCE_BACKEND = self.backend
            result = analyze_directory(
                self.source,
                self.db_path,
                use_faces=True,
                progress_cb=lambda phase, done, total: self.progress.emit(phase, done, total),
                cancel_check=lambda: self._cancel_requested or self.isInterruptionRequested(),
            )
            if self._cancel_requested or (result and result.get("cancelled")):
                self.cancelled.emit()
            else:
                self.succeeded.emit(result or {})
        except Exception as error:
            self.failed.emit(str(error))


class AnalyzePage(QWidget):
    succeeded = pyqtSignal(dict)
    failed = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.worker: AnalyzeWorker | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)
        title = QLabel("分析照片")
        title.setObjectName("PageTitle")
        layout.addWidget(title)
        self.stage_label = QLabel("等待选择照片目录")
        self.stage_label.setObjectName("PageSub")
        layout.addWidget(self.stage_label)
        panel = QFrame()
        panel.setObjectName("Panel")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(24, 24, 24, 24)
        panel_layout.setSpacing(12)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        panel_layout.addWidget(self.progress_bar)
        row = QHBoxLayout()
        self.cancel_button = QPushButton("取消分析")
        self.cancel_button.setObjectName("Danger")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel)
        self.fallback_button = QPushButton("改用 OpenCV 轻量后端")
        self.fallback_button.setEnabled(False)
        row.addWidget(self.cancel_button)
        row.addWidget(self.fallback_button)
        row.addStretch(1)
        panel_layout.addLayout(row)
        layout.addWidget(panel)
        layout.addStretch(1)
        self._last_source: str | None = None
        self._last_db: str | None = None
        self.fallback_button.clicked.connect(self._retry_heuristic)

    def start(self, source: str, db_path: str, *, backend: str | None = None) -> None:
        if self.worker is not None and self.worker.isRunning():
            raise RuntimeError("analysis is already running")
        self._last_source, self._last_db = source, db_path
        self.stage_label.setText("准备分析…")
        self.progress_bar.setValue(0)
        self.cancel_button.setEnabled(True)
        self.fallback_button.setEnabled(False)
        worker = AnalyzeWorker(source, db_path, backend=backend, parent=self)
        self.worker = worker
        worker.progress.connect(self._on_progress)
        worker.succeeded.connect(self._on_success)
        worker.failed.connect(self._on_failure)
        worker.cancelled.connect(self._on_cancelled)
        worker.start()

    def _on_progress(self, phase: str, done: int, total: int) -> None:
        self.stage_label.setText(f"{phase} · {done}/{total}")
        self.progress_bar.setValue(round(done * 100 / total) if total else 0)

    def _on_success(self, result: dict) -> None:
        self.cancel_button.setEnabled(False)
        self.stage_label.setText("分析完成，等待人工复核")
        self.succeeded.emit(result)

    def _on_failure(self, message: str) -> None:
        self.cancel_button.setEnabled(False)
        self.fallback_button.setEnabled(True)
        self.stage_label.setText("分析失败；可重试或切换轻量后端")
        self.failed.emit(message)

    def _on_cancelled(self) -> None:
        self.cancel_button.setEnabled(False)
        self.stage_label.setText("分析已取消，可重新开始")
        self.cancelled.emit()

    def _retry_heuristic(self) -> None:
        if self._last_source and self._last_db:
            self.start(self._last_source, self._last_db, backend="heuristic")

    def cancel(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            self.worker.cancel()
            self.stage_label.setText("正在安全取消…")
