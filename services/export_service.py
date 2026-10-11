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
from engine.log import get_logger
from engine.store import PhotoStore
from .asset_pairing import AssetPair
from .metadata_writer import MetadataWriter
from .xmp_service import XmpSelection, write_sidecar


# 【v0.6 S3-8】必须用项目统一 logger（engine.log 会给根 logger 挂控制台 + 文件 handler），
# 直接 logging.getLogger(__name__) 的记录没有任何 handler 承接，只会落到 stderr 的
# lastResort，永远不会进 smart_cull.log —— 等于没记。
_log = get_logger("export")


AssetMode = Literal["raw", "raw+jpeg", "jpeg"]
WriteMode = Literal["copy", "in-place-xmp"]
Status = Literal["success", "skipped", "failed"]
CAMERA_RAW_EXTS = {extension.casefold() for extension in RAW_EXTS} - {".dng"}
# 可直接把 XMP 写进文件本身的格式（非 RAW 走 embedded、RAW 走 sidecar）。
# 【P1-2】必须与 engine.config.JPEG_PNG_EXTS 对齐：扫描既然收 .png，导出就得能交付它。
# 旧实现漏了 .png，导致 PNG 走 unsupported_format：copy 模式下已复制的交付副本还会被
# unlink，用户既没拿到照片、也没拿到元数据。
EMBEDDED_XMP_EXTS = {".jpg", ".jpeg", ".tif", ".tiff", ".psd", ".png", ".dng"}


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


def _target_key(path: Path) -> str:
    """同名目标判定的规范化键（Windows 下大小写不敏感，避免 A.JPG / a.jpg 漏判）。"""
    return os.path.normcase(str(path))


def _dedupe_paths(paths: list[Path]) -> tuple[Path, ...]:
    """按出现顺序去重（同一目标既是「已存在」又是「批次内重复」时只报一次）。"""
    seen: set[str] = set()
    unique: list[Path] = []
    for path in paths:
        key = _target_key(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return tuple(unique)


def _best_effort_unlink(path: Path) -> None:
    """尽力删除交付/临时文件；失败只记日志，绝不向上抛。

    【v0.6 S3-3 / P2-16】清理动作本身也可能 OSError（文件被占用等），
    若让它冒泡会把整批导出带崩 —— 清理失败只应降级为一条日志。
    """
    try:
        path.unlink(missing_ok=True)
    except OSError:
        _log.warning("清理交付/临时文件失败（已忽略）：%s", path, exc_info=True)


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
        conflicts: list[Path] = []
        if request.write_mode == "copy":
            # ① 目标位置已存在同名文件
            conflicts.extend(item.target for item in eligible if item.target.exists())
            # ② 【v0.6 S3-7 / P1-6】批次内同名冲突：来自不同来源目录的同名文件会映射到
            #    同一个目标（A/IMG_0001.CR3 与 B/IMG_0001.CR3 → 同一 target），真实导出时
            #    第二个必然撞上 target_conflict。必须在**预览阶段**就暴露。
            #    同时要覆盖 sidecar 冲突：X.CR3 与 X.NEF 目标不同，但都写 X.xmp。
            counts: dict[str, int] = {}
            for item in eligible:
                for output in (item.target, item.xmp_path):
                    if output is not None:
                        key = _target_key(output)
                        counts[key] = counts.get(key, 0) + 1
            for item in eligible:
                for output in (item.target, item.xmp_path):
                    if output is not None and counts[_target_key(output)] > 1:
                        conflicts.append(output)
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
            conflicts=_dedupe_paths(conflicts),
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
            # 【v0.6 S3-3】清理必须是 best-effort：残留 `.lumina-part` 也不能把整批带崩
            _best_effort_unlink(temporary)

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
            # 【v0.6 S3-3 / P1-7】旧实现只为 copy 模式兜了 OSError；in-place 模式下
            # 单个文件的权限/磁盘错误会沿 run() 的循环抛出，**整批导出中止且不留记录**。
            try:
                return self._write_xmp(candidate, request)
            except OSError as error:
                return ExportItemResult(
                    candidate.source, candidate.target, candidate.xmp_path,
                    "failed", _error_code(error), str(error),
                )
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
                _best_effort_unlink(candidate.target)
            return result
        except OSError as error:
            if copied:
                _best_effort_unlink(candidate.target)
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
                # 【v0.6 S3-3 / S3-8】交付是逐文件流程：任何单点异常都必须降级为该文件的
                # failed 记录 + 一条带 traceback 的日志，绝不能中断整批（旧实现里
                # in-place 的 OSError、selector 的 ValueError 都会直接抛穿 run()）。
                try:
                    result = self._execute_one(candidate, request)
                except Exception as error:
                    _log.exception("导出单文件失败，已跳过：%s", candidate.source)
                    result = ExportItemResult(
                        candidate.source, candidate.target, candidate.xmp_path,
                        "failed", _error_code(error), str(error),
                    )
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
