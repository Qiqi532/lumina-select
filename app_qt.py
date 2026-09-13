"""Lumina Select desktop entry point."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import traceback
import json
import tempfile

from PyQt6.QtWidgets import QApplication, QMessageBox

from ui.main_window import MainWindow
from ui.theme import stylesheet


def _install_exception_handler() -> None:
    original_hook = sys.excepthook

    def handle_exception(error_type, error, error_traceback) -> None:
        details = "".join(traceback.format_exception(error_type, error, error_traceback))
        app = QApplication.instance()
        if app is not None:
            QMessageBox.critical(None, "Lumina Select 错误", str(error))
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            try:
                log_dir = Path(local_app_data) / "Lumina Select"
                log_dir.mkdir(parents=True, exist_ok=True)
                with (log_dir / "crash.log").open("a", encoding="utf-8") as handle:
                    handle.write(details)
                    handle.write("\n")
            except OSError:
                pass
        original_hook(error_type, error, error_traceback)

    sys.excepthook = handle_exception


def main() -> int:
    _install_exception_handler()
    if "--smoke-dist" in sys.argv:
        position = sys.argv.index("--smoke-dist")
        report_path = Path(sys.argv[position + 1])
        from services.distribution_smoke import run_smoke_payload

        try:
            with tempfile.TemporaryDirectory(prefix="lumina-frozen-smoke-") as directory:
                report = run_smoke_payload(Path(directory))
            report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
            return 0 if all(report[key] for key in (
                "frozen", "started", "analyzed", "exported", "source_unchanged", "delivery_exists"
            )) else 1
        except Exception as error:
            report_path.write_text(json.dumps({
                "error": str(error), "traceback": traceback.format_exc(),
            }), encoding="utf-8")
            return 1
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    qss_path = Path(__file__).resolve().parent / "styles.qss"
    try:
        app.setStyleSheet(qss_path.read_text(encoding="utf-8"))
    except OSError:
        app.setStyleSheet(stylesheet())
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
