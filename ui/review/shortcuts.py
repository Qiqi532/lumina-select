"""Scoped QAction command table for keyboard-first culling."""
from __future__ import annotations

from collections.abc import Callable

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QAction, QKeySequence
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QLineEdit,
    QPlainTextEdit,
    QSpinBox,
    QTextEdit,
    QWidget,
)


COMMAND_KEYS: dict[str, str] = {
    "P": "pick",
    "X": "reject",
    **{str(star): f"star-{star}" for star in range(6)},
    **{letter: f"candidate-{letter.lower()}" for letter in "ABCD"},
    "Left": "previous",
    "Right": "next",
    "Tab": "toggle-view",
    "F": "fullscreen",
    "I": "toggle-inspector",
    "Ctrl+Z": "undo",
}

COMMAND_TOOLTIPS: dict[str, str] = {
    "pick": "保留当前资产",
    "reject": "排除当前资产",
    "previous": "上一张",
    "next": "下一张",
    "toggle-view": "切换单张/比较视图",
    "fullscreen": "切换全屏",
    "toggle-inspector": "显示或隐藏技术面板",
    "undo": "撤销上一步人工决定",
}


def _is_editing_text() -> bool:
    focused = QApplication.focusWidget()
    return isinstance(
        focused, (QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox, QComboBox)
    )


def install_review_shortcuts(
    host: QWidget, dispatch: Callable[[str], None]
) -> dict[str, QAction]:
    actions: dict[str, QAction] = {}
    for key, command in COMMAND_KEYS.items():
        action = QAction(host)
        action.setObjectName(f"review.{command}")
        action.setShortcut(QKeySequence(key))
        action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        action.setToolTip(
            COMMAND_TOOLTIPS.get(
                command,
                "选择候选" if command.startswith("candidate-") else "设置星级",
            )
        )
        action.triggered.connect(
            lambda _checked=False, selected=command: None
            if _is_editing_text()
            else dispatch(selected)
        )
        host.addAction(action)
        actions[key] = action
    return actions
