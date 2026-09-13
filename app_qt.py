"""Lumina Select desktop entry point."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import traceback

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
