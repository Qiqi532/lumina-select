from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from engine.catalog_registry import (
    CatalogRegistry,
    default_registry_path,
    normalize_project_path,
    project_display_name,
    read_photo_count,
)


def _make_project_db(path: Path, *, photos: int = 0) -> Path:
    """造一个带 photos 表的项目库（只建最小结构，够 read_photo_count 用）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE photos (path TEXT PRIMARY KEY)")
    for index in range(photos):
        connection.execute("INSERT INTO photos VALUES (?)", (f"C:/p/IMG_{index:04d}.CR3",))
    connection.commit()
    connection.close()
    return path


# ---------------------------------------------------------------------------
# 路径与命名
# ---------------------------------------------------------------------------
def test_default_registry_path_uses_local_app_data(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    path = default_registry_path()

    assert path == tmp_path / "LuminaSelect" / "catalogs.db"


def test_default_registry_path_falls_back_to_home(monkeypatch):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    path = default_registry_path()

    assert path.name == "catalogs.db"
    assert ".lumina_select" in str(path)


def test_normalize_is_absolute_and_dedupes(tmp_path):
    a = normalize_project_path(tmp_path / "p" / ".." / "p" / "cull.db")
    b = normalize_project_path(str(tmp_path / "p") + os.sep + "cull.db")

    assert os.path.isabs(a)
    assert a == b, "同一路径的不同写法必须归一，否则入口页会出现重复卡片"


def test_display_name_prefers_project_folder_over_container(tmp_path):
    assert project_display_name(tmp_path / "2026-婚礼" / "data" / "cull.db") == "2026-婚礼"
    assert project_display_name(tmp_path / "2026-婚礼" / "cull.db") == "2026-婚礼"
    assert project_display_name(tmp_path / "毕业季" / "db" / "cull.db") == "毕业季"


# ---------------------------------------------------------------------------
# 登记 / 去重 / 排序
# ---------------------------------------------------------------------------
def test_register_dedupes_same_project_path(tmp_path):
    reg = CatalogRegistry(tmp_path / "catalogs.db")
    project = _make_project_db(tmp_path / "婚礼" / "data" / "cull.db", photos=3)

    reg.register(project)
    reg.register(project)          # 重复登记

    assert reg.count() == 1, "同路径重复打开不应产生多张卡片"
    assert reg.get(project).photo_count == 0  # 未传计数则保留原值


def test_register_records_name_count_and_recent_order(tmp_path):
    reg = CatalogRegistry(tmp_path / "catalogs.db")
    first = _make_project_db(tmp_path / "婚礼" / "data" / "cull.db", photos=2)
    second = _make_project_db(tmp_path / "毕业季" / "data" / "cull.db", photos=5)

    reg.register(first, photo_count=read_photo_count(first))
    # 保证时间可分辨（文件系统时间戳精度足够，但显式间隔更稳）
    import time

    time.sleep(0.02)
    reg.register(second, photo_count=read_photo_count(second))

    recent = [entry.name for entry in reg.entries()]
    assert recent == ["毕业季", "婚礼"], "默认按最近打开倒序"
    assert reg.get(second).photo_count == 5


def test_entries_can_sort_by_name(tmp_path):
    reg = CatalogRegistry(tmp_path / "catalogs.db")
    reg.register(_make_project_db(tmp_path / "b" / "data" / "cull.db"))
    reg.register(_make_project_db(tmp_path / "a" / "data" / "cull.db"))

    assert [entry.name for entry in reg.entries(order="name")] == ["a", "b"]


def test_rename_and_update_photo_count(tmp_path):
    reg = CatalogRegistry(tmp_path / "catalogs.db")
    project = _make_project_db(tmp_path / "x" / "data" / "cull.db")

    reg.register(project)
    reg.rename(project, "自定义名称")
    reg.update_photo_count(project, 42)

    entry = reg.get(project)
    assert entry.name == "自定义名称"
    assert entry.photo_count == 42


def test_touch_only_updates_opened_at(tmp_path):
    reg = CatalogRegistry(tmp_path / "catalogs.db")
    project = _make_project_db(tmp_path / "x" / "data" / "cull.db")
    reg.register(project, name="保留名", photo_count=7)
    before = reg.get(project)

    import time

    time.sleep(0.02)
    reg.touch(project)
    after = reg.get(project)

    assert after.opened_at > before.opened_at
    assert after.name == "保留名", "touch 不得改名"
    assert after.photo_count == 7, "touch 不得改计数"


# ---------------------------------------------------------------------------
# forget：只忘索引，绝不删库
# ---------------------------------------------------------------------------
def test_forget_removes_entry_but_keeps_library_file(tmp_path):
    reg = CatalogRegistry(tmp_path / "catalogs.db")
    project = _make_project_db(tmp_path / "x" / "data" / "cull.db", photos=1)
    reg.register(project)

    assert reg.forget(project) is True
    assert reg.count() == 0
    assert project.is_file(), "从列表移除绝不能删除用户的项目库"
    assert reg.forget(project) is False, "重复移除返回 False"


# ---------------------------------------------------------------------------
# 只读计数：绝不创建库、绝不迁移
# ---------------------------------------------------------------------------
def test_read_photo_count_returns_none_for_missing_db(tmp_path):
    missing = tmp_path / "nope" / "data" / "cull.db"

    assert read_photo_count(missing) is None
    assert not missing.exists(), "只读查询不得凭空创建库文件"


def test_read_photo_count_does_not_create_missing_db(tmp_path):
    missing = tmp_path / "ghost" / "data" / "cull.db"

    read_photo_count(missing)

    assert not missing.parent.exists()


def test_read_photo_count_returns_none_for_foreign_schema(tmp_path):
    """存在但不是本项目库（无 photos 表）→ 返回 None，不猜数字。"""
    other = tmp_path / "other.db"
    connection = sqlite3.connect(other)
    connection.execute("CREATE TABLE unrelated (id INTEGER)")
    connection.commit()
    connection.close()

    assert read_photo_count(other) is None


def test_read_photo_count_works_and_does_not_mutate(tmp_path):
    project = _make_project_db(tmp_path / "x" / "data" / "cull.db", photos=4)
    before = project.read_bytes()

    assert read_photo_count(project) == 4
    assert project.read_bytes() == before, "只读查询不得改动库内容"


def test_registry_is_not_a_project_database(tmp_path):
    """注册表只存索引表，绝不含 photos —— 与项目库彻底分离。"""
    reg = CatalogRegistry(tmp_path / "catalogs.db")
    tables = {
        row[0]
        for row in reg.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }

    assert "catalogs" in tables
    assert "photos" not in tables


# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------
def test_registry_context_manager_and_persistence(tmp_path):
    path = tmp_path / "catalogs.db"
    project = _make_project_db(tmp_path / "x" / "data" / "cull.db")
    with CatalogRegistry(path) as reg:
        reg.register(project, name="持久化测试")

    with CatalogRegistry(path) as reg:
        assert reg.get(project).name == "持久化测试"


def test_entry_exists_flag_reflects_disk(tmp_path):
    reg = CatalogRegistry(tmp_path / "catalogs.db")
    project = _make_project_db(tmp_path / "x" / "data" / "cull.db")
    reg.register(project)

    assert reg.get(project).exists is True
    project.unlink()
    assert reg.get(project).exists is False, "库被移走后卡片应能标出失效"
