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
QFrame#ProjectCard {{
    background-color: {color['card']};
    border: 1px solid {color['border']};
    border-radius: 10px;
}}
QFrame#ProjectCard:hover {{ border-color: {color['accent']}; }}
QLabel#CardTitle {{ font-size: 15px; font-weight: 700; }}
QLabel#CardMeta {{ color: {color['muted']}; }}
QLabel#CardWarn {{ color: {color['warning']}; }}
QPushButton#CardForget {{
    background: transparent;
    border: 1px solid {color['border']};
    border-radius: 6px;
    padding: 1px 8px;
    color: {color['muted']};
}}
QPushButton#CardForget:hover {{ border-color: {color['danger']}; color: {color['danger']}; }}
QScrollArea {{ background: transparent; }}
/* ---- 【T-08】分面筛选条 ---- */
QWidget#FilterSidebar {{
    background-color: {color['surface']};
    border: 1px solid {color['border']};
    border-radius: 8px;
}}
QLabel#SidebarTitle {{ font-size: 15px; font-weight: 700; }}
QLabel#SectionTitle {{ color: {color['muted']}; font-size: 12px; font-weight: 600; }}
QLabel#ScopeLabel {{ color: {color['accent']}; font-size: 12px; }}
QFrame#Divider {{ color: {color['border']}; max-height: 1px; }}
QPushButton#LinkButton {{
    background: transparent;
    border: none;
    padding: 2px 6px;
    min-height: 0;
    color: {color['muted']};
    font-size: 12px;
}}
QPushButton#LinkButton:hover {{ color: {color['accent']}; }}
QPushButton#FacetHeader {{
    background: transparent;
    border: none;
    padding: 2px 0;
    min-height: 0;
    text-align: left;
    color: {color['text']};
    font-weight: 600;
}}
QPushButton#FacetHeader:hover {{ color: {color['accent']}; }}
QPushButton#FacetChip {{
    background-color: {color['card']};
    border: 1px solid {color['border']};
    border-radius: 6px;
    padding: 3px 8px;
    min-height: 20px;
    font-size: 12px;
    text-align: left;
}}
QPushButton#FacetChip:hover {{ border-color: {color['text']}; }}
QPushButton#FacetChip:checked {{
    background-color: {color['accent']};
    border-color: {color['accent']};
    color: {color['background']};
    font-weight: 700;
}}
QPushButton#FacetChip:disabled {{ color: {color['border']}; }}
/* ---- 【T-07】统一任务面板 ---- */
QWidget#TaskPanel {{
    background-color: {color['surface']};
    border: 1px solid {color['border']};
    border-radius: 8px;
}}
QFrame#TaskRow {{
    background-color: {color['card']};
    border-radius: 6px;
}}
QLabel#TaskRunning {{ color: {color['accent']}; font-weight: 700; }}
QLabel#TaskOk {{ color: {color['success']}; font-weight: 700; }}
QLabel#TaskFail {{ color: {color['danger']}; font-weight: 700; }}
QLabel#TaskMuted {{ color: {color['muted']}; }}
QPushButton#TaskCancel {{
    background: transparent;
    border: 1px solid {color['border']};
    border-radius: 6px;
    padding: 2px 8px;
    min-height: 0;
    color: {color['muted']};
    font-size: 12px;
}}
QPushButton#TaskCancel:hover {{ border-color: {color['danger']}; color: {color['danger']}; }}
QPushButton#TaskCancel:disabled {{ color: {color['border']}; border-color: {color['border']}; }}
/* ---- 【T-10】批量操作条 ---- */
QWidget#BatchBar {{
    background-color: {color['surface']};
    border: 1px solid {color['border']};
    border-radius: 8px;
    padding: 4px 6px;
}}
QLabel#BatchCount {{ color: {color['text']}; font-weight: 700; font-size: 12px; }}
QLabel#BatchMuted {{ color: {color['muted']}; font-size: 12px; }}
QPushButton#BatchLink {{
    background: transparent;
    border: none;
    color: {color['accent']};
    padding: 2px 6px;
    min-height: 0;
    font-size: 12px;
}}
QPushButton#BatchLink:disabled {{ color: {color['border']}; }}
QPushButton#BatchFlag, QPushButton#BatchStarApply {{
    background-color: {color['card']};
    border: 1px solid {color['border']};
    border-radius: 6px;
    padding: 2px 10px;
    min-height: 0;
    font-size: 12px;
}}
QPushButton#BatchFlag:hover, QPushButton#BatchStarApply:hover {{
    border-color: {color['accent']};
    color: {color['accent']};
}}
QPushButton#BatchFlag:disabled, QPushButton#BatchStarApply:disabled {{
    color: {color['border']};
    border-color: {color['border']};
}}
QComboBox#BatchStar {{
    border: 1px solid {color['border']};
    border-radius: 6px;
    padding: 2px 6px;
    min-height: 0;
    font-size: 12px;
    background-color: {color['card']};
}}
QComboBox#BatchStar:disabled {{ color: {color['border']}; }}
/* 危险动作：只有"清理已排除项"用，且不填色 —— 靠描边区分，避免误点 */
QPushButton#DangerButton {{
    background-color: {color['card']};
    border: 1px solid {color['danger']};
    border-radius: 6px;
    padding: 2px 10px;
    min-height: 0;
    font-size: 12px;
    color: {color['danger']};
}}
QPushButton#DangerButton:hover {{
    background-color: {color['danger']};
    color: {color['background']};
}}
QPushButton#DangerButton:disabled {{
    color: {color['border']};
    border-color: {color['border']};
}}
"""
