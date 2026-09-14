from __future__ import annotations

from PyQt6.QtWidgets import QApplication

from ui.main_window import MainWindow
from ui.pages.analyze_page import AnalyzePage, AnalyzeWorker
from ui.pages.import_page import ImportPage
from ui.theme import COLORS, SPACING, stylesheet


def test_theme_has_approved_tokens_and_focus_indicator():
    assert COLORS["background"] == "#111315"
    assert COLORS["accent"] == "#D7AA5A"
    assert SPACING == (4, 8, 12, 16, 24)
    qss = stylesheet()
    assert "#D7AA5A" in qss
    assert ":focus" in qss


def test_four_stage_header_visible_at_1280_by_720(qtbot, tmp_path):
    window = MainWindow(project_db=tmp_path / "project.db")
    qtbot.addWidget(window)
    window.resize(1280, 720)
    window.show()
    QApplication.processEvents()

    assert window.width() == 1280
    assert window.height() == 720
    assert [button.text() for button in window.stage_buttons] == [
        "1 导入", "2 分析", "3 复核", "4 导出到 LR"
    ]
    assert all(button.isVisible() for button in window.stage_buttons)
    assert window.stage_buttons[0].property("state") == "current"
    assert window.current_stage_name == "import"
    assert "当前" in window.stage_state_label.text()


def test_import_analyze_switch_keeps_project_store(qtbot, tmp_path):
    window = MainWindow(project_db=tmp_path / "project.db")
    qtbot.addWidget(window)
    original_store = window.store
    original_store.set_meta("fixture", "retained")

    window.goto("analyze")
    assert isinstance(window.current_page, AnalyzePage)
    assert window.store is original_store
    window.goto("import")
    assert isinstance(window.current_page, ImportPage)
    assert window.store.get_meta("fixture") == "retained"


def test_analyze_worker_includes_raw_assets(monkeypatch):
    from engine import pipeline

    calls = []

    def fake_analyze(source, db_path, **kwargs):
        calls.append((source, db_path, kwargs))
        return {"total": 1}

    monkeypatch.setattr(pipeline, "analyze_directory", fake_analyze)
    worker = AnalyzeWorker("C:/photos", "C:/project.db")
    worker.run()

    assert len(calls) == 1
    assert calls[0][2]["include_raw"] is True


def test_primary_button_size_hint_fits_text_at_150_percent(qtbot, tmp_path):
    window = MainWindow(project_db=tmp_path / "project.db")
    qtbot.addWidget(window)
    button = window.import_page.primary_button
    font = button.font()
    font.setPointSizeF(font.pointSizeF() * 1.5)
    button.setFont(font)
    text_width = button.fontMetrics().horizontalAdvance(button.text())

    assert button.sizeHint().width() >= text_width + 24


def test_close_cancels_and_waits_for_running_worker(qtbot, tmp_path):
    window = MainWindow(project_db=tmp_path / "project.db")
    qtbot.addWidget(window)

    class FakeWorker:
        cancelled = False
        waited = False

        def isRunning(self):
            return True

        def cancel(self):
            self.cancelled = True

        def wait(self, _timeout):
            self.waited = True
            return True

    worker = FakeWorker()
    window.analyze_page.worker = worker
    window.close()
    assert worker.cancelled and worker.waited
