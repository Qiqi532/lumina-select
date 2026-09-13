"""Five-zone keyboard-first review workspace wired exclusively to services."""
from __future__ import annotations

from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from engine.store import PhotoStore
from services.review_service import ReviewFilter, ReviewService
from ui.review.canvas import CanvasWidget
from ui.review.filmstrip import FilmstripModel, FilmstripView
from ui.review.filters import FilterSidebar
from ui.review.inspector import InspectorWidget
from ui.review.shortcuts import install_review_shortcuts


class ReviewPage(QWidget):
    def __init__(
        self,
        store: PhotoStore,
        *,
        image_loader=None,
        thumbnail_loader=None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.review = ReviewService(store)
        self._updating = False
        self._stopped = False
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(250)
        self._save_timer.timeout.connect(self.save_splitter_sizes)

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.sidebar = FilterSidebar()
        self.sidebar.setMinimumWidth(160)
        self.sidebar.setMaximumWidth(200)
        self.sidebar.filter_selected.connect(self._filter_selected)
        self.splitter.addWidget(self.sidebar)

        middle = QWidget()
        middle_layout = QVBoxLayout(middle)
        middle_layout.setContentsMargins(4, 0, 4, 0)
        middle_layout.setSpacing(8)
        self.canvas = CanvasWidget(
            review_service=self.review,
            **({"image_loader": image_loader} if image_loader is not None else {}),
        )
        self.canvas.image_ready.connect(self._preview_ready)
        middle_layout.addWidget(self.canvas, 1)
        self.filmstrip_model = FilmstripModel(
            self.review.queue,
            **({"thumbnail_loader": thumbnail_loader} if thumbnail_loader is not None else {}),
        )
        self.filmstrip = FilmstripView(self.filmstrip_model)
        self.filmstrip.setFixedHeight(126)
        self.filmstrip.selectionModel().currentChanged.connect(self._filmstrip_selected)
        middle_layout.addWidget(self.filmstrip)
        footer = QHBoxLayout()
        self.progress_label = QLabel("0 / 0")
        self.hint_label = QLabel("P 保留 · X 排除 · 0–5 星 · A–D 候选 · ←/→ 导航 · Ctrl+Z 撤销")
        self.confirm_button = QPushButton("确认并进入下一组")
        self.confirm_button.setObjectName("Primary")
        self.confirm_button.clicked.connect(self._next_group)
        footer.addWidget(self.progress_label)
        footer.addWidget(self.hint_label, 1)
        footer.addWidget(self.confirm_button)
        middle_layout.addLayout(footer)
        self.splitter.addWidget(middle)

        self.inspector = InspectorWidget(store)
        self.inspector.face_selected.connect(self.canvas.focus_rect)
        self.splitter.addWidget(self.inspector)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.splitterMoved.connect(lambda _position, _index: self._save_timer.start())
        root.addWidget(self.splitter, 1)
        self.actions = install_review_shortcuts(self, self._command)
        self._restore_splitter_sizes()
        self.refresh()

    def _restore_splitter_sizes(self) -> None:
        left = int(self.review.store.get_preference("review.left_width") or 180)
        right = int(self.review.store.get_preference("review.right_width") or 290)
        self.splitter.setSizes([left, 700, right])

    def save_splitter_sizes(self) -> None:
        if self._stopped:
            return
        sizes = self.splitter.sizes()
        if len(sizes) == 3 and sizes[0] and sizes[2]:
            self.review.store.set_preference("review.left_width", str(sizes[0]))
            self.review.store.set_preference("review.right_width", str(sizes[2]))

    def reload(self) -> None:
        self.review.reload()
        self.refresh()

    def refresh(self) -> None:
        if self._stopped:
            return
        current = self.review.current
        self._updating = True
        try:
            self.filmstrip_model.replace_items(self.review.queue)
            if current is not None:
                self.filmstrip.setCurrentIndex(self.filmstrip_model.index(self.review.cursor))
        finally:
            self._updating = False
        self.sidebar.set_counts(self.review.filter_counts())
        if current is None:
            self.canvas.set_items([], self.review.view_mode)
            self.inspector.set_item({}, [])
            self.progress_label.setText("0 / 0")
            return
        if current.group_id is not None and current.group_id in self.review.groups:
            group = self.review.groups[current.group_id]
            group_items = [
                self.review.items[asset_id] for asset_id in group.item_ids
            ]
            self.progress_label.setText(
                f"组 {current.group_id} · 候选 {group.item_ids.index(current.asset_id) + 1}/{len(group.item_ids)}"
            )
        else:
            group_items = [current]
            self.progress_label.setText(f"照片 {self.review.cursor + 1}/{len(self.review.queue)}")
        self.canvas.set_items(
            group_items, self.review.view_mode, selected_id=current.asset_id
        )
        row, regions = self.review.inspector_data(current.asset_id)
        self.inspector.set_item(row, regions, focused=True)

    def _preview_ready(self, asset_id: str, image: QImage) -> None:
        current = self.review.current
        if current is not None and current.asset_id == asset_id:
            row, regions = self.review.inspector_data(asset_id)
            self.inspector.set_item(row, regions, focused=True, preview_image=image)

    def _filmstrip_selected(self, index, _previous) -> None:
        if self._updating or not index.isValid():
            return
        asset_id = index.data(FilmstripModel.AssetIdRole)
        if self.review.focus(asset_id) is not None:
            self.refresh()

    def _filter_selected(self, selected: ReviewFilter) -> None:
        self.review.set_filter(selected)
        self.refresh()

    def _next_group(self) -> None:
        current = self.review.current
        if current is None:
            return
        group_id = current.group_id
        while self.review.next() is not None:
            next_item = self.review.current
            if next_item.asset_id == current.asset_id or next_item.group_id != group_id:
                break
            current = next_item
        self.refresh()

    def _command(self, command: str) -> None:
        if command == "pick":
            self.review.decide("P")
        elif command == "reject":
            self.review.decide("X")
        elif command.startswith("star-"):
            self.review.set_star(int(command[-1]))
        elif command.startswith("candidate-"):
            self.review.select_candidate(command[-1].upper())
        elif command == "previous":
            self.review.previous()
        elif command == "next":
            self.review.next()
        elif command == "toggle-view":
            self.review.toggle_view_mode()
        elif command == "fullscreen":
            window = self.window()
            window.showNormal() if window.isFullScreen() else window.showFullScreen()
        elif command == "toggle-inspector":
            self.inspector.setVisible(self.inspector.isHidden())
        elif command == "undo":
            self.review.undo()
        self.refresh()

    def shutdown(self) -> None:
        if self._stopped:
            return
        if self._save_timer.isActive():
            self._save_timer.stop()
        self.save_splitter_sizes()
        self._stopped = True
        self.filmstrip_model.close()
        self.canvas.close()
