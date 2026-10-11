"""Professional four-stage shell and project lifecycle."""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from engine import config
from engine.log import get_logger
from engine.store import PhotoStore
from .pages.analyze_page import AnalyzePage
from .pages.export_page import ExportPage
from .pages.import_page import ImportPage
from .pages.review_page import ReviewPage

_log = get_logger("ui.main")


STAGES = (
    ("import", "1 导入"),
    ("analyze", "2 分析"),
    ("review", "3 复核"),
    ("export", "4 导出到 LR"),
)


class MainWindow(QMainWindow):
    def __init__(self, *, project_db: str | Path | None = None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Lumina Select · 光影选片助手")
        self.resize(1280, 800)
        self.setMinimumSize(960, 600)
        self.store = PhotoStore(str(project_db or config.DEFAULT_DB))
        self.current_stage_name = "import"

        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(16, 16, 16, 8)
        root_layout.setSpacing(12)
        header = QFrame()
        header.setObjectName("WorkflowHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 12, 16, 12)
        brand = QLabel("LUMINA SELECT")
        brand.setObjectName("PageTitle")
        header_layout.addWidget(brand)
        header_layout.addStretch(1)
        self.stage_buttons: list[QPushButton] = []
        for name, label in STAGES:
            button = QPushButton(label)
            button.setObjectName("StageButton")
            button.clicked.connect(lambda _checked=False, stage=name: self.goto(stage))
            header_layout.addWidget(button)
            self.stage_buttons.append(button)
        root_layout.addWidget(header)
        state_row = QHBoxLayout()
        self.stage_state_label = QLabel()
        self.stage_state_label.setObjectName("StageState")
        state_row.addWidget(self.stage_state_label)
        state_row.addStretch(1)
        # 【v0.6 S4-1 / G4】项目级兜底入口：旧库升级说明 + 已复核清单导出。
        # 两者都放在状态行而非弹窗自动弹出 —— 启动时不阻塞用户，
        # 需要时一点即得（旧库提示按钮只在确实检测到旧库时才出现）。
        self.upgrade_button = QPushButton("旧库升级说明")
        self.upgrade_button.setObjectName("UpgradeNotice")
        self.upgrade_button.clicked.connect(self.show_upgrade_notice)
        self.upgrade_button.setVisible(False)
        state_row.addWidget(self.upgrade_button)
        self.decisions_export_button = QPushButton("导出已复核清单")
        self.decisions_export_button.setObjectName("ExportDecisions")
        self.decisions_export_button.setToolTip(
            "把人工标星/保留排除/人工场景导出为 CSV 兜底清单"
            "（库文件损坏或被误删时仍能查到自己做过什么）"
        )
        self.decisions_export_button.clicked.connect(lambda: self.export_review_list())
        state_row.addWidget(self.decisions_export_button)
        root_layout.addLayout(state_row)

        self.stack = QStackedWidget()
        self.import_page = ImportPage()
        self.analyze_page = AnalyzePage()
        self.review_page = ReviewPage(self.store)
        self.export_page = ExportPage(self.store)
        self.pages = {
            "import": self.import_page,
            "analyze": self.analyze_page,
            "review": self.review_page,
            "export": self.export_page,
        }
        for page in self.pages.values():
            self.stack.addWidget(page)
        root_layout.addWidget(self.stack, 1)
        self.setCentralWidget(root)
        self.statusBar().showMessage("项目已保存 · 照片仅在本机处理")

        self.import_page.source_selected.connect(self.begin_analysis)
        self.analyze_page.succeeded.connect(self._analysis_succeeded)
        self.analyze_page.failed.connect(self.show_error)
        self.analyze_page.cancelled.connect(lambda: self.status("分析已取消"))
        self.goto("import")
        self.refresh_upgrade_notice()

    @property
    def current_page(self) -> QWidget:
        return self.stack.currentWidget()

    def goto(self, name: str) -> None:
        if name not in self.pages:
            raise ValueError(f"unknown workflow stage: {name}")
        self.current_stage_name = name
        if name == "review":
            self.review_page.reload()
        elif name == "export":
            self.export_page.reload()
        self.stack.setCurrentWidget(self.pages[name])
        current_index = list(self.pages).index(name)
        for index, button in enumerate(self.stage_buttons):
            state = "current" if index == current_index else "done" if index < current_index else "future"
            button.setProperty("state", state)
            button.style().unpolish(button)
            button.style().polish(button)
        self.stage_state_label.setText(f"当前：{STAGES[current_index][1]} · 项目已保存")

    def begin_analysis(self, source: str) -> None:
        # 【v0.6 S3-5 / P1-9】分析进行中再次触发（重复点选目录、双击）不得让
        # AnalyzePage.start() 的 RuntimeError 冒到 Qt 事件循环里 —— 旧实现下
        # 用户看不到任何提示，而 goto("analyze") 已经切走了页面。
        worker = self.analyze_page.worker
        if worker is not None and worker.isRunning():
            self.status("分析正在进行中，请等待完成或先取消后再更换目录")
            return
        source_path = Path(source)
        if not source_path.is_dir():
            self.show_error("请选择存在的照片目录")
            return
        self.store.set_meta("source_dir", str(source_path))
        self.goto("analyze")
        self.analyze_page.start(str(source_path), self.store.db_path)

    def _analysis_succeeded(self, result: dict) -> None:
        self.goto("review")
        # 重算之后旧后端记录已被覆盖：顺手刷新"旧库"入口，别让提示一直挂着。
        self.refresh_upgrade_notice(announce=False)
        degradations = result.get("degradations") or []
        upgrade = result.get("upgrade") or {}
        details: list[str] = []
        if upgrade.get("recomputed"):
            # 【v0.6 S4-1 / G4】重算必须说清楚：否则用户只会看到"分数换了一套"
            # 却不知道为什么，也无从确认自己的复核有没有被保住。
            legacy = upgrade.get("legacy_backends") or []
            origin = f"（原后端：{'、'.join(legacy)}）" if legacy else ""
            details.append(
                f"模型已更新，指标已重算{origin}；"
                f"人工复核 {upgrade.get('decisions_preserved', 0)} 条已保留"
            )
        if degradations:
            # 【v0.6 S2-2 / G3】降级必须对用户可见：否则用户会以为"AI 都算过了"，
            # 实际少了美学/场景这一半信号，却拿着一份看起来完全正常的甄选结果。
            first = degradations[0]
            detail = f"{first.get('stage', '未知阶段')}：{first.get('effect', '')}"
            if len(degradations) > 1:
                detail += f"（另有 {len(degradations) - 1} 项降级）"
            details.append(f"降级 · {detail}")
        head = "分析完成（降级）" if degradations else "分析完成"
        message = f"{head}：{result.get('total', 0)} 张"
        message += " · " + " · ".join(details) if details else "，等待人工复核"
        self.status(message)

    # ------------------------------------------------------------------
    # 【v0.6 S4-1 / G4】旧库升级提示 + 已复核清单导出（兜底）
    # ------------------------------------------------------------------
    def upgrade_report(self) -> dict:
        """读取本项目的旧库升级信息（读失败不阻断启动）。"""
        try:
            return self.store.upgrade_report()
        except Exception as error:          # noqa: BLE001 —— 提示失败不该拦住用户
            _log.warning("读取旧库升级信息失败（已忽略）：%s", error)
            return {}

    def refresh_upgrade_notice(self, *, announce: bool = True) -> None:
        """检测到旧库才显示升级入口，避免给新用户一个纯噪音按钮。"""
        report = self.upgrade_report()
        needs_attention = bool(report.get("needs_attention"))
        self.upgrade_button.setVisible(needs_attention)
        if needs_attention and announce:
            self.status("检测到旧项目：点「旧库升级说明」查看升级、备份与重算详情")

    def show_upgrade_notice(self) -> None:
        report = self.upgrade_report()
        if not report.get("needs_attention"):
            self.status("项目为当前版本，无需升级")
            return
        box = QMessageBox(self)
        box.setWindowTitle("旧项目升级说明")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(self.store.upgrade_summary())
        box.setInformativeText(
            "人工复核结果不会丢失（备份也留在原项目旁）；指标会在下次分析时重算。"
        )
        export = box.addButton("导出已复核清单", QMessageBox.ButtonRole.ActionRole)
        box.addButton("知道了", QMessageBox.ButtonRole.AcceptRole)
        box.exec()
        if box.clickedButton() is export:
            self.export_review_list()

    def export_review_list(self, target: str | None = None) -> None:
        """导出人工复核清单（CSV）。target 为空时弹保存对话框。"""
        if target is None:
            default = str(Path(self.store.db_path).with_name("已复核清单.csv"))
            chosen, _ = QFileDialog.getSaveFileName(
                self, "导出已复核清单", default, "CSV 文件 (*.csv)"
            )
            if not chosen:
                return
            target = chosen
        try:
            result = self.store.export_decisions(target)
        except Exception as error:          # noqa: BLE001
            _log.exception("导出已复核清单失败：%s", target)
            self.show_error(f"导出已复核清单失败：{error}")
            return
        self.status(f"已导出 {result['rows']} 条人工复核记录 → {result['path']}")

    def status(self, message: str) -> None:
        self.statusBar().showMessage(message)

    def show_error(self, message: str) -> None:
        self.statusBar().showMessage(f"错误：{message}")

    def closeEvent(self, event) -> None:
        worker = self.analyze_page.worker
        if worker is not None and worker.isRunning():
            worker.cancel()
            if not worker.wait(5000):
                self.show_error("分析仍在安全取消中，请稍后再退出")
                event.ignore()
                return
        if not self.export_page.shutdown():
            self.show_error("导出仍在完成当前文件，请稍后再退出")
            event.ignore()
            return
        self.review_page.shutdown()
        self.store.close()
        super().closeEvent(event)
