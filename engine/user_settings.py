"""全局用户设置：跨项目、跨重启的偏好（快捷键绑定等）。

为什么单独一个库
----------------
项目库（``cull.db``）是**按项目**的，而"我把 P 绑到空格"这种偏好属于**人**，
不属于某个项目。放进项目库意味着每开一个新项目就要重设一遍 —— 这正是 T-11
要消灭的"每次重开重设"的隐形流失。

与 ``catalog_registry.py``（全局项目清单）分开放，是因为两者职责不同：
那个管"有哪些项目"，这个管"用户怎么用它"。混在一张表里会让以后加设置项时
不得不去动项目注册的代码。
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

SETTINGS_DIRNAME = "LuminaSelect"
SETTINGS_FILENAME = "settings.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at REAL NOT NULL DEFAULT 0
);
"""


def default_settings_path() -> Path:
    """``%LOCALAPPDATA%\\LuminaSelect\\settings.db``。

    与项目注册表同目录；非 Windows 或无 LOCALAPPDATA 时回落到用户主目录，
    保证单测也能拿到可写位置。
    """
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / SETTINGS_DIRNAME / SETTINGS_FILENAME
    return Path.home() / ".lumina_select" / SETTINGS_FILENAME


class UserSettings:
    """极简键值设置库。值一律存字符串（JSON 序列化由调用方负责）。"""

    def __init__(self, db_path: str | os.PathLike[str] | None = None) -> None:
        self.db_path = str(db_path) if db_path is not None else str(default_settings_path())
        directory = os.path.dirname(os.path.abspath(self.db_path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=10000")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def get(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM settings WHERE key=?", (key,)
        ).fetchone()
        return row["value"] if row else default

    def set(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at",
            (key, str(value), time.time()),
        )
        self.conn.commit()

    def delete(self, key: str) -> bool:
        cursor = self.conn.execute("DELETE FROM settings WHERE key=?", (key,))
        self.conn.commit()
        return cursor.rowcount > 0

    def all(self) -> dict[str, str]:
        return {
            row["key"]: row["value"]
            for row in self.conn.execute("SELECT key, value FROM settings ORDER BY key")
        }

    def clear(self) -> None:
        self.conn.execute("DELETE FROM settings")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "UserSettings":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


__all__ = ["UserSettings", "default_settings_path"]
