"""Adaptive single/compare canvas with asynchronous high-resolution previews."""
from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from typing import Callable

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QImage, QKeyEvent, QMouseEvent, QPixmap
from PyQt6.QtWidgets import (
    QGraphicsScene,
    QGraphicsView,
    QGridLayout,
    QWidget,
)

from services.review_service import ReviewItem, ViewMode


def _load_large_qimage(path: str) -> QImage:
    from engine.loader import load_review_preview

    image = load_review_preview(path, max_size=2500)
    if image is None:
        return QImage()
    rgb = image.convert("RGB")
    width, height = rgb.size
    return QImage(
        rgb.tobytes(), width, height, width * 3, QImage.Format.Format_RGB888
    ).copy()


class PhotoViewport(QGraphicsView):
    double_clicked = pyqtSignal(str)

    def __init__(self, asset_id: str, parent=None) -> None:
        super().__init__(parent)
        self.asset_id = asset_id
        self.setScene(QGraphicsScene(self))
        self.setRenderHints(self.renderHints())
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.has_image = False
        self.error_text: str | None = None
        self.fit_mode = True
        self._zoom = 1.0

    @property
    def zoom_percent(self) -> int:
        return round(self._zoom * 100)

    def set_image(self, image: QImage) -> None:
        if image.isNull():
            self.set_error("无法预览该照片；可继续浏览其他照片")
            return
        scene = self.scene()
        scene.clear()
        scene.addPixmap(QPixmap.fromImage(image))
        self.has_image = True
        self.error_text = None
        self.fit_view()

    def set_error(self, message: str) -> None:
        scene = self.scene()
        scene.clear()
        scene.addText(message)
        self.has_image = False
        self.error_text = message
        self.fit_mode = True

    def fit_view(self) -> None:
        if self.has_image:
            self.fitInView(self.scene().itemsBoundingRect(), Qt.AspectRatioMode.KeepAspectRatio)
        self.fit_mode = True
        self._zoom = 1.0

    def set_zoom(self, factor: float) -> None:
        self._zoom = min(8.0, max(0.1, factor))
        self.resetTransform()
        self.scale(self._zoom, self._zoom)
        self.fit_mode = False

    def focus_rect(self, bbox: tuple[float, float, float, float]) -> None:
        if not self.has_image:
            return
        bounds = self.scene().itemsBoundingRect()
        x, y, width, height = bbox
        self.set_zoom(max(1.0, self._zoom))
        self.centerOn(
            bounds.left() + (x + width / 2) * bounds.width(),
            bounds.top() + (y + height / 2) * bounds.height(),
        )

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        self.double_clicked.emit(self.asset_id)
        super().mouseDoubleClickEvent(event)


class CanvasWidget(QWidget):
    image_finished = pyqtSignal(str, object)

    def __init__(
        self,
        *,
        review_service=None,
        image_loader: Callable[[str], QImage] = _load_large_qimage,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.review_service = review_service
        self.image_loader = image_loader
        self.mode = ViewMode.SINGLE
        self.viewports: list[PhotoViewport] = []
        self._items: list[ReviewItem] = []
        self._selected_id: str | None = None
        self._return_mode: ViewMode | None = None
        self._inspect_100 = False
        self._executor = ThreadPoolExecutor(max_workers=5, thread_name_prefix="lumina-preview")
        self._pending: dict[str, Future] = {}
        self._cache: OrderedDict[tuple[str, int], QImage] = OrderedDict()
        self._closed = False
        self.image_finished.connect(self._on_image, Qt.ConnectionType.QueuedConnection)
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(4, 4, 4, 4)
        self.grid.setSpacing(8)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def set_items(self, items: list[ReviewItem], mode: ViewMode) -> None:
        if not items:
            self._items = []
            self._selected_id = None
        else:
            self._items = list(items[:5])
            self._selected_id = self._items[0].asset_id
        self._return_mode = None
        self._inspect_100 = False
        self.mode = ViewMode(mode)
        active_ids = {item.asset_id for item in self._items}
        for asset_id, future in list(self._pending.items()):
            if asset_id not in active_ids and future.cancel():
                self._pending.pop(asset_id, None)
        for key in list(self._cache):
            if key[0] not in active_ids:
                self._cache.pop(key)
        self._render()

    def _clear_views(self) -> None:
        while self.grid.count():
            cell = self.grid.takeAt(0)
            widget = cell.widget()
            if widget is not None:
                widget.deleteLater()
        self.viewports = []

    def _visible_items(self) -> list[ReviewItem]:
        if self.mode == ViewMode.COMPARE:
            return self._items[:5]
        return [
            item for item in self._items if item.asset_id == self._selected_id
        ][:1]

    def _render(self) -> None:
        self._clear_views()
        visible = self._visible_items()
        columns = 1 if len(visible) == 1 else 2 if len(visible) <= 4 else 3
        for index, item in enumerate(visible):
            view = PhotoViewport(item.asset_id)
            view.double_clicked.connect(self.inspect_candidate)
            self.grid.addWidget(view, index // columns, index % columns)
            self.viewports.append(view)
            key = (item.asset_id, 2500)
            image = self._cache.get(key)
            if image is not None:
                self._cache.move_to_end(key)
                view.set_image(image)
                if self._inspect_100:
                    view.set_zoom(1.0)
            else:
                self._request(item)

    def _request(self, item: ReviewItem) -> None:
        if self._closed or item.asset_id in self._pending:
            return
        future = self._executor.submit(self.image_loader, item.preview_path)
        self._pending[item.asset_id] = future

        def finished(completed: Future, asset_id: str = item.asset_id) -> None:
            try:
                image = completed.result()
            except CancelledError:
                return
            except Exception:
                image = QImage()
            if not self._closed:
                self.image_finished.emit(asset_id, image)

        future.add_done_callback(finished)

    def _on_image(self, asset_id: str, image: QImage) -> None:
        if asset_id not in self._pending:
            return
        self._pending.pop(asset_id, None)
        if not image.isNull():
            key = (asset_id, 2500)
            self._cache[key] = image
            self._cache.move_to_end(key)
            while len(self._cache) > 5:
                self._cache.popitem(last=False)
        for view in self.viewports:
            if view.asset_id == asset_id:
                view.set_image(image)
                if self._inspect_100 and not image.isNull():
                    view.set_zoom(1.0)

    def toggle_view(self) -> ViewMode:
        if self.review_service is not None:
            self.mode = self.review_service.toggle_view_mode()
        else:
            self.mode = ViewMode.COMPARE if self.mode == ViewMode.SINGLE else ViewMode.SINGLE
        self._return_mode = None
        self._inspect_100 = False
        self._render()
        return self.mode

    def inspect_candidate(self, asset_id: str) -> None:
        if asset_id not in {item.asset_id for item in self._items}:
            return
        if self.mode == ViewMode.COMPARE:
            self._return_mode = self.mode
        self._selected_id = asset_id
        self.mode = ViewMode.SINGLE
        self._inspect_100 = True
        self._render()

    def escape_inspection(self) -> None:
        if self._return_mode is not None:
            self.mode = self._return_mode
            self._return_mode = None
            self._inspect_100 = False
            self._render()

    def focus_rect(self, bbox: tuple[float, float, float, float]) -> None:
        for view in self.viewports:
            if view.asset_id == self._selected_id:
                view.focus_rect(bbox)
                return

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.escape_inspection()
            event.accept()
            return
        super().keyPressEvent(event)

    def close(self) -> bool:
        self._closed = True
        for future in self._pending.values():
            future.cancel()
        self._pending.clear()
        self._executor.shutdown(wait=False, cancel_futures=True)
        return super().close()
