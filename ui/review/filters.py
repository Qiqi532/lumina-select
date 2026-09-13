"""Review filter intent and count display, without SQL or state mutation."""
from __future__ import annotations

from collections.abc import Mapping

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget

from services.review_service import ReviewFilter


LABELS = {
    ReviewFilter.ALL: "全部照片",
    ReviewFilter.AI_PICKS: "AI 精选",
    ReviewFilter.UNCERTAIN: "待确认",
    ReviewFilter.REJECTED: "已排除",
    ReviewFilter.TECHNICAL_ISSUE: "技术问题",
    ReviewFilter.STAR: "星级筛选",
}


class FilterSidebar(QWidget):
    filter_selected = pyqtSignal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 12, 8, 12)
        title = QLabel("筛选")
        title.setObjectName("PageTitle")
        layout.addWidget(title)
        self.buttons: dict[ReviewFilter, QPushButton] = {}
        for filter_name, label in LABELS.items():
            button = QPushButton(label)
            button.setObjectName(f"filter-{filter_name.value}")
            button.clicked.connect(
                lambda _checked=False, selected=filter_name: self.filter_selected.emit(selected)
            )
            self.buttons[filter_name] = button
            layout.addWidget(button)
        layout.addStretch(1)

    def set_counts(self, counts: Mapping[ReviewFilter, int]) -> None:
        for filter_name, button in self.buttons.items():
            button.setText(f"{LABELS[filter_name]}  {counts.get(filter_name, 0)}")
