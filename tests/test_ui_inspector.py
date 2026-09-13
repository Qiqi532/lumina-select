from __future__ import annotations

from PyQt6.QtGui import QImage

from engine.store import PhotoStore
from ui.review.face_strip import FaceStrip
from ui.review.inspector import InspectorWidget


def test_five_sections_default_expanded_and_persist_collapse(qtbot, tmp_path):
    store = PhotoStore(str(tmp_path / "review.db"), enable_wal=False)
    try:
        inspector = InspectorWidget(store)
        qtbot.addWidget(inspector)
        assert list(inspector.sections) == [
            "决策摘要", "关键人脸", "评分明细", "拍摄信息", "分析信息"
        ]
        assert all(section.is_expanded for section in inspector.sections.values())
        inspector.sections["评分明细"].toggle()
        assert store.get_preference("inspector.scores.expanded") == "0"
        reopened = InspectorWidget(store)
        qtbot.addWidget(reopened)
        assert reopened.sections["评分明细"].is_expanded is False
    finally:
        store.close()


def test_missing_values_are_not_displayed_as_zero_and_badges_are_distinct(qtbot):
    inspector = InspectorWidget()
    qtbot.addWidget(inspector)
    inspector.set_item(
        {
            "fname": "portrait.CR3",
            "is_best": 1,
            "label": "P",
            "comp_score": None,
            "blur_score": None,
            "camera_model": None,
            "analysis_backend": None,
        },
        [],
        focused=True,
    )

    assert inspector.value_labels["comp_score"].text() == "未提供"
    assert inspector.value_labels["blur_score"].text() == "未提供"
    assert inspector.value_labels["camera_model"].text() == "未提供"
    assert inspector.face_strip.empty_text == "未检测"
    assert len({
        inspector.ai_badge.text(),
        inspector.focus_badge.text(),
        inspector.manual_badge.text(),
    }) == 3
    assert "清晰" in inspector.value_labels["blur_score"].toolTip()


def test_face_click_emits_normalized_bbox_from_existing_preview(qtbot):
    image = QImage(200, 100, QImage.Format.Format_RGB32)
    image.fill(0xFF778899)
    strip = FaceStrip()
    qtbot.addWidget(strip)
    strip.set_faces(
        [{"face_index": 0, "x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4,
          "eye_close_prob": 0.8, "sharpness": 42.0}],
        image,
    )
    assert strip.face_buttons[0].icon().isNull() is False
    assert "疑似闭眼" in strip.face_buttons[0].text()
    with qtbot.waitSignal(strip.face_selected) as captured:
        strip.face_buttons[0].click()
    assert captured.args == [(0.1, 0.2, 0.3, 0.4)]
