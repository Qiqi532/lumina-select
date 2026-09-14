"""Virtual asset filmstrip with background thumbnails and bounded cache."""
from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from PyQt6.QtCore import QAbstractListModel, QModelIndex, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QImage, QPainter, QPixmap
from PyQt6.QtWidgets import QListView, QStyle, QStyledItemDelegate

from services.review_service import ReviewItem
from ui.theme import COLORS


def _load_qimage(path: str) -> QImage:
    from engine.loader import load_review_preview

    image = load_review_preview(path, max_size=256)
    if image is None:
        return QImage()
    rgb = image.convert("RGB")
    width, height = rgb.size
    return QImage(
        rgb.tobytes(), width, height, width * 3, QImage.Format.Format_RGB888
    ).copy()


class FilmstripModel(QAbstractListModel):
    thumbnail_finished = pyqtSignal(int, str, object)

    AssetIdRole = int(Qt.ItemDataRole.UserRole) + 1
    StarRole = AssetIdRole + 1
    LabelRole = AssetIdRole + 2
    RecommendedRole = AssetIdRole + 3
    IssueRole = AssetIdRole + 4
    CandidateRole = AssetIdRole + 5
    ThumbnailStateRole = AssetIdRole + 6

    def __init__(
        self,
        items: list[ReviewItem],
        *,
        thumbnail_loader: Callable[[str], QImage] = _load_qimage,
        cache_limit: int = 256,
        cache_max_pixels: int = 256 * 256 * 256,
        parent=None,
    ) -> None:
        super().__init__(parent)
        if cache_limit < 1 or cache_max_pixels < 1:
            raise ValueError("thumbnail cache limits must be positive")
        self.items = list(items)
        self.thumbnail_loader = thumbnail_loader
        self.cache_limit = cache_limit
        self.cache_max_pixels = cache_max_pixels
        self._cache: OrderedDict[str, tuple[QPixmap, int]] = OrderedDict()
        self._cached_pixels = 0
        self._futures: dict[str, Future] = {}
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="lumina-thumb")
        self._closed = False
        self._generation = 0
        self._cache_paths = {item.asset_id: item.preview_path for item in self.items}
        self._visible_window: tuple[int, int] | None = None
        self.thumbnail_finished.connect(
            self._thumbnail_ready, Qt.ConnectionType.QueuedConnection
        )

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.items)

    def roleNames(self) -> dict[int, bytes]:
        return {
            self.AssetIdRole: b"assetId",
            self.StarRole: b"star",
            self.LabelRole: b"label",
            self.RecommendedRole: b"recommended",
            self.IssueRole: b"issue",
            self.CandidateRole: b"candidate",
            self.ThumbnailStateRole: b"thumbnailState",
        }

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.items):
            return None
        item = self.items[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return Path(item.preview_path).name
        if role == Qt.ItemDataRole.DecorationRole:
            cached = self._cache.get(item.asset_id)
            if cached is not None:
                self._cache.move_to_end(item.asset_id)
                return cached[0]
            self._request_thumbnail(item)
            return None
        if role == self.AssetIdRole:
            return item.asset_id
        if role == self.StarRole:
            return item.star
        if role == self.LabelRole:
            return item.label
        if role == self.RecommendedRole:
            return item.is_best
        if role == self.IssueRole:
            return item.has_technical_issue or item.is_rejected
        if role == self.CandidateRole:
            rank = item.candidate_rank
            return chr(ord("A") + rank - 1) if rank is not None and 1 <= rank <= 4 else ""
        if role == self.ThumbnailStateRole:
            return self.thumbnail_state(item.asset_id)
        return None

    def _request_thumbnail(self, item: ReviewItem) -> None:
        if self._closed or item.asset_id in self._futures:
            return
        future = self._executor.submit(self.thumbnail_loader, item.preview_path)
        self._futures[item.asset_id] = future

        generation = self._generation

        def finished(completed: Future, asset_id: str = item.asset_id) -> None:
            try:
                image = completed.result()
            except CancelledError:
                return
            except Exception:
                image = QImage()
            if not self._closed:
                self.thumbnail_finished.emit(generation, asset_id, image)

        future.add_done_callback(finished)

    def _thumbnail_ready(self, generation: int, asset_id: str, image: QImage) -> None:
        if generation != self._generation or asset_id not in self._futures:
            return
        self._futures.pop(asset_id, None)
        if image.isNull():
            return
        pixmap = QPixmap.fromImage(image)
        pixels = max(1, pixmap.width() * pixmap.height())
        self._cache[asset_id] = (pixmap, pixels)
        self._cache.move_to_end(asset_id)
        self._cached_pixels += pixels
        while len(self._cache) > self.cache_limit or self._cached_pixels > self.cache_max_pixels:
            _, (_, evicted_pixels) = self._cache.popitem(last=False)
            self._cached_pixels -= evicted_pixels
        for row, item in enumerate(self.items):
            if item.asset_id == asset_id:
                index = self.index(row)
                self.dataChanged.emit(index, index, [Qt.ItemDataRole.DecorationRole])
                break

    @property
    def cached_count(self) -> int:
        return len(self._cache)

    def thumbnail_state(self, asset_id: str) -> str:
        if asset_id in self._cache:
            return "ready"
        if asset_id in self._futures:
            return "loading"
        return "missing"

    def set_visible_range(self, first: int, last: int, *, prefetch: int = 16) -> None:
        lower = max(0, first - prefetch)
        upper = min(len(self.items) - 1, last + prefetch)
        self._visible_window = (lower, upper)
        visible_ids = {item.asset_id for item in self.items[lower : upper + 1]}
        for asset_id, future in list(self._futures.items()):
            if asset_id not in visible_ids and future.cancel():
                self._futures.pop(asset_id, None)

    def replace_items(self, items: list[ReviewItem]) -> None:
        self._generation += 1
        for future in self._futures.values():
            future.cancel()
        self._futures.clear()
        next_paths = {item.asset_id: item.preview_path for item in items}
        for asset_id in list(self._cache):
            if self._cache_paths.get(asset_id) != next_paths.get(asset_id):
                _, pixels = self._cache.pop(asset_id)
                self._cached_pixels -= pixels
        self._cache_paths = next_paths
        self.beginResetModel()
        self.items = list(items)
        self.endResetModel()

    def close(self) -> None:
        self._closed = True
        for future in self._futures.values():
            future.cancel()
        self._futures.clear()
        self._executor.shutdown(wait=False, cancel_futures=True)


class FilmstripDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index) -> QSize:
        return QSize(156, 106)

    def paint(self, painter: QPainter, option, index) -> None:
        painter.save()
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        painter.fillRect(option.rect, QColor(COLORS["card"]))
        painter.setPen(QColor(COLORS["accent"] if selected else COLORS["border"]))
        painter.drawRect(option.rect.adjusted(1, 1, -2, -2))
        thumb_rect = option.rect.adjusted(8, 6, -8, -36)
        pixmap = index.data(Qt.ItemDataRole.DecorationRole)
        if isinstance(pixmap, QPixmap):
            scaled = pixmap.scaled(
                thumb_rect.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            painter.drawPixmap(
                thumb_rect.x() + (thumb_rect.width() - scaled.width()) // 2,
                thumb_rect.y() + (thumb_rect.height() - scaled.height()) // 2,
                scaled,
            )
        painter.setPen(QColor(COLORS["text"]))
        text_flags = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        painter.drawText(option.rect.adjusted(8, 70, -8, -18), text_flags, str(index.data()))
        flags = []
        if index.data(FilmstripModel.RecommendedRole):
            flags.append("AI")
        if index.data(FilmstripModel.IssueRole):
            flags.append("!")
        candidate = index.data(FilmstripModel.CandidateRole)
        if candidate:
            flags.append(candidate)
        star = index.data(FilmstripModel.StarRole)
        painter.setPen(QColor(COLORS["accent"] if flags else COLORS["muted"]))
        painter.drawText(
            option.rect.adjusted(8, 86, -8, -4), text_flags,
            f"{' '.join(flags)}  {star}★",
        )
        painter.restore()


class FilmstripView(QListView):
    def __init__(self, model: FilmstripModel, parent=None) -> None:
        super().__init__(parent)
        self.setModel(model)
        self.setItemDelegate(FilmstripDelegate(self))
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setFlow(QListView.Flow.LeftToRight)
        self.setWrapping(False)
        self.setUniformItemSizes(True)
        self.setHorizontalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def scrollContentsBy(self, dx: int, dy: int) -> None:
        super().scrollContentsBy(dx, dy)
        model = self.model()
        if isinstance(model, FilmstripModel):
            first = self.indexAt(self.viewport().rect().topLeft()).row()
            last = self.indexAt(self.viewport().rect().bottomRight()).row()
            if first >= 0:
                model.set_visible_range(first, last if last >= first else first)
