# -*- coding: utf-8 -*-
"""catalog_registry.py —— 全局项目注册表（跨项目索引）

【T-05 / M0-5 目标】把"一次向导"变成"可持续的库"，解决跨天工作流：
用户关掉 App 再打开，应当能从入口页看到自己做过哪些项目、各自多少张照片、
最近什么时候打开的，并且一键回到某个项目 —— 而不是每次都要重新选目录。

设计要点：
    * 全局库位置：``%LOCALAPPDATA%\\LuminaSelect\\catalogs.db``（可用参数覆盖，便于测试）。
      它**只存"有哪些项目"这一张索引表**，与各项目自己的 ``cull.db`` 完全分离：
      项目库损坏/被删不会连累注册表，注册表损坏也只需重建索引、不丢任何照片决策。
    * 只读式登记：本模块**不打开、不修改任何项目的 cull.db**（照片数由调用方传入或
      通过一个轻量只读查询获取），避免在入口页就把每个项目库锁住。
    * 路径去重：以规范化后的绝对路径为主键，重复打开同一项目只更新"最近打开"。

红线遵守：
    * 本文件**不含任何 UI 代码**，SQL 只出现在这里（N6 允许 `engine/` 持有 SQL）。
    * 不触碰 ``METRIC_FIELDS`` / ``_ai_model_signature()``（A6）。
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

from .log import get_logger

_log = get_logger("engine.catalog_registry")

# 全局注册表文件名（放在 %LOCALAPPDATA%\LuminaSelect\ 下）
REGISTRY_DIRNAME = "LuminaSelect"
REGISTRY_FILENAME = "catalogs.db"

_REGISTRY_SCHEMA = """
CREATE TABLE IF NOT EXISTS catalogs (
    path          TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    photo_count   INTEGER NOT NULL DEFAULT 0,
    opened_at     REAL    NOT NULL DEFAULT 0,
    created_at    REAL    NOT NULL DEFAULT 0,
    last_seen_dir TEXT
);
CREATE INDEX IF NOT EXISTS idx_catalogs_opened ON catalogs(opened_at DESC);
"""


def default_registry_path() -> Path:
    """``%LOCALAPPDATA%\\LuminaSelect\\catalogs.db``。

    非 Windows（或无 LOCALAPPDATA）时回落到用户主目录下的 ``.lumina_select``，
    保证单测/跨平台脚本也能拿到一个可写位置。
    """
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / REGISTRY_DIRNAME / REGISTRY_FILENAME
    return Path.home() / ".lumina_select" / REGISTRY_FILENAME


def normalize_project_path(path: str | os.PathLike[str]) -> str:
    """项目库路径 → 规范化绝对路径（用于去重主键）。

    只做 ``abspath`` + ``normpath``，**不 resolve 符号链接**：Windows 上项目常在
    网盘/映射盘，resolve 会把它变成 UNC 或另一台机器的路径，反而制造重复条目。
    """
    return os.path.normpath(os.path.abspath(os.fspath(path)))


def project_display_name(db_path: str | os.PathLike[str]) -> str:
    """默认项目名：库所在目录名；取不到时退回文件名。

    用户项目通常长这样：``D:\\Photos\\2026-婚礼\\data\\cull.db`` —— 目录名
    ``2026-婚礼`` 远比分不清的 ``cull.db`` 好认。因此若库被放在 ``data/``
    之类的容器目录里，取**上一层**的名字；否则退回库文件名。
    """
    folder = Path(db_path).expanduser()
    parent = folder.parent
    if parent.name:
        # 项目库常被放在 data/ 之类的中性目录下，此时目录名没有辨识度，
        # 真正有意义的是再上一层（项目名）。
        if parent.name.lower() in {"data", "db", "database", "catalog", "catalogs"}:
            grand = parent.parent
            if grand.name:
                return grand.name
        return parent.name
    return folder.stem or folder.name


class CatalogEntry:
    """注册表里的一条项目记录（纯数据，不持有连接）。"""

    __slots__ = ("path", "name", "photo_count", "opened_at", "created_at", "last_seen_dir")

    def __init__(self, *, path: str, name: str, photo_count: int,
                 opened_at: float, created_at: float, last_seen_dir: str | None) -> None:
        self.path = path
        self.name = name
        self.photo_count = int(photo_count)
        self.opened_at = float(opened_at)
        self.created_at = float(created_at)
        self.last_seen_dir = last_seen_dir

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "name": self.name,
            "photo_count": self.photo_count,
            "opened_at": self.opened_at,
            "created_at": self.created_at,
            "last_seen_dir": self.last_seen_dir,
        }

    @property
    def exists(self) -> bool:
        """库文件当前是否仍在磁盘上（供入口页标灰失效项目）。"""
        return os.path.isfile(self.path)

    def __repr__(self) -> str:  # pragma: no cover - 调试友好
        return f"<CatalogEntry {self.name!r} photos={self.photo_count} path={self.path!r}>"


class CatalogRegistry:
    """全局项目注册表。线程内使用；跨线程请自行串行化。"""

    def __init__(self, db_path: str | os.PathLike[str] | None = None) -> None:
        self.db_path = str(db_path) if db_path is not None else str(default_registry_path())
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=10000")
        try:
            self.conn.executescript(_REGISTRY_SCHEMA)
        except Exception:
            self.conn.close()
            raise

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def register(
        self,
        project_db: str | os.PathLike[str],
        *,
        name: str | None = None,
        photo_count: int | None = None,
        touch: bool = True,
    ) -> CatalogEntry:
        """登记/更新一个项目。

        ``name`` 为空时用目录名推断；``photo_count`` 为空时保留原值（新记录为 0）。
        ``touch=True`` 时刷新"最近打开"时间。同路径重复登记**不会**产生新行。
        """
        path = normalize_project_path(project_db)
        now = time.time()
        row = self.conn.execute(
            "SELECT * FROM catalogs WHERE path=?", (path,)
        ).fetchone()
        resolved_name = name or (row["name"] if row else None) or project_display_name(path)
        opened_at = now if touch else (row["opened_at"] if row else now)
        created_at = row["created_at"] if row else now
        count = int(photo_count) if photo_count is not None else (row["photo_count"] if row else 0)
        self.conn.execute(
            "INSERT INTO catalogs (path, name, photo_count, opened_at, created_at, last_seen_dir) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET "
            "  name=excluded.name, photo_count=excluded.photo_count, "
            "  opened_at=excluded.opened_at",
            (path, resolved_name, count, opened_at, created_at, os.path.dirname(path)),
        )
        self.conn.commit()
        return CatalogEntry(
            path=path, name=resolved_name, photo_count=count,
            opened_at=opened_at, created_at=created_at,
            last_seen_dir=os.path.dirname(path),
        )

    def touch(self, project_db: str | os.PathLike[str]) -> None:
        """只刷新"最近打开"，不改名、不改计数（入口页点开项目时用）。"""
        path = normalize_project_path(project_db)
        self.conn.execute(
            "UPDATE catalogs SET opened_at=? WHERE path=?", (time.time(), path)
        )
        self.conn.commit()

    def rename(self, project_db: str | os.PathLike[str], name: str) -> None:
        path = normalize_project_path(project_db)
        self.conn.execute("UPDATE catalogs SET name=? WHERE path=?", (name, path))
        self.conn.commit()

    def update_photo_count(self, project_db: str | os.PathLike[str], photo_count: int) -> None:
        path = normalize_project_path(project_db)
        self.conn.execute(
            "UPDATE catalogs SET photo_count=? WHERE path=?", (int(photo_count), path)
        )
        self.conn.commit()

    def forget(self, project_db: str | os.PathLike[str]) -> bool:
        """从注册表移除一条记录（**不删除库文件本身**）。

        返回是否确实删掉了记录。入口页"从列表移除"用的是它 —— 必须是
        "只忘掉索引"，绝不能顺手删用户的照片库。
        """
        path = normalize_project_path(project_db)
        cursor = self.conn.execute("DELETE FROM catalogs WHERE path=?", (path,))
        self.conn.commit()
        return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------
    def get(self, project_db: str | os.PathLike[str]) -> CatalogEntry | None:
        path = normalize_project_path(project_db)
        row = self.conn.execute("SELECT * FROM catalogs WHERE path=?", (path,)).fetchone()
        return self._row_to_entry(row) if row else None

    def entries(self, *, order: str = "recent") -> list[CatalogEntry]:
        """列出全部项目。

        ``order="recent"``（默认）按最近打开倒序；``"name"`` 按名称排序。
        名称排序用 ``COLLATE NOCASE``，中文按码位排（够用且稳定）。
        """
        if order == "name":
            sql = "SELECT * FROM catalogs ORDER BY name COLLATE NOCASE ASC"
        else:
            sql = "SELECT * FROM catalogs ORDER BY opened_at DESC, name COLLATE NOCASE ASC"
        return [self._row_to_entry(row) for row in self.conn.execute(sql)]

    def count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) FROM catalogs").fetchone()
        return int(row[0]) if row else 0

    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> CatalogEntry:
        return CatalogEntry(
            path=row["path"], name=row["name"], photo_count=row["photo_count"],
            opened_at=row["opened_at"], created_at=row["created_at"],
            last_seen_dir=row["last_seen_dir"],
        )

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:  # pragma: no cover - 关闭失败不该抛
            pass

    def __enter__(self) -> "CatalogRegistry":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def read_photo_count(project_db: str | os.PathLike[str]) -> int | None:
    """以**只读**方式取某项目库的照片数；库不存在/读失败返回 None。

    用 ``mode=ro`` 打开：绝不因为入口页刷个数字就创建空库、或触发迁移。
    查不到就让调用方保留注册表里的旧值，而不是把 0 写回去。
    """
    path = normalize_project_path(project_db)
    if not os.path.isfile(path):
        return None
    try:
        uri = Path(path).as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=5)
    except (sqlite3.Error, ValueError):
        return None
    try:
        row = connection.execute("SELECT COUNT(*) FROM photos").fetchone()
        return int(row[0]) if row else None
    except sqlite3.Error:
        # 库存在但结构不对（旧库/半截文件）——不猜，交给调用方保留旧值。
        return None
    finally:
        connection.close()
