"""Directory selection intent; project ownership stays in MainWindow."""
from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class ContentSafeButton(QPushButton):
    def sizeHint(self):
        hint = super().sizeHint()
        hint.setWidth(max(hint.width(), self.fontMetrics().horizontalAdvance(self.text()) + 32))
        return hint


class ImportPage(QWidget):
    source_selected = pyqtSignal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)
        title = QLabel("导入照片")
        title.setObjectName("PageTitle")
        subtitle = QLabel("选择本地照片目录。原片不会被移动或修改。")
        subtitle.setObjectName("PageSub")
        layout.addWidget(title)
        layout.addWidget(subtitle)

        panel = QFrame()
        panel.setObjectName("Panel")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(24, 24, 24, 24)
        panel_layout.setSpacing(12)
        panel_layout.addWidget(QLabel("照片目录"))
        self.source_edit = QLineEdit()
        self.source_edit.setPlaceholderText("选择包含 RAW、JPEG 或其他照片的目录")
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.addWidget(self.source_edit, 1)
        row.addWidget(browse)
        panel_layout.addLayout(row)
        self.primary_button = ContentSafeButton("导入并开始分析")
        self.primary_button.setObjectName("Primary")
        self.primary_button.clicked.connect(self._submit)
        panel_layout.addWidget(self.primary_button)
        layout.addWidget(panel)
        layout.addStretch(1)

    def _browse(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "选择照片目录")
        if directory:
            self.source_edit.setText(directory)

    def _submit(self) -> None:
        source = self.source_edit.text().strip()
        if source:
            self.source_selected.emit(source)
