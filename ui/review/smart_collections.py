"""智能集合面板（T-09）。

只负责"展示 + 收集意图"，真正的写库与撤销交给命令表（N1）。

一个刻意的取舍
--------------
集合列表里**每行都显示实时计数**，而不是存一个数字。计数会随分析新照片、
改星级而变化；显示一个过期数字会让用户以为集合"坏了"。宁可每次多算一次
（实测毫秒级），也不要显示假数据。
"""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class CollectionPanel(QWidget):
    """集合列表 + 保存/应用/删除。"""

    save_requested = Signal()
    applied = Signal(dict)
    delete_requested = Signal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("CollectionPanel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(4)

        title = QLabel("智能集合")
        title.setObjectName("SectionTitle")
        layout.addWidget(title)

        self.list_widget = QListWidget()
        self.list_widget.setObjectName("CollectionList")
        self.list_widget.setToolTip("点某个集合即可把审片范围切进去（计数实时计算）")
        self.list_widget.itemClicked.connect(self._item_clicked)
        layout.addWidget(self.list_widget, 1)

        row = QHBoxLayout()
        self.save_button = QPushButton("存当前筛选…")
        self.save_button.setObjectName("BatchFlag")
        self.save_button.setToolTip("把当前的星级/机型/焦距等条件存成可复用的集合（Ctrl+B）")
        self.save_button.clicked.connect(self.save_requested.emit)
        self.delete_button = QPushButton("删除")
        self.delete_button.setObjectName("DangerButton")
        self.delete_button.setEnabled(False)
        self.delete_button.clicked.connect(self._delete_current)
        row.addWidget(self.save_button)
        row.addWidget(self.delete_button)
        row.addStretch(1)
        layout.addLayout(row)

        self._rows: list[dict] = []

    def refresh(self, rows: list[dict]) -> None:
        self._rows = list(rows)
        self.list_widget.clear()
        for row in rows:
            item = QListWidgetItem(f"{row['name']}  ·  {row['count']} 张")
            item.setData(1000, int(row["id"]))
            note = str(row.get("description_text") or "")
            item.setToolTip(note or "（全部照片）")
            self.list_widget.addItem(item)
        self.delete_button.setEnabled(False)

    def _item_clicked(self, item: QListWidgetItem) -> None:
        collection_id = int(item.data(1000) or 0)
        self.delete_button.setEnabled(collection_id > 0)
        row = next((r for r in self._rows if int(r["id"]) == collection_id), None)
        if row is None:
            return
        self.applied.emit(
            {
                "kind": "collection",
                "id": collection_id,
                "name": str(row["name"]),
                "paths": list(row.get("paths") or ()),
            }
        )

    def _delete_current(self) -> None:
        item = self.list_widget.currentItem()
        if item is None:
            return
        self.delete_requested.emit(int(item.data(1000) or 0))

    def current_id(self) -> int | None:
        item = self.list_widget.currentItem()
        if item is None:
            return None
        return int(item.data(1000) or 0)


__all__ = ["CollectionPanel"]
