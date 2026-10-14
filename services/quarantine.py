"""隔离式清理：把文件搬进项目内 ``.trash_<日期>/``，可原样搬回。

【为什么不是删除】
N3 红线要求一律软删除 / 隔离，**禁用 ``rm`` 语义**。选片是一个"我可能改主意"的
过程：今天把 300 张判为废片，明天可能发现其中 5 张是客户要的。真删是不可逆的，
而"移进隔离目录 + 留一份清单"让撤销和人工找回都成立。

本模块**没有任何删除调用**（无 ``os.remove`` / ``unlink`` / ``rmtree``），
只有 ``shutil.move`` 与反向 ``shutil.move``。

【为什么同时写 ``manifest.json``】
撤销栈在内存里，进程一关就没了。如果用户在别处误删了应用数据，隔离目录里的
清单仍能让人（或用文本编辑器）把文件搬回去。清单是**给人看的逃生舱**，
不是给程序用的唯一真相 —— 程序的真相是返回的 ``QuarantineResult``。
"""
from __future__ import annotations

import json
import shutil
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePath

TRASH_PREFIX = ".trash_"


class QuarantineError(RuntimeError):
    """隔离/还原过程中的可预期失败。"""


@dataclass(frozen=True, slots=True)
class QuarantineEntry:
    """一次成功的搬移：原路径 ↔ 隔离后路径。"""

    source: str
    target: str


@dataclass(frozen=True, slots=True)
class QuarantineResult:
    """一次隔离操作的完整结果。

    ``moved`` / ``missing`` / ``failed`` 三者互斥且并集等于输入，便于 UI 如实汇报
    —— 绝不能"搬了 297 张却报 300 张成功"。
    """

    root: str
    moved: tuple[QuarantineEntry, ...] = ()
    missing: tuple[str, ...] = ()
    failed: tuple[tuple[str, str], ...] = ()
    manifest_path: str | None = None

    @property
    def moved_count(self) -> int:
        return len(self.moved)

    @property
    def ok(self) -> bool:
        return not self.failed

    def sources(self) -> tuple[str, ...]:
        return tuple(entry.source for entry in self.moved)

    def to_dict(self) -> dict:
        return {
            "root": self.root,
            "manifest_path": self.manifest_path,
            "moved": [[entry.source, entry.target] for entry in self.moved],
            "missing": list(self.missing),
            "failed": [[path, reason] for path, reason in self.failed],
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "QuarantineResult":
        return cls(
            root=str(payload.get("root") or ""),
            moved=tuple(
                QuarantineEntry(str(pair[0]), str(pair[1]))
                for pair in payload.get("moved") or ()
            ),
            missing=tuple(str(path) for path in payload.get("missing") or ()),
            failed=tuple(
                (str(pair[0]), str(pair[1])) for pair in payload.get("failed") or ()
            ),
            manifest_path=payload.get("manifest_path"),
        )


@dataclass(frozen=True, slots=True)
class RestoreResult:
    restored: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    failed: tuple[tuple[str, str], ...] = ()

    @property
    def restored_count(self) -> int:
        return len(self.restored)

    @property
    def ok(self) -> bool:
        return not self.failed


def quarantine_root(project_dir: str | Path, *, stamp: str | None = None) -> Path:
    """返回隔离根目录 ``<project_dir>/.trash_<stamp>``。

    ``stamp`` 默认取当天日期（``YYYYMMDD``）。同一天多次清理会落进同一个目录，
    这与"按天归档"的直觉一致，也避免目录爆炸。
    """
    if stamp is None:
        stamp = time.strftime("%Y%m%d")
    return Path(project_dir) / f"{TRASH_PREFIX}{stamp}"


def iter_quarantine_dirs(project_dir: str | Path) -> tuple[Path, ...]:
    """列出项目下所有隔离目录（按名称排序，旧的在前）。"""
    base = Path(project_dir)
    if not base.is_dir():
        return ()
    return tuple(
        sorted(
            (child for child in base.iterdir()
             if child.is_dir() and child.name.startswith(TRASH_PREFIX)),
            key=lambda path: path.name,
        )
    )


def _unique_target(root: Path, relative: PurePath) -> Path:
    """在 ``root`` 下为 ``relative`` 找一个不冲突的落点（冲突则加 ``~N`` 后缀）。"""
    target = root / relative
    if not target.exists():
        return target
    stem = relative.stem
    suffix = relative.suffix
    for index in range(1, 10_000):
        candidate = root / relative.with_name(f"{stem}~{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise QuarantineError(f"隔离目录内同名文件过多：{relative}")


def _relative_under(base: Path, path: Path) -> PurePath:
    """尽量保留项目内的相对结构；项目外的文件退化为文件名。

    隔离目录内部的布局**不影响还原** —— 还原靠的是 ``(source, target)`` 成对记录。
    这里保留结构只是为了让用户自己翻隔离目录时还能认出照片在哪。
    """
    try:
        return path.resolve().relative_to(base.resolve())
    except (ValueError, OSError):
        return PurePath(path.name)


def quarantine_files(
    paths: Iterable[str],
    project_dir: str | Path,
    *,
    stamp: str | None = None,
    reason: str = "",
) -> QuarantineResult:
    """把 ``paths`` 搬进隔离目录。

    - 源文件不存在 → 记入 ``missing``（不算失败，照片可能已被用户自己移走）；
    - 搬移抛异常 → 记入 ``failed`` 并**保留原文件不动**；
    - 全程无删除调用（N3）。
    """
    base = Path(project_dir)
    root = quarantine_root(base, stamp=stamp)
    moved: list[QuarantineEntry] = []
    missing: list[str] = []
    failed: list[tuple[str, str]] = []

    root.mkdir(parents=True, exist_ok=True)

    for raw in paths:
        source = Path(raw)
        if not source.exists():
            missing.append(str(source))
            continue
        try:
            target = _unique_target(root, _relative_under(base, source))
            if target.parent != root:
                target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(target))
            moved.append(QuarantineEntry(str(source), str(target)))
        except (OSError, shutil.Error, QuarantineError) as exc:
            failed.append((str(source), str(exc)))

    manifest_path: str | None = None
    if moved:
        manifest_path = str(_write_manifest(root, moved, reason=reason))

    return QuarantineResult(
        root=str(root),
        moved=tuple(moved),
        missing=tuple(missing),
        failed=tuple(failed),
        manifest_path=manifest_path,
    )


def _write_manifest(root: Path, moved: Sequence[QuarantineEntry], *, reason: str) -> Path:
    """把本次搬移写成 ``manifest.json`` —— 给用户手工找回用的逃生舱。"""
    manifest = root / "manifest.json"
    payload: dict = {
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "reason": reason,
        "moved": [[entry.source, entry.target] for entry in moved],
    }
    existing: list = []
    if manifest.exists():
        try:
            loaded = json.loads(manifest.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("moved"), list):
                existing = loaded["moved"]
            elif isinstance(loaded, list):
                existing = loaded
        except (OSError, ValueError):
            existing = []
    payload["moved"] = existing + payload["moved"]
    manifest.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def restore_quarantine(
    entries: Iterable[QuarantineEntry] | QuarantineResult,
) -> RestoreResult:
    """把隔离的文件搬回原路径（撤销语义）。

    只搬那些**目标位置当前为空**的；若原位置已被同名文件占用则记为失败并
    **不动隔离副本** —— 宁可留着让用户手工处理，也不能覆盖用户的新文件。
    """
    if isinstance(entries, QuarantineResult):
        entries = entries.moved
    restored: list[str] = []
    missing: list[str] = []
    failed: list[tuple[str, str]] = []

    for entry in entries:
        source = Path(entry.target)
        destination = Path(entry.source)
        if not source.exists():
            missing.append(entry.source)
            continue
        if destination.exists():
            failed.append((entry.source, "原位置已存在同名文件，未覆盖"))
            continue
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(destination))
            restored.append(entry.source)
        except (OSError, shutil.Error) as exc:
            failed.append((entry.source, str(exc)))

    return RestoreResult(
        restored=tuple(restored), missing=tuple(missing), failed=tuple(failed)
    )


__all__ = [
    "QuarantineEntry",
    "QuarantineError",
    "QuarantineResult",
    "RestoreResult",
    "iter_quarantine_dirs",
    "quarantine_files",
    "quarantine_root",
    "restore_quarantine",
]
