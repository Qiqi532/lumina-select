"""Database-backed, collapsible technical explanation panel."""
from __future__ import annotations

from collections.abc import Mapping

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import (
    QLabel,
    QProgressBar,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .face_strip import FaceStrip


SECTIONS = (
    ("决策摘要", "decision"),
    ("关键人脸", "faces"),
    ("评分明细", "scores"),
    ("拍摄信息", "capture"),
    ("分析信息", "analysis"),
)

FIELDS = {
    "决策摘要": ("comp_score", "is_best", "is_uncertain"),
    "评分明细": ("blur_score", "over_ratio", "under_ratio", "aesthetic", "eye_open"),
    "拍摄信息": (
        "fname", "width", "height", "camera_model", "lens_model",
        "focal_length", "shutter_speed", "aperture", "iso", "ts", "path",
    ),
    "分析信息": (
        "analysis_backend", "quality_model", "scene_model", "scene",
        "scene_conf", "analysis_ms",
    ),
}

FIELD_TITLES = {
    "comp_score": "综合分",
    "is_best": "AI 推荐",
    "is_uncertain": "待确认",
    "blur_score": "清晰度",
    "over_ratio": "过曝比例",
    "under_ratio": "欠曝比例",
    "aesthetic": "美学评分",
    "eye_open": "眼睛状态",
    "fname": "文件名",
    "width": "宽度",
    "height": "高度",
    "camera_model": "相机",
    "lens_model": "镜头",
    "focal_length": "焦距",
    "shutter_speed": "快门",
    "aperture": "光圈",
    "iso": "ISO",
    "ts": "拍摄时间",
    "path": "文件路径",
    "analysis_backend": "分析后端",
    "quality_model": "质量算法",
    "scene_model": "场景模型",
    "scene": "场景",
    "scene_conf": "场景置信度",
    "analysis_ms": "分析耗时",
}

SCORE_TOOLTIPS = {
    "blur_score": "清晰度：衡量照片细节是否足够清晰。",
    "over_ratio": "过曝比例：画面中亮部细节丢失的面积比例。",
    "under_ratio": "欠曝比例：画面中暗部细节丢失的面积比例。",
    "aesthetic": "美学评分：模型对构图与视觉吸引力的参考评分。",
    "eye_open": "眼睛状态：检测到的人脸睁眼情况。",
}


class CollapsibleSection(QWidget):
    def __init__(self, title: str, slug: str, store=None, parent=None) -> None:
        super().__init__(parent)
        self.slug = slug
        self.store = store
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.header = QToolButton()
        self.header.setText(title)
        self.header.setCheckable(True)
        self.header.setObjectName(f"InspectorSection-{slug}")
        saved = store.get_preference(f"inspector.{slug}.expanded") if store else None
        self.header.setChecked(saved != "0")
        self.header.toggled.connect(self._on_toggled)
        layout.addWidget(self.header)
        self.content = QWidget()
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(8, 4, 8, 12)
        layout.addWidget(self.content)
        self.content.setVisible(self.header.isChecked())

    @property
    def is_expanded(self) -> bool:
        return self.header.isChecked()

    def toggle(self) -> None:
        self.header.setChecked(not self.header.isChecked())

    def _on_toggled(self, expanded: bool) -> None:
        self.content.setVisible(expanded)
        if self.store is not None:
            self.store.set_preference(
                f"inspector.{self.slug}.expanded", "1" if expanded else "0"
            )


class InspectorWidget(QScrollArea):
    face_selected = pyqtSignal(tuple)

    def __init__(self, store=None, parent=None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setMinimumWidth(260)
        self.setMaximumWidth(420)
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        self.sections = {
            title: CollapsibleSection(title, slug, store, container)
            for title, slug in SECTIONS
        }
        self.value_labels: dict[str, QLabel] = {}
        self.score_bars: dict[str, QProgressBar] = {}
        for title, section in self.sections.items():
            layout.addWidget(section)
            if title == "关键人脸":
                self.face_strip = FaceStrip()
                self.face_strip.face_selected.connect(self.face_selected)
                section.content_layout.addWidget(self.face_strip)
                continue
            if title == "决策摘要":
                self.ai_badge = QLabel("AI 未推荐")
                self.focus_badge = QLabel("非当前焦点")
                self.manual_badge = QLabel("未人工保留")
                for badge in (self.ai_badge, self.focus_badge, self.manual_badge):
                    section.content_layout.addWidget(badge)
            for field in FIELDS.get(title, ()):
                label = QLabel(f"{FIELD_TITLES[field]}：未提供")
                if field in SCORE_TOOLTIPS:
                    label.setToolTip(SCORE_TOOLTIPS[field])
                section.content_layout.addWidget(label)
                self.value_labels[field] = label
                if field in SCORE_TOOLTIPS:
                    bar = QProgressBar()
                    bar.setRange(0, 100)
                    bar.setValue(0)
                    bar.setEnabled(False)
                    section.content_layout.addWidget(bar)
                    self.score_bars[field] = bar
        layout.addStretch(1)
        self.setWidget(container)

    @staticmethod
    def _display(value) -> str:
        if value is None or value == "":
            return "未提供"
        if isinstance(value, float):
            return f"{value:.2f}"
        return str(value)

    def set_item(
        self,
        item: Mapping,
        regions: list[dict],
        *,
        focused: bool = False,
        preview_image: QImage | None = None,
    ) -> None:
        self.ai_badge.setText("AI 推荐" if item.get("is_best") else "AI 未推荐")
        self.focus_badge.setText("当前焦点" if focused else "非当前焦点")
        self.manual_badge.setText("人工保留" if item.get("label") == "P" else "未人工保留")
        for field, label in self.value_labels.items():
            value = item.get(field)
            label.setText(self._display(value))
            if field in self.score_bars:
                bar = self.score_bars[field]
                bar.setEnabled(value is not None)
                if value is not None:
                    score = float(value)
                    if field in {"over_ratio", "under_ratio"}:
                        score *= 100
                    bar.setValue(max(0, min(100, round(score))))
        self.face_strip.set_faces(regions, preview_image)
