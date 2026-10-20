"""批量操作条 —— 多选之后的"执行闭环"（T-10）。

放在这里而不是塞进 ReviewPage，是因为它**只负责收集意图**：点了什么按钮、
选了几张。真正的写库由 ReviewService 走命令表完成（N1）。这样它可以被
单独实例化做单测，不必拖起整个页面。

一个设计取舍
------------
"清理已排除项"放在这条上而不是导出页，是因为它的语义是**整理当前工作目录**，
和"选中 → 批量处理"是同一条动作链；但它只针对**人工判废**（label == "X"），
与当前选中无关 —— 所以按钮上必须把张数写清楚，避免用户以为它在清理"选中的"。
"""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QWidget,
)

STAR_CHOICES: tuple[tuple[int, str], ...] = (
    (0, "0 星（取消保留）"),
    (1, "1 星"),
    (2, "2 星"),
    (3, "3 星"),
    (4, "4 星"),
    (5, "5 星"),
)

FLAG_CHOICES: tuple[tuple[str, str], ...] = (
    ("P", "保留 (P)"),
    ("X", "排除 (X)"),
    ("U", "清标记"),
)


class BatchBar(QWidget):
    """多选批量操作条。"""

    star_requested = Signal(int)
    flag_requested = Signal(str)
    select_all_requested = Signal()
    clear_requested = Signal()
    cleanup_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("BatchBar")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(6)

        self.count_label = QLabel("未选择")
        self.count_label.setObjectName("BatchCount")
        layout.addWidget(self.count_label)

        self.select_all_button = QPushButton("全选")
        self.select_all_button.setObjectName("BatchLink")
        self.select_all_button.setToolTip("选中当前筛选结果里的全部照片")
        self.select_all_button.clicked.connect(self.select_all_requested.emit)
        layout.addWidget(self.select_all_button)

        self.clear_button = QPushButton("取消选择")
        self.clear_button.setObjectName("BatchLink")
        self.clear_button.clicked.connect(self.clear_requested.emit)
        layout.addWidget(self.clear_button)

        divider = QLabel("|")
        divider.setObjectName("BatchMuted")
        layout.addWidget(divider)

        for flag, text in FLAG_CHOICES:
            button = QPushButton(text)
            button.setObjectName("BatchFlag")
            button.setProperty("flag", flag)
            button.setToolTip(self._flag_tooltip(flag))
            button.clicked.connect(
                lambda _checked=False, value=flag: self.flag_requested.emit(value)
            )
            layout.addWidget(button)
            setattr(self, f"flag_{flag}_button", button)

        self.star_combo = QComboBox()
        self.star_combo.setObjectName("BatchStar")
        for value, text in STAR_CHOICES:
            self.star_combo.addItem(text, value)
        self.star_combo.setCurrentIndex(5)
        self.star_combo.setToolTip("给选中的照片统一标星；0 星会取消「保留」")
        layout.addWidget(self.star_combo)

        self.star_button = QPushButton("标星")
        self.star_button.setObjectName("BatchStarApply")
        self.star_button.clicked.connect(self._emit_star)
        layout.addWidget(self.star_button)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout.addWidget(spacer)

        self.rejected_label = QLabel("已判废 0 张")
        self.rejected_label.setObjectName("BatchMuted")
        self.rejected_label.setToolTip(
            "仅统计人工判废（标记为 X）的照片；AI 判废但你看都没看过的不会被清理"
        )
        layout.addWidget(self.rejected_label)

        self.cleanup_button = QPushButton("清理已排除项…")
        self.cleanup_button.setObjectName("DangerButton")
        self.cleanup_button.setToolTip(
            "把人工判废的照片移进项目内 .trash_<日期> 隔离目录，可随时搬回（不会真删）"
        )
        self.cleanup_button.clicked.connect(self.cleanup_requested.emit)
        layout.addWidget(self.cleanup_button)

        self._selection_count = 0
        self._apply_enabled()

    @staticmethod
    def _flag_tooltip(flag: str) -> str:
        if flag == "P":
            return "保留：记 5 星并标记 P，进入交付清单"
        if flag == "X":
            return "排除：记 0 星并标记 X，不进入交付清单"
        return "只清掉 P/X 标记，星级保持不变"

    def _emit_star(self) -> None:
        value = self.star_combo.currentData()
        self.star_requested.emit(int(value) if value is not None else 0)

    def _apply_enabled(self) -> None:
        """没选中任何照片时，所有"作用于选中"的按钮必须不可用。

        否则用户点了"保留"却什么都没发生 —— 那比按钮置灰更让人困惑。
        """
        has_selection = self._selection_count > 0
        for flag, _text in FLAG_CHOICES:
            getattr(self, f"flag_{flag}_button").setEnabled(has_selection)
        self.star_combo.setEnabled(has_selection)
        self.star_button.setEnabled(has_selection)

    def set_selection_count(self, count: int) -> None:
        self._selection_count = int(count)
        self.count_label.setText(
            "未选择" if count == 0 else f"已选 {count} 张"
        )
        self.clear_button.setEnabled(count > 0)
        self._apply_enabled()

    def set_rejected_count(self, count: int) -> None:
        self.rejected_label.setText(f"已判废 {int(count)} 张")
        # 清理与选中无关，只取决于有没有人工判废的照片。
        self.cleanup_button.setEnabled(int(count) > 0)

    def set_total_count(self, count: int) -> None:
        self.select_all_button.setEnabled(int(count) > 0)


__all__ = ["BatchBar", "FLAG_CHOICES", "STAR_CHOICES"]
