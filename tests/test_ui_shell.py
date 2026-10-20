from __future__ import annotations

from PySide6.QtWidgets import QApplication

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


# ---------------------------------------------------------------------------
# 【v0.6 S3-8】worker 异常不得"只传一行文案给界面"
# ---------------------------------------------------------------------------
def test_analyze_worker_failure_is_logged_with_traceback(monkeypatch, caplog):
    """界面拿到 str(error) 就够用户读，但事后复盘需要完整 traceback 落在日志里。"""
    from engine import pipeline

    def boom(source, db_path, **kwargs):      # noqa: ARG001
        raise RuntimeError("no space left on device")

    monkeypatch.setattr(pipeline, "analyze_directory", boom)
    worker = AnalyzeWorker("C:/photos", "C:/project.db")
    seen = []
    worker.failed.connect(seen.append)

    with caplog.at_level("ERROR"):
        worker.run()

    assert seen == ["no space left on device"], "界面仍应拿到一行可读文案"
    assert "分析失败" in caplog.text
    assert "no space left on device" in caplog.text
    assert "Traceback" in caplog.text, "缺少 traceback ⇒ 出问题后无法定位"


# ---------------------------------------------------------------------------
# 【v0.6 S4-1 / G4】旧库升级入口 + 已复核清单导出（兜底）
# ---------------------------------------------------------------------------
def _legacy_v04_db(path):
    import sqlite3

    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE photos (path TEXT PRIMARY KEY, fname TEXT, star INT, label TEXT)"
    )
    connection.execute(
        "INSERT INTO photos VALUES (?, ?, ?, ?)", ("C:/p/IMG_0001.CR3", "IMG_0001.CR3", 5, "P")
    )
    connection.commit()
    connection.close()


def test_upgrade_notice_appears_only_for_legacy_project(qtbot, tmp_path):
    legacy = tmp_path / "legacy.db"
    _legacy_v04_db(legacy)

    window = MainWindow(project_db=legacy)
    qtbot.addWidget(window)
    try:
        report = window.upgrade_report()
        assert report["schema_upgraded"] is True
        assert report["backup_path"]
        # isVisibleTo：窗口没 show() 时 isVisible() 恒为 False，用它才问得对
        assert window.upgrade_button.isVisibleTo(window) is True
        # 兜底入口始终可用，不该藏在弹窗深处
        assert window.decisions_export_button.isVisibleTo(window) is True
    finally:
        window.close()


def test_upgrade_notice_hidden_for_current_project(qtbot, tmp_path):
    window = MainWindow(project_db=tmp_path / "new.db")
    qtbot.addWidget(window)
    try:
        assert window.upgrade_report()["needs_attention"] is False
        assert window.upgrade_button.isVisibleTo(window) is False
    finally:
        window.close()


def test_export_review_list_without_dialog_writes_csv(qtbot, tmp_path):
    window = MainWindow(project_db=tmp_path / "p.db")
    qtbot.addWidget(window)
    try:
        window.store.upsert_photos_batch([
            {"path": "C:/p/IMG_0001.CR3", "star": 5, "label": "P",
             "decision_source": "manual"},
            {"path": "C:/p/IMG_0002.CR3", "star": 0},
        ])
        target = tmp_path / "已复核清单.csv"
        window.export_review_list(str(target))

        assert target.exists()
        assert "已导出 1 条" in window.statusBar().currentMessage()
    finally:
        window.close()


def test_analysis_status_reports_recompute_and_preserved_decisions(qtbot, tmp_path):
    """重算必须说清楚原因与"你的复核还在"，否则用户只会看到分数变了一套。"""
    window = MainWindow(project_db=tmp_path / "p.db")
    qtbot.addWidget(window)
    try:
        window._analysis_succeeded({
            "total": 8,
            "upgrade": {"recomputed": True, "decisions_preserved": 3,
                        "legacy_backends": ["heuristic"]},
        })
        message = window.statusBar().currentMessage()
        assert "重算" in message
        assert "3 条" in message
        assert "heuristic" in message
    finally:
        window.close()


# ---------------------------------------------------------------------------
# 【v0.6 S4-1 / G4】旧库升级入口 + 已复核清单导出（兜底）
# ---------------------------------------------------------------------------
def _legacy_v04_db(path):
    import sqlite3

    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE photos (path TEXT PRIMARY KEY, fname TEXT, star INT, label TEXT)"
    )
    connection.execute(
        "INSERT INTO photos VALUES (?, ?, ?, ?)", ("C:/p/IMG_0001.CR3", "IMG_0001.CR3", 5, "P")
    )
    connection.commit()
    connection.close()


def test_upgrade_notice_appears_only_for_legacy_project(qtbot, tmp_path):
    legacy = tmp_path / "legacy.db"
    _legacy_v04_db(legacy)

    window = MainWindow(project_db=legacy)
    qtbot.addWidget(window)
    try:
        report = window.upgrade_report()
        assert report["schema_upgraded"] is True
        assert report["backup_path"]
        # isVisibleTo：窗口没 show() 时 isVisible() 恒为 False，用它才问得对
        assert window.upgrade_button.isVisibleTo(window) is True
        # 兜底入口始终可用，不该藏在弹窗深处
        assert window.decisions_export_button.isVisibleTo(window) is True
    finally:
        window.close()


def test_upgrade_notice_hidden_for_current_project(qtbot, tmp_path):
    window = MainWindow(project_db=tmp_path / "new.db")
    qtbot.addWidget(window)
    try:
        assert window.upgrade_report()["needs_attention"] is False
        assert window.upgrade_button.isVisibleTo(window) is False
    finally:
        window.close()


def test_export_review_list_without_dialog_writes_csv(qtbot, tmp_path):
    window = MainWindow(project_db=tmp_path / "p.db")
    qtbot.addWidget(window)
    try:
        window.store.upsert_photos_batch([
            {"path": "C:/p/IMG_0001.CR3", "star": 5, "label": "P",
             "decision_source": "manual"},
            {"path": "C:/p/IMG_0002.CR3", "star": 0},
        ])
        target = tmp_path / "已复核清单.csv"
        window.export_review_list(str(target))

        assert target.exists()
        assert "已导出 1 条" in window.statusBar().currentMessage()
    finally:
        window.close()


def test_analysis_status_reports_recompute_and_preserved_decisions(qtbot, tmp_path):
    """重算必须说清楚原因与"你的复核还在"，否则用户只会看到分数变了一套。"""
    window = MainWindow(project_db=tmp_path / "p.db")
    qtbot.addWidget(window)
    try:
        window._analysis_succeeded({
            "total": 8,
            "upgrade": {"recomputed": True, "decisions_preserved": 3,
                        "legacy_backends": ["heuristic"]},
        })
        message = window.statusBar().currentMessage()
        assert "重算" in message
        assert "3 条" in message
        assert "heuristic" in message
    finally:
        window.close()
