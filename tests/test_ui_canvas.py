from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QImage
from PyQt6.QtTest import QTest

from services.review_service import ReviewItem, ViewMode
from ui.review.canvas import CanvasWidget


def _items(count):
    return [
        ReviewItem(
            asset_id=f"asset-{index}",
            paths=(f"/photos/{index}.jpg",),
            preview_path=f"/photos/{index}.jpg",
            group_id=1 if count > 1 else None,
            candidate_rank=index + 1,
            star=0,
            label=None,
            is_best=index == 0,
            is_uncertain=count > 1,
            is_rejected=False,
            has_technical_issue=False,
        )
        for index in range(count)
    ]


def _image(_path):
    image = QImage(100, 60, QImage.Format.Format_RGB32)
    image.fill(0xFF334455)
    return image


def test_single_and_compare_canvas_limit_candidate_count(qtbot):
    canvas = CanvasWidget(image_loader=_image)
    qtbot.addWidget(canvas)
    canvas.set_items(_items(1), ViewMode.SINGLE)
    assert canvas.mode == ViewMode.SINGLE
    assert len(canvas.viewports) == 1

    canvas.set_items(_items(5), ViewMode.COMPARE)
    assert canvas.mode == ViewMode.COMPARE
    assert len(canvas.viewports) == 5
    assert canvas.pending_count <= 5
    canvas.close()


def test_tab_toggle_and_double_click_inspection_esc_return(qtbot):
    class FakeReview:
        view_mode = ViewMode.COMPARE
        calls = 0

        def toggle_view_mode(self):
            self.calls += 1
            self.view_mode = ViewMode.SINGLE if self.view_mode == ViewMode.COMPARE else ViewMode.COMPARE
            return self.view_mode

    review = FakeReview()
    canvas = CanvasWidget(review_service=review, image_loader=_image)
    qtbot.addWidget(canvas)
    canvas.set_items(_items(2), ViewMode.COMPARE)
    canvas.show()
    qtbot.waitUntil(lambda: all(view.has_image for view in canvas.viewports))

    QTest.mouseDClick(canvas.viewports[1].viewport(), Qt.MouseButton.LeftButton)
    assert canvas.mode == ViewMode.SINGLE
    assert canvas.viewports[0].asset_id == "asset-1"
    assert canvas.viewports[0].zoom_percent == 100
    canvas.keyPressEvent(_escape_event())
    assert canvas.mode == ViewMode.COMPARE

    canvas.toggle_view()
    assert review.calls == 1
    assert canvas.mode == ViewMode.SINGLE
    canvas.close()


def _escape_event():
    from PyQt6.QtGui import QKeyEvent

    return QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)


def test_zoom_clamps_and_new_item_restores_fit(qtbot):
    canvas = CanvasWidget(image_loader=_image)
    qtbot.addWidget(canvas)
    canvas.set_items(_items(1), ViewMode.SINGLE)
    qtbot.waitUntil(lambda: canvas.viewports[0].has_image)
    view = canvas.viewports[0]
    view.set_zoom(0.01)
    assert view.zoom_percent == 10
    view.set_zoom(20.0)
    assert view.zoom_percent == 800

    canvas.set_items(_items(1), ViewMode.SINGLE)
    assert canvas.viewports[0].fit_mode is True
    canvas.close()


def test_corrupt_preview_shows_error_and_next_item_still_loads(qtbot):
    def loader(path):
        return QImage() if path.endswith("0.jpg") else _image(path)

    canvas = CanvasWidget(image_loader=loader)
    qtbot.addWidget(canvas)
    canvas.set_items(_items(2), ViewMode.COMPARE)
    qtbot.waitUntil(lambda: canvas.viewports[0].error_text is not None)
    assert "无法预览" in canvas.viewports[0].error_text
    qtbot.waitUntil(lambda: canvas.viewports[1].has_image)
    canvas.close()
