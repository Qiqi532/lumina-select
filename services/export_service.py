"""Recoverable Lightroom delivery pipeline, independent of Qt."""
from __future__ import annotations

from dataclasses import dataclass
import errno
import os
from pathlib import Path
import shutil
import tempfile
from typing import Callable, Literal
from uuid import uuid4

from engine.config import RAW_EXTS
from engine.store import PhotoStore
from .asset_pairing import AssetPair
from .metadata_writer import MetadataWriter
from .xmp_service import XmpSelection, write_sidecar


AssetMode = Literal["raw", "raw+jpeg", "jpeg"]
WriteMode = Literal["copy", "in-place-xmp"]
Status = Literal["success", "skipped", "failed"]
CAMERA_RAW_EXTS = {extension.casefold() for extension in RAW_EXTS} - {".dng"}
EMBEDDED_XMP_EXTS = {".jpg", ".jpeg", ".tif", ".tiff", ".psd", ".dng"}


@dataclass(frozen=True, slots=True)
class ExportRequest:
    target_dir: Path
    asset_mode: AssetMode
    write_mode: WriteMode
    selected_asset_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ExportItemResult:
    source: Path
    target: Path | None
    xmp_path: Path | None
    status: Status
    error_code: str | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True)
class ExportPlan:
    asset_count: int
    file_count: int
    xmp_count: int
    total_bytes: int
    nonempty_target: bool
    conflicts: tuple[Path, ...]
    affected_paths: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class _Candidate:
    source: Path
    target: Path
    xmp_path: Path | None
    decision: dict
    problem: str | None = None


def _error_code(error: BaseException) -> str:
    if isinstance(error, PermissionError):
        return "permission_denied"
    if isinstance(error, OSError) and error.errno == errno.ENOSPC:
        return "disk_full"
    if isinstance(error, FileNotFoundError):
        return "decode_error"
    return "unknown"


class ExportService:
    def __init__(
        self,
        store: PhotoStore,
        assets: list[AssetPair],
        *,
        metadata_writer: MetadataWriter | None = None,
        xmp_writer: Callable = write_sidecar,
        min_star: int = 4,
        disk_usage: Callable = shutil.disk_usage,
        copier: Callable = shutil.copy2,
    ) -> None:
        self.store = store
        self.assets = {asset.asset_id: asset for asset in assets}
        self.metadata_writer = metadata_writer
        self.xmp_writer = xmp_writer
        self.min_star = min_star
        self.disk_usage = disk_usage
        self.copier = copier

    def _asset_problem(self, asset: AssetPair, rows: dict[str, dict]) -> str | None:
        members = [path for path in (asset.raw_path, asset.jpeg_path) if path is not None]
        decisions = [rows.get(str(path), {}) for path in members]
        if any(row.get("label") == "X" for row in decisions):
            return "excluded"
        if any(
            row.get("decision_source")
            and (row.get("label") == "P" or int(row.get("star") or 0) >= self.min_star)
            for row in decisions
        ):
            return None
        return "not_reviewed"

    @staticmethod
    def _selection(row: dict) -> XmpSelection:
        reasons_text = row.get("waste_reasons") or ""
        reasons = tuple(item.strip() for item in reasons_text.split(",") if item.strip())
        return XmpSelection(
            rating=max(0, min(5, int(row.get("star") or 0))),
            label=row.get("label"),
            keywords=(),
            app_version="0.5.0",
            backend=row.get("analysis_backend") or "unknown",
            reasons=reasons,
        )

    def _candidates(self, request: ExportRequest) -> list[_Candidate]:
        rows = self.store.photos_map()
        candidates: list[_Candidate] = []
        for asset_id in request.selected_asset_ids:
            asset = self.assets.get(asset_id)
            if asset is None:
                continue
            members, missing = asset.members_for_mode(request.asset_mode)
            problem = self._asset_problem(asset, rows) or missing
            if not members:
                source = asset.preview_path
                candidates.append(
                    _Candidate(source, request.target_dir / source.name, None, rows.get(str(source), {}), problem)
                )
                continue
            for source in members:
                target = request.target_dir / source.name
                if request.write_mode == "in-place-xmp":
                    target = source
                sidecar = (
                    target.with_suffix(".xmp")
                    if source.suffix.casefold() in CAMERA_RAW_EXTS
                    else None
                )
                candidates.append(
                    _Candidate(source, target, sidecar, rows.get(str(source), {}), problem)
                )
        return candidates

    def _record(self, item: ExportItemResult) -> ExportItemResult:
        self.store.record_export_item(
            str(item.source),
            str(item.target) if item.target is not None else "",
            str(item.xmp_path) if item.xmp_path is not None else None,
            item.status,
            item.error_code,
        )
        return item

    def plan(self, request: ExportRequest) -> ExportPlan:
        candidates = self._candidates(request)
        eligible = [item for item in candidates if item.problem is None]
        conflicts = tuple(
            item.target
            for item in eligible
            if request.write_mode == "copy" and item.target.exists()
        )
        affected = tuple(
            item.xmp_path
            for item in eligible
            if item.xmp_path is not None
        )
        return ExportPlan(
            asset_count=len(set(request.selected_asset_ids) & self.assets.keys()),
            file_count=len(eligible),
            xmp_count=sum(
                item.xmp_path is not None
                or item.source.suffix.casefold() in EMBEDDED_XMP_EXTS
                for item in eligible
            ),
            total_bytes=sum(
                item.source.stat().st_size for item in eligible if item.source.is_file()
            ) if request.write_mode == "copy" else 0,
            nonempty_target=(
                request.target_dir.is_dir() and any(request.target_dir.iterdir())
                if request.write_mode == "copy" else False
            ),
            conflicts=conflicts,
            affected_paths=affected,
        )

    @staticmethod
    def _copy_atomic(source: Path, target: Path, copier: Callable) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".lumina-part", dir=target.parent
        )
        os.close(descriptor)
        temporary = Path(name)
        try:
            copier(source, temporary)
            with temporary.open("ab") as handle:
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def _write_xmp(
        self, candidate: _Candidate, request: ExportRequest
    ) -> ExportItemResult:
        selection = self._selection(candidate.decision)
        if candidate.xmp_path is not None:
            kwargs = {}
            if request.write_mode == "in-place-xmp":
                project_uuid = self.store.get_meta("project_uuid")
                if project_uuid is None:
                    project_uuid = str(uuid4())
                    self.store.set_meta("project_uuid", project_uuid)
                kwargs = {"in_place": True, "project_uuid": project_uuid}
            result = self.xmp_writer(candidate.xmp_path, selection, **kwargs)
        elif candidate.source.suffix.casefold() in EMBEDDED_XMP_EXTS:
            if request.write_mode == "in-place-xmp":
                return ExportItemResult(
                    candidate.source, candidate.target, None, "failed",
                    "unsupported_in_place_non_raw", "non-RAW source files are immutable",
                )
            if self.metadata_writer is None:
                return ExportItemResult(
                    candidate.source, candidate.target, None, "failed",
                    "unknown", "ExifTool writer is not configured",
                )
            result = self.metadata_writer.write(candidate.target, selection)
        else:
            return ExportItemResult(
                candidate.source, candidate.target, None, "failed",
                "unsupported_format", "no safe XMP write path for this format",
            )
        if not result.success:
            return ExportItemResult(
                candidate.source, candidate.target, candidate.xmp_path,
                "failed", result.error_code or "unknown", result.message,
            )
        return ExportItemResult(
            candidate.source, candidate.target, candidate.xmp_path, "success"
        )

    def _execute_one(
        self, candidate: _Candidate, request: ExportRequest
    ) -> ExportItemResult:
        if candidate.problem:
            return ExportItemResult(
                candidate.source, candidate.target, candidate.xmp_path,
                "skipped", candidate.problem, candidate.problem,
            )
        if request.write_mode == "in-place-xmp":
            return self._write_xmp(candidate, request)
        if candidate.target.exists() or (
            candidate.xmp_path is not None and candidate.xmp_path.exists()
        ):
            return ExportItemResult(
                candidate.source, candidate.target, candidate.xmp_path,
                "failed", "target_conflict", "delivery target already exists",
            )
        copied = False
        try:
            self._copy_atomic(candidate.source, candidate.target, self.copier)
            copied = True
            result = self._write_xmp(candidate, request)
            if result.status != "success":
                candidate.target.unlink(missing_ok=True)
            return result
        except OSError as error:
            if copied:
                candidate.target.unlink(missing_ok=True)
            return ExportItemResult(
                candidate.source, candidate.target, candidate.xmp_path,
                "failed", _error_code(error), str(error),
            )

    def run(
        self,
        request: ExportRequest,
        progress: Callable[[int, int], None] | None = None,
        *,
        only_sources: set[str] | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> list[ExportItemResult]:
        if request.write_mode not in {"copy", "in-place-xmp"}:
            raise ValueError(f"unsupported write mode: {request.write_mode}")
        candidates = self._candidates(request)
        if only_sources is not None:
            candidates = [item for item in candidates if str(item.source) in only_sources]
        total = len(candidates)
        if not total:
            return []

        if request.write_mode == "copy":
            bytes_needed = sum(
                item.source.stat().st_size
                for item in candidates
                if item.problem is None and item.source.exists()
            )
            volume = request.target_dir
            while not volume.exists() and volume != volume.parent:
                volume = volume.parent
            if self.disk_usage(volume).free < bytes_needed:
                results = []
                for index, candidate in enumerate(candidates, 1):
                    results.append(self._record(ExportItemResult(
                        candidate.source, candidate.target, candidate.xmp_path,
                        "failed", "disk_full", "insufficient free space before copying",
                    )))
                    if progress is not None:
                        progress(index, total)
                return results

        results = []
        for index, candidate in enumerate(candidates, 1):
            if cancel_check is not None and cancel_check():
                result = ExportItemResult(
                    candidate.source, candidate.target, candidate.xmp_path,
                    "skipped", "cancelled", "export cancelled before this file",
                )
            else:
                result = self._execute_one(candidate, request)
            results.append(self._record(result))
            if progress is not None:
                progress(index, total)
        return results

    def retry_failed(
        self,
        request: ExportRequest,
        progress: Callable[[int, int], None] | None = None,
        *,
        cancel_check: Callable[[], bool] | None = None,
    ) -> list[ExportItemResult]:
        requested = {
            (str(item.source), str(item.target)) for item in self._candidates(request)
        }
        failed = {
            row["path"]
            for row in self.store.failed_export_items()
            if (row["path"], row["target_path"]) in requested
        }
        return self.run(
            request, progress, only_sources=failed, cancel_check=cancel_check
        )
