from __future__ import annotations

from threading import Event

from PyQt6.QtCore import QRect, Qt
from PyQt6.QtGui import QImage, QPainter, QPixmap
from PyQt6.QtWidgets import QStyleOptionViewItem

from services.review_service import ReviewFilter, ReviewItem
from ui.review.filmstrip import FilmstripDelegate, FilmstripModel, FilmstripView
from ui.review.filters import FilterSidebar


def _items(count):
    return [
        ReviewItem(
            asset_id=f"asset-{index}",
            paths=(f"/photos/{index:04d}.jpg",),
            preview_path=f"/photos/{index:04d}.jpg",
            group_id=None,
            candidate_rank=None,
            star=index % 6,
            label=None,
            is_best=index % 7 == 0,
            is_uncertain=False,
            is_rejected=False,
            has_technical_issue=False,
        )
        for index in range(count)
    ]


def test_thousand_rows_use_delegate_without_index_widgets(qtbot):
    model = FilmstripModel(_items(1000), thumbnail_loader=lambda _path: QImage(16, 16, QImage.Format.Format_RGB32))
    view = FilmstripView(model)
    qtbot.addWidget(view)
    view.resize(800, 120)
    view.show()

    assert model.rowCount() == 1000
    assert all(view.indexWidget(model.index(row)) is None for row in (0, 20, 500, 999))
    assert view.itemDelegate() is not None
    model.close()


def test_filmstrip_delegate_paints_filename_and_badges_without_qt_error(qtbot):
    model = FilmstripModel(_items(1), thumbnail_loader=lambda _path: QImage())
    image = QImage(156, 106, QImage.Format.Format_RGB32)
    image.fill(0xFF111315)
    option = QStyleOptionViewItem()
    option.rect = QRect(0, 0, 156, 106)
    painter = QPainter(image)
    try:
        FilmstripDelegate().paint(painter, option, model.index(0))
    finally:
        painter.end()
        model.close()


def test_roles_and_lazy_thumbnail_request(qtbot):
    requested = []

    def load(path):
        requested.append(path)
        image = QImage(20, 10, QImage.Format.Format_RGB32)
        image.fill(0xFF336699)
        return image

    model = FilmstripModel(_items(2), thumbnail_loader=load)
    try:
        first = model.index(0)
        assert model.data(first, Qt.ItemDataRole.DisplayRole) == "0000.jpg"
        assert model.data(first, model.AssetIdRole) == "asset-0"
        assert model.data(first, model.StarRole) == 0
        assert requested == []

        assert model.data(first, Qt.ItemDataRole.DecorationRole) is None
        qtbot.waitUntil(lambda: len(requested) == 1 and model.thumbnail_state("asset-0") == "ready")
        assert isinstance(model.data(first, Qt.ItemDataRole.DecorationRole), QPixmap)
        assert requested == ["/photos/0000.jpg"]
    finally:
        model.close()


def test_thumbnail_lru_is_bounded(qtbot):
    def load(_path):
        image = QImage(10, 10, QImage.Format.Format_RGB32)
        image.fill(0xFF112233)
        return image

    model = FilmstripModel(_items(4), thumbnail_loader=load, cache_limit=2)
    try:
        for row in range(4):
            index = model.index(row)
            model.data(index, Qt.ItemDataRole.DecorationRole)
            qtbot.waitUntil(
                lambda row=row: model.thumbnail_state(f"asset-{row}") == "ready"
            )
        assert model.cached_count <= 2
        assert model.thumbnail_state("asset-0") != "ready"
    finally:
        model.close()


def test_scroll_cancels_queued_offscreen_thumbnail(qtbot):
    gate = Event()
    started = []

    def load(path):
        started.append(path)
        gate.wait(2)
        return QImage(8, 8, QImage.Format.Format_RGB32)

    model = FilmstripModel(_items(3), thumbnail_loader=load)
    try:
        for row in range(3):
            model.data(model.index(row), Qt.ItemDataRole.DecorationRole)
        qtbot.waitUntil(lambda: len(started) == 2)
        model.set_visible_range(0, 0, prefetch=0)

        assert model.thumbnail_state("asset-2") == "missing"
        assert "/photos/0002.jpg" not in started
    finally:
        gate.set()
        model.close()


def test_filter_sidebar_emits_filter_and_displays_counts(qtbot):
    sidebar = FilterSidebar()
    qtbot.addWidget(sidebar)
    counts = {filter_name: index for index, filter_name in enumerate(ReviewFilter)}
    sidebar.set_counts(counts)

    with qtbot.waitSignal(sidebar.filter_selected) as captured:
        sidebar.buttons[ReviewFilter.UNCERTAIN].click()

    assert captured.args == [ReviewFilter.UNCERTAIN]
    assert "2" in sidebar.buttons[ReviewFilter.UNCERTAIN].text()
