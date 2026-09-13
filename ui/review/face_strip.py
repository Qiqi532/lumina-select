"""Face crops from an already decoded preview; no source-file I/O."""
from __future__ import annotations

from PyQt6.QtCore import QSize, pyqtSignal
from PyQt6.QtGui import QIcon, QImage, QPixmap
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget


class FaceStrip(QWidget):
    face_selected = pyqtSignal(tuple)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.layout = QHBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.face_buttons: list[QPushButton] = []
        self.empty_text = "未检测"
        self._empty_label = QLabel(self.empty_text)
        self.layout.addWidget(self._empty_label)

    def set_faces(self, regions: list[dict], preview_image: QImage | None = None) -> None:
        for button in self.face_buttons:
            self.layout.removeWidget(button)
            button.deleteLater()
        self.face_buttons.clear()
        self._empty_label.setVisible(not regions)
        for region in regions:
            bbox = tuple(float(region.get(key) or 0.0) for key in ("x", "y", "width", "height"))
            eye_probability = region.get("eye_close_prob")
            eye_text = (
                "未检测" if eye_probability is None else
                "疑似闭眼" if float(eye_probability) >= 0.7 else "睁眼"
            )
            sharpness = region.get("sharpness")
            sharpness_text = "未检测" if sharpness is None else f"{float(sharpness):.0f}"
            button = QPushButton(f"{eye_text} · 清晰度 {sharpness_text}")
            button.setObjectName("FaceCandidate")
            if preview_image is not None and not preview_image.isNull():
                x, y, width, height = bbox
                crop = preview_image.copy(
                    round(x * preview_image.width()),
                    round(y * preview_image.height()),
                    max(1, round(width * preview_image.width())),
                    max(1, round(height * preview_image.height())),
                )
                if not crop.isNull():
                    button.setIcon(QIcon(QPixmap.fromImage(crop)))
                    button.setIconSize(QSize(48, 48))
            button.clicked.connect(
                lambda _checked=False, selected=bbox: self.face_selected.emit(selected)
            )
            self.layout.addWidget(button)
            self.face_buttons.append(button)
