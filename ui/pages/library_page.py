"""项目入口页（库）—— T-05 / M0-5。

【目标】把"一次向导"变成"可持续的库"：用户重开 App 后，从这一页看到自己
做过的项目（名称/路径/照片数/最近打开），点一下即回到该项目继续工作；
也可以新建项目、从已有文件夹打开、把不再需要的条目从列表移除。

【边界（Q5 约束）】本页**只负责展示与发意图**，不自己打开库里、不持有
PhotoStore。真正的"打开项目"由 MainWindow（Shell）完成 —— 这样四个既有
工作流 Page 的内部实现完全不用动，只是被 Shell 挂到内容区。
"""
from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from engine.catalog_registry import CatalogEntry, read_photo_count

_COLUMNS = 3


def _human_time(epoch: float) -> str:
    """epoch 秒 → 简短相对时间（入口页只需"多久以前"这个量级）。"""
    if not epoch:
        return "从未打开"
    delta = max(0.0, time.time() - float(epoch))
    if delta < 60:
        return "刚刚打开"
    if delta < 3600:
        return f"{int(delta // 60)} 分钟前"
    if delta < 86400:
        return f"{int(delta // 3600)} 小时前"
    if delta < 86400 * 30:
        return f"{int(delta // 86400)} 天前"
    return time.strftime("%Y-%m-%d", time.localtime(float(epoch)))


class ProjectCard(QFrame):
    """单个项目的卡片。点击卡片本体 = 打开；角标按钮 = 移除登记。"""

    open_requested = Signal(str)     # 项目库路径
    forget_requested = Signal(str)   # 项目库路径

    def __init__(self, entry: CatalogEntry, parent=None) -> None:
        super().__init__(parent)
        self.entry = entry
        self.setObjectName("ProjectCard")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setMinimumWidth(200)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(6)

        top = QHBoxLayout()
        name = QLabel(entry.name or Path(entry.path).stem)
        name.setObjectName("CardTitle")
        name.setWordWrap(True)
        top.addWidget(name, 1)
        forget = QPushButton("移除")
        forget.setObjectName("CardForget")
        forget.setToolTip("只从列表移除登记，不会删除项目文件或照片")
        forget.setFixedHeight(22)
        forget.clicked.connect(lambda: self.forget_requested.emit(entry.path))
        top.addWidget(forget, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(top)

        path_label = QLabel(entry.path)
        path_label.setObjectName("Muted")
        path_label.setWordWrap(True)
        path_label.setToolTip(entry.path)
        layout.addWidget(path_label)

        meta = QLabel(f"{entry.photo_count} 张照片 · {_human_time(entry.opened_at)}")
        meta.setObjectName("CardMeta")
        layout.addWidget(meta)

        if not entry.exists:
            warn = QLabel("库文件已不在原位置")
            warn.setObjectName("CardWarn")
            warn.setToolTip("可能已被移动或删除；点卡片可重新指定位置")
            layout.addWidget(warn)

        open_button = QPushButton("打开项目")
        open_button.setObjectName("Primary")
        open_button.clicked.connect(lambda: self.open_requested.emit(entry.path))
        layout.addWidget(open_button)

        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        self.open_requested.emit(self.entry.path)
        super().mouseDoubleClickEvent(event)


class LibraryPage(QWidget):
    """项目入口页：卡片网格 + 新建/从文件夹打开。"""

    open_project = Signal(str)        # 打开已登记项目的库路径
    create_project = Signal(str)      # 新建项目：用户选择的目标文件夹
    forget_project = Signal(str)      # 从列表移除登记

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.setSpacing(16)

        head = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(2)
        title = QLabel("我的项目")
        title.setObjectName("PageTitle")
        subtitle = QLabel("照片只在本机处理；这里记录你打开过的项目，方便随时回来继续。")
        subtitle.setObjectName("PageSub")
        titles.addWidget(title)
        titles.addWidget(subtitle)
        head.addLayout(titles, 1)
        self.new_button = QPushButton("新建项目")
        self.new_button.setObjectName("Primary")
        self.new_button.clicked.connect(self._choose_new)
        self.open_dir_button = QPushButton("从文件夹打开…")
        self.open_dir_button.clicked.connect(self._choose_open)
        head.addWidget(self.open_dir_button, 0, Qt.AlignmentFlag.AlignBottom)
        head.addWidget(self.new_button, 0, Qt.AlignmentFlag.AlignBottom)
        outer.addLayout(head)

        self.empty_hint = QLabel("还没有项目。点「新建项目」开始第一次选片，或「从文件夹打开…」。")
        self.empty_hint.setObjectName("Muted")
        self.empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(self.empty_hint)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._grid_host = QWidget()
        self.grid = QGridLayout(self._grid_host)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(12)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.scroll.setWidget(self._grid_host)
        outer.addWidget(self.scroll, 1)

        self.cards: list[ProjectCard] = []

    # ------------------------------------------------------------------
    # 渲染
    # ------------------------------------------------------------------
    def show_entries(self, entries: list[CatalogEntry]) -> None:
        """用给定项目列表重建卡片网格（由 Shell 调用，页面自己不查库）。"""
        for card in self.cards:
            card.setParent(None)
            card.deleteLater()
        self.cards.clear()
        while self.grid.count():
            item = self.grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)

        self.empty_hint.setVisible(not entries)
        self.scroll.setVisible(bool(entries))

        for index, entry in enumerate(entries):
            card = ProjectCard(entry)
            card.open_requested.connect(self.open_project)
            card.forget_requested.connect(self.forget_project)
            self.grid.addWidget(card, index // _COLUMNS, index % _COLUMNS)
            self.cards.append(card)

    def refresh_counts(self) -> None:
        """就地刷新每张卡片的照片数（只读查询，不创建库）。"""
        for card in self.cards:
            count = read_photo_count(card.entry.path)
            if count is None or count == card.entry.photo_count:
                continue
            card.entry.photo_count = count
            for child in card.findChildren(QLabel):
                if child.objectName() == "CardMeta":
                    child.setText(f"{count} 张照片 · {_human_time(card.entry.opened_at)}")
                    break

    # ------------------------------------------------------------------
    # 意图
    # ------------------------------------------------------------------
    def _choose_new(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "选择新建项目的文件夹")
        if directory:
            self.create_project.emit(directory)

    def _choose_open(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "选择包含项目库的文件夹")
        if directory:
            self.open_project.emit(directory)
