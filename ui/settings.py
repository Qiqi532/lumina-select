"""设置对话框（T-11）—— 快捷键重绑定。

两个刻意的设计
--------------
1. **改键用"抓取"而不是手输**。让用户手写 ``Ctrl+Shift+Alt+K`` 既难写也容易
   写错；按住修饰键再按主键，抓到什么就是什么，所见即所得。
2. **冲突当场拦下并指名道姓**。不写"快捷键冲突"这种废话，而是说清
   "『保留』和『全屏』撞用了同一个按键 P" —— 用户才知道该去改哪一个。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QKeySequenceEdit,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from services.shortcut_settings import (
    COMMAND_LABELS,
    COMMAND_NOTES,
    COMMAND_TABLE,
    DEFAULT_BINDINGS,
    RESERVED_KEYS,
    ShortcutConflict,
    normalize_key,
    occupied_by,
)


class ShortcutEditor(QDialog):
    """单条命令的按键编辑（模态）。"""

    def __init__(self, command: str, current: str, bindings: dict, parent=None) -> None:
        super().__init__(parent)
        self.command = command
        self.bindings = dict(bindings)
        self.setWindowTitle(f"修改快捷键 · {COMMAND_LABELS.get(command, command)}")
        self.setModal(True)

        layout = QVBoxLayout(self)
        note = COMMAND_NOTES.get(command, "")
        if note:
            hint = QLabel(note)
            hint.setObjectName("SettingsHint")
            layout.addWidget(hint)

        layout.addWidget(QLabel("按下新的组合键（Esc 取消）："))
        self.editor = QKeySequenceEdit(QKeySequence(current))
        layout.addWidget(self.editor)
        self.editor.keySequenceChanged.connect(self._preview_conflict)

        self.conflict_label = QLabel("")
        self.conflict_label.setObjectName("SettingsConflict")
        self.conflict_label.setWordWrap(True)
        layout.addWidget(self.conflict_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("确定")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._preview_conflict()

    def accept(self) -> None:
        """重写 accept，而不是只在 Ok 按钮上挂槽。

        若只挂按钮，任何**编程式** ``accept()``（例如回车确认、未来从别处
        触发）都会绕过提交逻辑，静默丢掉用户改的键 —— 正是最难受的那类 bug。
        """
        self._commit()
        super().accept()

    def _current_text(self) -> str:
        return normalize_key(self.editor.keySequence().toString())

    def _preview_conflict(self) -> None:
        """实时提示冲突 —— 不要等按下"确定"才报错。"""
        key = self._current_text()
        if not key:
            self.conflict_label.setText("")
            return
        other = occupied_by(self.bindings, key)
        if other and other != self.command:
            self.conflict_label.setText(
                f"「{key}」当前已被「{COMMAND_LABELS.get(other, other)}」占用，"
                "确定后会从它那里夺过来。"
            )
        elif key in RESERVED_KEYS and DEFAULT_BINDINGS.get(self.command) != key:
            self.conflict_label.setText(
                f"注意：「{key}」是选片常用裸键，改绑后原来的动作会失效。"
            )
        else:
            self.conflict_label.setText("")

    def _commit(self) -> None:
        key = self._current_text()
        if key:
            # 冲突在这里**允许通过**，但必须让用户确认过（上面已实时提示）。
            # 占用方的键会被清空，避免出现"一键两用"的幽灵行为。
            other = occupied_by(self.bindings, key)
            if other and other != self.command:
                self.bindings[other] = ""
        self.bindings[self.command] = key

    def result_bindings(self) -> dict:
        return self.bindings


class SettingsDialog(QDialog):
    """快捷键设置。"""

    bindings_changed = Signal(dict)

    def __init__(self, bindings: dict, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("设置 · 快捷键")
        self.resize(560, 520)
        self._bindings = dict(DEFAULT_BINDINGS)
        self._bindings.update(bindings or {})

        layout = QVBoxLayout(self)
        intro = QLabel(
            "点击某一行再点「修改」，按下新的组合键即可。"
            "设置保存在本机，重启后仍然生效。"
        )
        intro.setObjectName("SettingsHint")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.table = QTableWidget(len(COMMAND_TABLE), 3)
        self.table.setHorizontalHeaderLabels(["动作", "快捷键", "说明"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setColumnWidth(0, 140)
        self.table.setColumnWidth(1, 150)
        self.table.itemDoubleClicked.connect(lambda _item: self._edit_current())
        self._fill_table()
        layout.addWidget(self.table, 1)

        row = QHBoxLayout()
        self.edit_button = QPushButton("修改…")
        self.edit_button.clicked.connect(self._edit_current)
        self.reset_button = QPushButton("恢复默认")
        self.reset_button.clicked.connect(self._reset_all)
        row.addWidget(self.edit_button)
        row.addWidget(self.reset_button)
        row.addStretch(1)
        layout.addLayout(row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("确定")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _fill_table(self) -> None:
        for index, (command, _default, label, note) in enumerate(COMMAND_TABLE):
            name_item = QTableWidgetItem(label)
            key_item = QTableWidgetItem(self._bindings.get(command, ""))
            key_item.setToolTip("双击可修改")
            note_item = QTableWidgetItem(note or "")
            for item in (name_item, key_item, note_item):
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            name_item.setData(Qt.ItemDataRole.UserRole, command)
            self.table.setItem(index, 0, name_item)
            self.table.setItem(index, 1, key_item)
            self.table.setItem(index, 2, note_item)

    def _current_command(self) -> str | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _edit_current(self) -> None:
        command = self._current_command()
        if command is None:
            QMessageBox.information(self, "提示", "请先选择一行")
            return
        dialog = ShortcutEditor(
            command, self._bindings.get(command, ""), self._bindings, self
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._bindings = dialog.result_bindings()
            self._fill_table()

    def _reset_all(self) -> None:
        answer = QMessageBox.question(
            self,
            "恢复默认",
            "把所有快捷键恢复为默认值？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._bindings = dict(DEFAULT_BINDINGS)
            self._fill_table()

    def bindings(self) -> dict:
        return dict(self._bindings)


def open_settings(bindings: dict, parent: QWidget | None = None) -> dict | None:
    """打开设置对话框；返回新的绑定，取消则返回 ``None``。"""
    dialog = SettingsDialog(bindings, parent)
    if dialog.exec() == QDialog.DialogCode.Accepted:
        return dialog.bindings()
    return None


__all__ = ["SettingsDialog", "ShortcutEditor", "open_settings"]
