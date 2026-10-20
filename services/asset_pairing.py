"""Stable RAW+JPEG asset pairing without database or UI dependencies."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Callable, Iterable, Literal

from engine.config import RAW_EXTS


JPEG_EXTS = {".jpg", ".jpeg"}
PAIR_WINDOW_MS = 2_000.0


def _canonical(path: Path) -> str:
    return str(path.resolve(strict=False)).replace("\\", "/").casefold()


def _timestamp(value) -> float | None:
    if isinstance(value, dict):
        value = value.get("ts")
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class AssetPair:
    asset_id: str
    directory: Path
    stem: str
    raw_path: Path | None
    jpeg_path: Path | None
    preview_path: Path

    def members_for_mode(
        self, mode: Literal["raw", "raw+jpeg", "jpeg"]
    ) -> tuple[tuple[Path, ...], str | None]:
        if mode == "raw":
            return (
                ((self.raw_path,), None)
                if self.raw_path is not None
                else ((), "raw_not_available")
            )
        if mode == "jpeg":
            return (
                ((self.jpeg_path,), None)
                if self.jpeg_path is not None
                else ((), "jpeg_not_available")
            )
        if mode == "raw+jpeg":
            members = tuple(
                path for path in (self.raw_path, self.jpeg_path) if path is not None
            )
            return members, None if members else "asset_has_no_members"
        raise ValueError(f"unsupported asset mode: {mode}")


def _asset(directory: Path, stem: str, raw: Path | None, jpeg: Path | None) -> AssetPair:
    members = sorted(_canonical(path) for path in (raw, jpeg) if path is not None)
    identity = "\0".join((_canonical(directory), stem.casefold(), *members))
    asset_id = hashlib.sha256(identity.encode("utf-8", "surrogatepass")).hexdigest()[:24]
    preview = jpeg or raw
    if preview is None:  # pragma: no cover - constructor is private and always has a member
        raise ValueError("asset requires at least one member")
    return AssetPair(asset_id, directory, stem, raw, jpeg, preview)


def pair_assets(
    paths: Iterable[str | Path],
    timestamp_reader: Callable[[Path], float | dict | None],
    *,
    window_ms: float = PAIR_WINDOW_MS,
) -> list[AssetPair]:
    """Pair same-directory, same-stem RAW/JPEG captures within ``window_ms``."""
    grouped: dict[tuple[str, str], list[Path]] = {}
    for raw_path in paths:
        path = Path(raw_path)
        key = (_canonical(path.parent), path.stem.casefold())
        grouped.setdefault(key, []).append(path)

    assets: list[AssetPair] = []
    raw_exts = {extension.casefold() for extension in RAW_EXTS}
    for key in sorted(grouped):
        members = sorted(grouped[key], key=_canonical)
        raws = [path for path in members if path.suffix.casefold() in raw_exts]
        jpegs = [path for path in members if path.suffix.casefold() in JPEG_EXTS]
        others = [path for path in members if path not in raws and path not in jpegs]
        timestamps = {path: _timestamp(timestamp_reader(path)) for path in members}
        unused_jpegs = set(jpegs)

        for raw in raws:
            raw_time = timestamps[raw]
            eligible = [
                jpeg
                for jpeg in unused_jpegs
                if raw_time is not None
                and timestamps[jpeg] is not None
                and abs(raw_time - timestamps[jpeg]) <= window_ms
            ]
            jpeg = min(
                eligible,
                key=lambda item: (abs(raw_time - timestamps[item]), _canonical(item)),
                default=None,
            )
            if jpeg is not None:
                unused_jpegs.remove(jpeg)
            assets.append(_asset(raw.parent, raw.stem, raw, jpeg))

        for jpeg in sorted(unused_jpegs, key=_canonical):
            assets.append(_asset(jpeg.parent, jpeg.stem, None, jpeg))
        for path in others:
            assets.append(_asset(path.parent, path.stem, None, path))

    return sorted(assets, key=lambda item: (item.asset_id, _canonical(item.preview_path)))


def assets_from_rows(rows: Iterable[dict]) -> list[AssetPair]:
    """Rehydrate saved asset identities without scanning files or decoding EXIF."""
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row.get("asset_pair_id") or row["path"], []).append(row)
    assets: list[AssetPair] = []
    raw_exts = {extension.casefold() for extension in RAW_EXTS}
    for asset_id, members in grouped.items():
        raw: Path | None = None
        jpeg: Path | None = None
        for row in members:
            path = Path(row["path"])
            role = row.get("asset_role")
            if role == "jpeg" or path.suffix.casefold() in JPEG_EXTS:
                jpeg = path
            elif role == "raw" or path.suffix.casefold() in raw_exts:
                raw = path
            elif raw is None:
                raw = path
        preview = jpeg or raw
        if preview is None:
            continue
        assets.append(
            AssetPair(asset_id, preview.parent, preview.stem, raw, jpeg, preview)
        )
    return sorted(assets, key=lambda item: item.asset_id)
