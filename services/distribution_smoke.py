"""Isolated frozen-app start/analyze/export probe; never uses a user's photos."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import sys

import numpy as np
from PIL import Image
from PyQt6.QtWidgets import QApplication

from engine import config, inference
from engine.pipeline import analyze_directory
from engine.store import PhotoStore
from services.asset_pairing import assets_from_rows
from services.export_service import ExportRequest, ExportService
from services.metadata_writer import MetadataWriter
from ui.main_window import MainWindow


def run_smoke_payload(root: Path, *, backend: str | None = None) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    if backend is not None:
        config.INFERENCE_BACKEND = backend
        inference.reset_cache()
    app = QApplication.instance() or QApplication([])
    db_path = root / "smoke.db"
    window = MainWindow(project_db=db_path)
    window.show()
    app.processEvents()
    started = window.isVisible() and window.current_stage_name == "import"
    window.close()

    source_dir = root / "source"
    source_dir.mkdir()
    source = source_dir / "smoke.jpg"
    pixels = np.indices((128, 128)).sum(axis=0).astype(np.uint8)
    Image.fromarray(np.stack((pixels, np.roll(pixels, 17, 0), np.roll(pixels, 31, 1)), axis=2)).save(source)
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    with ThreadPoolExecutor(max_workers=1) as pool:
        analyzed = pool.submit(analyze_directory, str(source_dir), str(db_path), use_faces=False).result()

    delivery = root / "delivery"
    runtime_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    if getattr(sys, "frozen", False):
        executable = runtime_root / "exiftool/exiftool.exe"
        config_path = runtime_root / "exiftool-lumina.config"
    else:
        executable = runtime_root / "release/exiftool/exiftool.exe"
        config_path = runtime_root / "release/exiftool-lumina.config"
    with PhotoStore(str(db_path)) as store:
        store.update_photo(str(source), star=5, label="P", decision_source="manual")
        assets = assets_from_rows(store.all_photos())
    request = ExportRequest(delivery, "jpeg", "copy", (assets[0].asset_id,))

    def export_in_worker():
        with PhotoStore(str(db_path)) as worker_store:
            service = ExportService(
                worker_store, assets,
                metadata_writer=MetadataWriter(executable, delivery, config_path=config_path),
            )
            return service.run(request)

    with ThreadPoolExecutor(max_workers=1) as pool:
        results = pool.submit(export_in_worker).result()
    return {
        "frozen": bool(getattr(sys, "frozen", False)),
        "backend": config.INFERENCE_BACKEND,
        "started": started,
        "analyzed": analyzed["new_analyzed"],
        "exported": sum(item.status == "success" for item in results),
        "export_results": [item.status for item in results],
        "export_errors": [
            {"code": item.error_code, "message": item.message}
            for item in results if item.status != "success"
        ],
        "source_unchanged": hashlib.sha256(source.read_bytes()).hexdigest() == source_hash,
        "delivery_exists": (delivery / source.name).is_file(),
    }
