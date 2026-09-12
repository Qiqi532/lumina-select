"""ExifTool adapter for XMP embedded in non-RAW delivery copies."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Callable

from .xmp_service import XmpSelection


SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".tif", ".tiff", ".psd", ".dng"}
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass(frozen=True, slots=True)
class MetadataWriteResult:
    target_path: Path
    success: bool
    error_code: str | None = None
    message: str | None = None


class MetadataWriter:
    def __init__(
        self,
        exiftool_path: str | Path,
        delivery_root: str | Path,
        *,
        runner: Callable = subprocess.run,
        timeout: int = 30,
        config_path: str | Path | None = None,
    ) -> None:
        self.exiftool_path = Path(exiftool_path)
        self.delivery_root = Path(delivery_root).resolve(strict=False)
        self.runner = runner
        self.timeout = timeout
        self.config_path = Path(config_path) if config_path is not None else None

    def _base_args(self) -> list[str]:
        args = [str(self.exiftool_path)]
        if self.config_path is not None:
            args.extend(["-config", str(self.config_path)])
        args.extend(["-charset", "filename=utf8"])
        return args

    def _run(self, args: list[str]):
        return self.runner(
            args,
            shell=False,
            timeout=self.timeout,
            creationflags=CREATE_NO_WINDOW,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    def _write_args(self, path: Path, selection: XmpSelection) -> list[str]:
        args = [
            "-overwrite_original",
            f"-XMP-xmp:Rating={selection.rating}",
            f"-XMP-xmp:Label={selection.label or ''}",
            "-XMP-dc:Subject=",
        ]
        args.extend(f"-XMP-dc:Subject+={keyword}" for keyword in selection.keywords)
        args.extend(
            [
                f"-XMP-lumina:AppVersion={selection.app_version}",
                f"-XMP-lumina:Backend={selection.backend}",
                "-XMP-lumina:Reasons=",
            ]
        )
        args.extend(f"-XMP-lumina:Reasons+={reason}" for reason in selection.reasons)
        args.append(str(path))
        return args

    @staticmethod
    def _normalized_readback(payload: str) -> dict:
        decoded = json.loads(payload)
        if not isinstance(decoded, list) or not decoded or not isinstance(decoded[0], dict):
            raise ValueError("ExifTool readback was not a JSON object")
        return {key.rsplit(":", 1)[-1]: value for key, value in decoded[0].items()}

    @staticmethod
    def _matches(selection: XmpSelection, metadata: dict) -> bool:
        try:
            rating = int(metadata.get("Rating"))
        except (TypeError, ValueError):
            return False
        subject = metadata.get("Subject", [])
        if isinstance(subject, str):
            subject = [subject]
        return (
            rating == selection.rating
            and metadata.get("Label", "") == (selection.label or "")
            and {str(value) for value in selection.keywords} <= {str(value) for value in subject}
        )

    def write(
        self,
        target_path: str | Path,
        selection: XmpSelection,
        *,
        in_place: bool = False,
    ) -> MetadataWriteResult:
        target = Path(target_path)
        if in_place:
            return MetadataWriteResult(
                target, False, "unsupported_in_place_non_raw", "non-RAW source files are immutable"
            )
        resolved = target.resolve(strict=False)
        try:
            resolved.relative_to(self.delivery_root)
        except ValueError:
            return MetadataWriteResult(
                target, False, "outside_delivery_root", "target is outside delivery root"
            )
        if target.suffix.casefold() not in SUPPORTED_EXTENSIONS:
            return MetadataWriteResult(
                target, False, "unsupported_format", f"unsupported metadata format: {target.suffix}"
            )
        if not target.is_file():
            return MetadataWriteResult(target, False, "target_missing", "delivery copy is missing")

        temporary_path = target.with_name(f".{target.name}.lumina-meta-part")
        args_path: Path | None = None
        try:
            shutil.copy2(target, temporary_path)
            descriptor, name = tempfile.mkstemp(
                prefix=".lumina-exiftool-", suffix=".args", dir=target.parent
            )
            args_path = Path(name)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                for argument in self._write_args(temporary_path, selection):
                    handle.write(argument)
                    handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())

            write_result = self._run([*self._base_args(), "-@", str(args_path)])
            if write_result.returncode != 0:
                return MetadataWriteResult(
                    target, False, "exiftool_failed", write_result.stderr.strip() or "ExifTool failed"
                )
            read_result = self._run(
                [*self._base_args(), "-j", "-XMP:all", str(temporary_path)]
            )
            if read_result.returncode != 0:
                return MetadataWriteResult(
                    target, False, "verification_failed", read_result.stderr.strip() or "readback failed"
                )
            try:
                metadata = self._normalized_readback(read_result.stdout)
            except (json.JSONDecodeError, ValueError) as error:
                return MetadataWriteResult(target, False, "verification_failed", str(error))
            if not self._matches(selection, metadata):
                return MetadataWriteResult(
                    target, False, "verification_failed", "written XMP did not match requested selection"
                )

            with temporary_path.open("ab") as handle:
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, target)
            return MetadataWriteResult(target, True)
        except subprocess.TimeoutExpired as error:
            return MetadataWriteResult(target, False, "timeout", str(error))
        except PermissionError as error:
            return MetadataWriteResult(target, False, "permission_denied", str(error))
        except OSError as error:
            return MetadataWriteResult(target, False, "unknown", str(error))
        finally:
            temporary_path.unlink(missing_ok=True)
            if args_path is not None:
                args_path.unlink(missing_ok=True)
