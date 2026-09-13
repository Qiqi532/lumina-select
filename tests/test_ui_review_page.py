from __future__ import annotations

from dataclasses import replace
from threading import Event

from PyQt6.QtGui import QImage

from engine.store import PhotoStore
from services.review_service import ReviewFilter, ReviewService, ViewMode
from ui.main_window import MainWindow
from ui.pages.review_page import ReviewPage
from ui.review.canvas import CanvasWidget


def _populate(store: PhotoStore):
    rows = [
        ("/photos/solo.JPG", "solo", None, None, 0, 0, None),
        ("/photos/IMG_A.CR3", "candidate-a", 1, 1, 1, 0, None),
        ("/photos/IMG_B.CR3", "candidate-b", 1, 2, 1, 0, None),
        ("/photos/rejected.CR3", "rejected", None, None, 0, 1, "X"),
    ]
    for path, asset_id, group, rank, uncertain, waste, label in rows:
        store.upsert_photo(
            {
                "path": path,
                "fname": path.rsplit("/", 1)[-1],
                "asset_pair_id": asset_id,
                "asset_role": "jpeg" if path.endswith("JPG") else "raw",
                "group_id": group,
                "candidate_rank": rank,
                "is_candidate": int(group is not None),
                "is_uncertain": uncertain,
                "is_waste": waste,
                "label": label,
                "decision_source": "manual" if label else None,
                "analysis_backend": "heuristic",
            }
        )
    store.set_group(1, 2, "/photos/IMG_A.CR3", is_uncertain=True, n_candidates=2)


def _image(_path):
    image = QImage(100, 70, QImage.Format.Format_RGB32)
    image.fill(0xFF223344)
    return image


def test_main_window_enters_real_review_workspace_and_modes(qtbot, tmp_path):
    window = MainWindow(project_db=tmp_path / "review.db")
    qtbot.addWidget(window)
    _populate(window.store)
    window.goto("review")

    assert isinstance(window.review_page, ReviewPage)
    page = window.review_page
    page.review.focus("solo")
    page.refresh()
    assert page.canvas.mode == ViewMode.SINGLE
    page.review.focus("candidate-a")
    page.refresh()
    assert page.canvas.mode == ViewMode.COMPARE
    assert len(page.canvas.viewports) == 2
    assert page.filmstrip_model.rowCount() == 4


def test_keyboard_flow_updates_store_undo_counts_and_filters(qtbot, tmp_path):
    store = PhotoStore(str(tmp_path / "review.db"), enable_wal=False)
    _populate(store)
    page = ReviewPage(store, image_loader=_image, thumbnail_loader=_image)
    qtbot.addWidget(page)
    try:
        page.review.focus("candidate-a")
        page.refresh()
        page.actions["B"].trigger()
        assert page.review.current.asset_id == "candidate-b"
        page.actions["P"].trigger()
        assert store.get_photo("/photos/IMG_B.CR3")["label"] == "P"
        assert page.sidebar.buttons[ReviewFilter.AI_PICKS].text().endswith("1")

        page.actions["Right"].trigger()
        assert page.review.current.asset_id == "rejected"
        page.actions["Ctrl+Z"].trigger()
        assert store.get_photo("/photos/IMG_B.CR3")["label"] is None
        assert page.review.current.asset_id == "candidate-b"
        assert page.sidebar.buttons[ReviewFilter.AI_PICKS].text().endswith("0")

        page.sidebar.buttons[ReviewFilter.UNCERTAIN].click()
        assert [item.asset_id for item in page.review.queue] == [
            "candidate-a", "candidate-b"
        ]
        page.actions["I"].trigger()
        assert page.inspector.isHidden()
        page.actions["I"].trigger()
        assert not page.inspector.isHidden()
    finally:
        page.shutdown()
        store.close()


def test_project_reopen_restores_view_and_splitter_widths(qtbot, tmp_path):
    path = tmp_path / "review.db"
    first = MainWindow(project_db=path)
    qtbot.addWidget(first)
    _populate(first.store)
    first.resize(1280, 720)
    first.show()
    first.goto("review")
    first.review_page.review.focus("candidate-a")
    first.review_page.refresh()
    first.review_page.actions["Tab"].trigger()
    assert first.store.get_preference("review.view_mode") == "single"
    first.review_page.splitter.setSizes([190, 720, 290])
    first.review_page.save_splitter_sizes()
    first.close()

    reopened = MainWindow(project_db=path)
    qtbot.addWidget(reopened)
    reopened.resize(1280, 720)
    reopened.show()
    reopened.goto("review")
    reopened.review_page.review.focus("candidate-a")
    reopened.review_page.refresh()
    assert reopened.review_page.canvas.mode == ViewMode.SINGLE
    assert reopened.store.get_preference("review.left_width") is not None
    assert reopened.store.get_preference("review.right_width") is not None
    sizes = reopened.review_page.splitter.sizes()
    assert 160 <= sizes[0] <= 220
    assert 260 <= sizes[2] <= 340
    reopened.close()


def test_late_preview_from_previous_generation_cannot_replace_new_project(qtbot, tmp_path):
    store = PhotoStore(str(tmp_path / "review.db"), enable_wal=False)
    _populate(store)
    source_item = ReviewService(store).items["solo"]
    old_started = Event()
    release_old = Event()

    def loader(path):
        image = QImage(20, 20, QImage.Format.Format_RGB32)
        if path == "old.jpg":
            old_started.set()
            release_old.wait(2)
            image.fill(0xFFFF0000)
        else:
            image.fill(0xFF00FF00)
        return image

    canvas = CanvasWidget(image_loader=loader)
    qtbot.addWidget(canvas)
    try:
        canvas.set_items([replace(source_item, preview_path="old.jpg")], ViewMode.SINGLE)
        assert old_started.wait(1)
        canvas.set_items([replace(source_item, preview_path="new.jpg")], ViewMode.SINGLE)
        qtbot.waitUntil(lambda: canvas.viewports[0].has_image)
        release_old.set()
        qtbot.wait(50)
        pixmap = canvas.viewports[0].scene().items()[0].pixmap()
        assert pixmap.toImage().pixelColor(0, 0).name() == "#00ff00"
    finally:
        release_old.set()
        canvas.close()
        store.close()
