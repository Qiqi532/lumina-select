"""Verify and stage the fixed ExifTool Windows runtime from its locked archive."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def verify_archive(archive: Path, lock: dict) -> bool:
    if not archive.is_file() or archive.stat().st_size != lock["archive_size"]:
        return False
    with archive.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    return digest.casefold() == lock["archive_sha256"].casefold()


def _runtime_entries(bundle: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    entries = {}
    for info in bundle.infolist():
        path = PurePosixPath(info.filename.replace("\\", "/"))
        if info.is_dir() or ".." in path.parts:
            continue
        parts = path.parts
        exe_index = next((i for i, part in enumerate(parts) if part.lower() in {"exiftool.exe", "exiftool(-k).exe"}), None)
        files_index = next((i for i, part in enumerate(parts) if part.lower() == "exiftool_files"), None)
        if exe_index is not None and exe_index == len(parts) - 1:
            entries["exiftool.exe"] = info
        elif files_index is not None and files_index < len(parts) - 1:
            entries["/".join(parts[files_index:])] = info
    if "exiftool.exe" not in entries or not any(name.startswith("exiftool_files/") for name in entries):
        raise ValueError("archive lacks ExifTool executable or support files")
    return entries


def stage_runtime(archive: Path, destination: Path, lock: dict) -> None:
    if not verify_archive(archive, lock):
        raise ValueError("ExifTool archive SHA-256 or size does not match lock")
    with zipfile.ZipFile(archive) as bundle:
        entries = _runtime_entries(bundle)
        if destination.exists():
            if not destination.is_dir():
                raise ValueError("runtime destination is not a directory")
            staged_files = {
                path.relative_to(destination).as_posix()
                for path in destination.rglob("*") if path.is_file()
            }
            if staged_files != set(entries):
                raise ValueError("staged ExifTool runtime contains missing or extra files")
            for relative, info in entries.items():
                target = destination / Path(relative)
                if not target.is_file() or hashlib.sha256(target.read_bytes()).digest() != hashlib.sha256(bundle.read(info)).digest():
                    raise ValueError(f"staged ExifTool differs from locked archive: {relative}")
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="exiftool-stage-", dir=destination.parent) as temporary:
            staged = Path(temporary) / "exiftool"
            for relative, info in entries.items():
                target = staged / Path(relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(info) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
            staged.replace(destination)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", type=Path, default=ROOT / "release/exiftool.lock.json")
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--stage", type=Path, default=ROOT / "release/exiftool")
    args = parser.parse_args(argv)
    lock = json.loads(args.lock.read_text(encoding="utf-8"))
    archive = args.archive or ROOT / "release" / lock["archive"]
    try:
        stage_runtime(archive, args.stage, lock)
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        print(f"ExifTool verification failed: {error}. Obtain {lock['official_download_url']}", file=sys.stderr)
        return 1
    print(f"ExifTool {lock['version']} archive and staged runtime verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
