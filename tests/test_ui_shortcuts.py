from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QLineEdit, QVBoxLayout, QWidget

from ui.review.shortcuts import COMMAND_KEYS, install_review_shortcuts


def test_all_review_keys_have_stable_commands_and_tooltips(qtbot):
    host = QWidget()
    qtbot.addWidget(host)
    calls = []
    actions = install_review_shortcuts(host, lambda command: calls.append(command))

    expected_keys = {
        "P", "X", "0", "1", "2", "3", "4", "5", "A", "B", "C", "D",
        "Left", "Right", "Tab", "F", "I", "Ctrl+Z",
    }
    assert set(COMMAND_KEYS) == expected_keys
    assert len(actions) == len(expected_keys)
    for key, action in actions.items():
        assert action.objectName().startswith("review.")
        assert action.toolTip()
        assert action.shortcut().toString() == key
        assert action.shortcutContext() == Qt.ShortcutContext.WidgetWithChildrenShortcut
        action.trigger()
        assert calls[-1] == COMMAND_KEYS[key]


def test_input_focus_does_not_trigger_culling_commands(qtbot):
    host = QWidget()
    layout = QVBoxLayout(host)
    edit = QLineEdit()
    layout.addWidget(edit)
    qtbot.addWidget(host)
    host.show()
    actions = install_review_shortcuts(host, lambda command: calls.append(command))
    calls = []
    edit.setFocus()
    qtbot.waitUntil(edit.hasFocus)

    actions["P"].trigger()
    actions["Ctrl+Z"].trigger()
    assert calls == []
