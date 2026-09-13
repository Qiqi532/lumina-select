"""Professional four-stage shell and project lifecycle."""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from engine import config
from engine.store import PhotoStore
from .pages.analyze_page import AnalyzePage
from .pages.export_page import ExportPage
from .pages.import_page import ImportPage
from .pages.review_page import ReviewPage


STAGES = (
    ("import", "1 导入"),
    ("analyze", "2 分析"),
    ("review", "3 复核"),
    ("export", "4 导出到 LR"),
)


class MainWindow(QMainWindow):
    def __init__(self, *, project_db: str | Path | None = None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Lumina Select · 光影选片助手")
        self.resize(1280, 800)
        self.setMinimumSize(960, 600)
        self.store = PhotoStore(str(project_db or config.DEFAULT_DB))
        self.current_stage_name = "import"

        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(16, 16, 16, 8)
        root_layout.setSpacing(12)
        header = QFrame()
        header.setObjectName("WorkflowHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 12, 16, 12)
        brand = QLabel("LUMINA SELECT")
        brand.setObjectName("PageTitle")
        header_layout.addWidget(brand)
        header_layout.addStretch(1)
        self.stage_buttons: list[QPushButton] = []
        for name, label in STAGES:
            button = QPushButton(label)
            button.setObjectName("StageButton")
            button.clicked.connect(lambda _checked=False, stage=name: self.goto(stage))
            header_layout.addWidget(button)
            self.stage_buttons.append(button)
        root_layout.addWidget(header)
        self.stage_state_label = QLabel()
        self.stage_state_label.setObjectName("StageState")
        root_layout.addWidget(self.stage_state_label)

        self.stack = QStackedWidget()
        self.import_page = ImportPage()
        self.analyze_page = AnalyzePage()
        self.review_page = ReviewPage(self.store)
        self.export_page = ExportPage(self.store)
        self.pages = {
            "import": self.import_page,
            "analyze": self.analyze_page,
            "review": self.review_page,
            "export": self.export_page,
        }
        for page in self.pages.values():
            self.stack.addWidget(page)
        root_layout.addWidget(self.stack, 1)
        self.setCentralWidget(root)
        self.statusBar().showMessage("项目已保存 · 照片仅在本机处理")

        self.import_page.source_selected.connect(self.begin_analysis)
        self.analyze_page.succeeded.connect(self._analysis_succeeded)
        self.analyze_page.failed.connect(self.show_error)
        self.analyze_page.cancelled.connect(lambda: self.status("分析已取消"))
        self.goto("import")

    @property
    def current_page(self) -> QWidget:
        return self.stack.currentWidget()

    def goto(self, name: str) -> None:
        if name not in self.pages:
            raise ValueError(f"unknown workflow stage: {name}")
        self.current_stage_name = name
        if name == "review":
            self.review_page.reload()
        elif name == "export":
            self.export_page.reload()
        self.stack.setCurrentWidget(self.pages[name])
        current_index = list(self.pages).index(name)
        for index, button in enumerate(self.stage_buttons):
            state = "current" if index == current_index else "done" if index < current_index else "future"
            button.setProperty("state", state)
            button.style().unpolish(button)
            button.style().polish(button)
        self.stage_state_label.setText(f"当前：{STAGES[current_index][1]} · 项目已保存")

    def begin_analysis(self, source: str) -> None:
        source_path = Path(source)
        if not source_path.is_dir():
            self.show_error("请选择存在的照片目录")
            return
        self.store.set_meta("source_dir", str(source_path))
        self.goto("analyze")
        self.analyze_page.start(str(source_path), self.store.db_path)

    def _analysis_succeeded(self, result: dict) -> None:
        self.goto("review")
        self.status(f"分析完成：{result.get('total', 0)} 张，等待人工复核")

    def status(self, message: str) -> None:
        self.statusBar().showMessage(message)

    def show_error(self, message: str) -> None:
        self.statusBar().showMessage(f"错误：{message}")

    def closeEvent(self, event) -> None:
        worker = self.analyze_page.worker
        if worker is not None and worker.isRunning():
            worker.cancel()
            if not worker.wait(5000):
                self.show_error("分析仍在安全取消中，请稍后再退出")
                event.ignore()
                return
        if not self.export_page.shutdown():
            self.show_error("导出仍在完成当前文件，请稍后再退出")
            event.ignore()
            return
        self.review_page.shutdown()
        self.store.close()
        super().closeEvent(event)
