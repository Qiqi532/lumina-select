"""Background analysis progress and cancellation.

【v0.6 S2-1】这里原先还有一个「改用 OpenCV 轻量后端」的回退按钮，做法是把后端写进
进程级 `config.INFERENCE_BACKEND`。轻量后端已随双版本机制一起下线，后端改为
`analyze_directory(..., backend=...)` 的**显式参数**，UI 不再持有任何后端全局状态。
`backend` 透传保留，作为 v0.7 接入 ONNX 后端时的唯一入口。
"""
from __future__ import annotations

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from engine.log import get_logger

# 【v0.6 S3-8】worker 里的异常过去只把 str(error) 抛给界面 —— 用户看到一句话，
# 日志里却没有任何 traceback，出问题后无法复盘。这里补上完整栈。
_log = get_logger("ui.analyze")


class AnalyzeWorker(QThread):
    progress = Signal(str, int, int)
    succeeded = Signal(dict)
    failed = Signal(str)
    cancelled = Signal()

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
            from engine.pipeline import analyze_directory

            # 后端是调用参数，不再是进程全局（v0.6 S2-1）。
            result = analyze_directory(
                self.source,
                self.db_path,
                include_raw=True,
                use_faces=True,
                progress_cb=lambda phase, done, total: self.progress.emit(phase, done, total),
                cancel_check=lambda: self._cancel_requested or self.isInterruptionRequested(),
                backend=self.backend,
            )
            if self._cancel_requested or (result and result.get("cancelled")):
                self.cancelled.emit()
            else:
                self.succeeded.emit(result or {})
        except Exception as error:
            _log.exception("分析失败（源目录 %s）：%s", self.source, error)
            self.failed.emit(str(error))


class AnalyzePage(QWidget):
    succeeded = Signal(dict)
    failed = Signal(str)
    cancelled = Signal()

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
        row.addWidget(self.cancel_button)
        row.addStretch(1)
        panel_layout.addLayout(row)
        layout.addWidget(panel)
        layout.addStretch(1)

    def start(self, source: str, db_path: str, *, backend: str | None = None) -> None:
        if self.worker is not None and self.worker.isRunning():
            raise RuntimeError("analysis is already running")
        self.stage_label.setText("准备分析…")
        self.progress_bar.setValue(0)
        self.cancel_button.setEnabled(True)
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
        self.stage_label.setText("分析失败；可重试")
        self.failed.emit(message)

    def _on_cancelled(self) -> None:
        self.cancel_button.setEnabled(False)
        self.stage_label.setText("分析已取消，可重新开始")
        self.cancelled.emit()

    def cancel(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            self.worker.cancel()
            self.stage_label.setText("正在安全取消…")
