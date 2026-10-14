"""Scoped QAction command table for keyboard-first culling."""
from __future__ import annotations

from collections.abc import Callable, Mapping

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QLineEdit,
    QPlainTextEdit,
    QSpinBox,
    QTextEdit,
    QWidget,
)

from services.shortcut_settings import COMMAND_LABELS, COMMAND_NOTES, DEFAULT_BINDINGS

# ---------------------------------------------------------------------------
# 既有键位（v0.6 起在用）—— 保持原样不动，避免改坏肌肉记忆。
# ---------------------------------------------------------------------------
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


def _tooltip_for(command: str) -> str:
    if command in COMMAND_TOOLTIPS:
        return COMMAND_TOOLTIPS[command]
    label = COMMAND_LABELS.get(command, command)
    note = COMMAND_NOTES.get(command, "")
    if command.startswith("star-"):
        return f"设为 {command[-1]} 星"
    if command.startswith("candidate-"):
        return f"跳到本组第 {ord(command[-1].upper()) - ord('A') + 1} 张候选"
    return f"{label}（{note}）" if note else label


def install_review_shortcuts(
    host: QWidget,
    dispatch: Callable[[str], None],
    *,
    bindings: Mapping[str, str] | None = None,
) -> dict[str, QAction]:
    """在 ``host`` 上装一批 QAction，返回 ``{按键: QAction}``。

    ``bindings`` 为 ``{命令: 按键}``（来自 :mod:`services.shortcut_settings`）。
    不传时沿用 :data:`COMMAND_KEYS` 的既有 18 个键位 —— 这是为了不破坏
    已有行为；**新键位（Ctrl+F / Ctrl+K / Space …）只有在显式传入 bindings
    时才生效**，因为调用方必须先把对应命令接上，否则按下没反应更糟。
    """
    if bindings is None:
        pairs = [(key, command) for key, command in COMMAND_KEYS.items()]
    else:
        pairs = [
            (key, command)
            for command, key in bindings.items()
            if command in DEFAULT_BINDINGS
        ]
    actions: dict[str, QAction] = {}
    for key, command in pairs:
        action = QAction(host)
        action.setObjectName(f"review.{command}")
        action.setShortcut(QKeySequence(key))
        action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        action.setToolTip(_tooltip_for(command))
        action.triggered.connect(
            lambda _checked=False, selected=command: None
            if _is_editing_text()
            else dispatch(selected)
        )
        host.addAction(action)
        actions[key] = action
    return actions
