"""Approved v0.5 visual tokens and Qt stylesheet."""
from __future__ import annotations


COLORS = {
    "background": "#111315",
    "surface": "#181B1E",
    "card": "#1C1F22",
    "border": "#303338",
    "text": "#DFE2E6",
    "muted": "#9299A1",
    "accent": "#D7AA5A",
    "success": "#73C69A",
    "warning": "#DFB970",
    "danger": "#D46A6A",
}
SPACING = (4, 8, 12, 16, 24)
FONT_FAMILY = '"Segoe UI", "Microsoft YaHei UI", sans-serif'


def stylesheet() -> str:
    color = COLORS
    return f"""
QWidget {{
    background-color: {color['background']};
    color: {color['text']};
    font-family: {FONT_FAMILY};
    font-size: 13px;
}}
QMainWindow, QDialog {{ background-color: {color['background']}; }}
QLabel {{ background: transparent; }}
QFrame#WorkflowHeader, QFrame#Panel {{
    background-color: {color['surface']};
    border: 1px solid {color['border']};
    border-radius: 8px;
}}
QLabel#PageTitle {{ font-size: 22px; font-weight: 700; }}
QLabel#PageSub, QLabel#Muted {{ color: {color['muted']}; }}
QLabel#StageState {{ color: {color['accent']}; font-weight: 600; }}
QPushButton {{
    background-color: {color['card']};
    border: 1px solid {color['border']};
    border-radius: 7px;
    padding: 8px 16px;
    min-height: 24px;
}}
QPushButton:hover {{ border-color: {color['text']}; }}
QPushButton:focus {{ border: 2px solid {color['accent']}; }}
QPushButton:disabled {{ color: {color['muted']}; }}
QPushButton#Primary {{
    background-color: {color['accent']};
    border-color: {color['accent']};
    color: {color['background']};
    font-weight: 700;
}}
QPushButton#Primary:hover {{ background-color: {color['warning']}; }}
QPushButton#StageButton[state="current"] {{
    border-color: {color['accent']};
    color: {color['accent']};
    font-weight: 700;
}}
QPushButton#StageButton[state="done"] {{ color: {color['success']}; }}
QPushButton#Danger {{ color: {color['danger']}; }}
QProgressBar {{
    background-color: {color['card']};
    border: 1px solid {color['border']};
    border-radius: 6px;
    min-height: 20px;
    text-align: center;
}}
QProgressBar::chunk {{ background-color: {color['accent']}; }}
QLineEdit {{
    background-color: {color['card']};
    border: 1px solid {color['border']};
    border-radius: 6px;
    padding: 8px;
}}
QLineEdit:focus {{ border: 2px solid {color['accent']}; }}
QStatusBar {{ background-color: {color['surface']}; color: {color['muted']}; }}
"""
