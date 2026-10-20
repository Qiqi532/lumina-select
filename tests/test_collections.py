"""T-09 智能集合。

验收对照（PLAN.md T-09）：
    · 保存 3 个集合，重启后仍在
    · 计数与 SELECT COUNT(*) 一致
    · 迁移副本演练：照片数与人工决策零丢失
另加两条设计约束：
    · 存**条件树**而不是照片清单（清单会过期）
    · 支持"排除低于 N 星或未评级"这类负向条件
"""
from __future__ import annotations

import sqlite3

import pytest

from engine.migrations import LATEST_SCHEMA_VERSION
from engine.store import PhotoStore
from services.collection_service import (
    CollectionError,
    CollectionService,
    count_for_condition,
    exclude_below_star_or_unrated,
    facet_conditions,
    star_at_least,
    validate_condition,
)
from services.command_catalog import build_registry

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _store(tmp_path, *, name="cull.db"):
    """造 12 张照片：星级 0–5 各若干，其中 2 张未评级（star=None）。"""
    store = PhotoStore(str(tmp_path / name), enable_wal=False)
    rows = [
        # path, star, label, camera
        ("/p/a01.CR3", 5, "P", "ILCE-7M4"),
        ("/p/a02.CR3", 4, "P", "ILCE-7M4"),
        ("/p/a03.CR3", 3, None, "ILCE-7M4"),
        ("/p/a04.CR3", 2, None, "Canon 5D4"),
        ("/p/a05.CR3", 1, None, "Canon 5D4"),
        ("/p/a06.CR3", 0, "X", "Canon 5D4"),
        ("/p/a07.CR3", None, None, "Canon 5D4"),   # 未评级
        ("/p/a08.CR3", None, None, "ILCE-7M4"),    # 未评级
        ("/p/a09.CR3", 5, None, "ILCE-7M4"),
        ("/p/a10.CR3", 4, None, "ILCE-7M4"),
        ("/p/a11.CR3", 3, None, "Canon 5D4"),
        ("/p/a12.CR3", 2, None, "ILCE-7M4"),
    ]
    for index, (path, star, label, camera) in enumerate(rows):
        store.upsert_photo(
            {
                "path": path,
                "fname": path.rsplit("/", 1)[-1],
                "asset_pair_id": f"pair-{index}",
                "asset_role": "raw",
                "star": star,
                "label": label,
                "camera_model": camera,
                "blur_score": 88.0,
                "comp_score": 50.0 + index,
            }
        )
    return store


class TestSchemaMigration:
    def test_new_database_has_collections_table(self, tmp_path):
        store = _store(tmp_path)
        try:
            assert store.conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION
            assert store.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='collections'"
            ).fetchone()
        finally:
            store.close()

    def test_upgrade_from_v5_keeps_photos_and_decisions(self, tmp_path):
        """验收：迁移副本演练 —— 照片数与人工决策零丢失。"""
        db_path = tmp_path / "old.db"
        legacy = sqlite3.connect(str(db_path))
        legacy.execute("PRAGMA user_version=5")
        legacy.execute("CREATE TABLE photos (path TEXT PRIMARY KEY, star INT, label TEXT)")
        legacy.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        legacy.execute("CREATE TABLE ui_preferences (key TEXT PRIMARY KEY, value TEXT)")
        for index in range(5):
            legacy.execute(
                "INSERT INTO photos (path, star, label) VALUES (?,?,?)",
                (f"/old/{index}.CR3", 5, "P"),
            )
        legacy.commit()
        legacy.close()

        store = PhotoStore(str(db_path), enable_wal=False)
        try:
            assert store.conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION
            assert store.conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0] == 5, \
                "升级后照片数不能变"
            assert store.conn.execute(
                "SELECT COUNT(*) FROM photos WHERE star=5 AND label='P'"
            ).fetchone()[0] == 5, "人工决策必须原样保留"
            assert store.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='collections'"
            ).fetchone(), "升级后必须有集合表"
        finally:
            store.close()

    def test_upgrade_creates_backup(self, tmp_path):
        db_path = tmp_path / "old.db"
        legacy = sqlite3.connect(str(db_path))
        legacy.execute("PRAGMA user_version=5")
        legacy.execute("CREATE TABLE photos (path TEXT PRIMARY KEY)")
        legacy.commit()
        legacy.close()
        store = PhotoStore(str(db_path), enable_wal=False)
        store.close()
        assert list(tmp_path.glob("old.db.pre-v*.bak")), "升级前必须留备份"


class TestConditionHelpers:
    def test_negative_condition_excludes_unrated(self, tmp_path):
        """PLAN 明确要求：排除低于 N 星**或未评级**。"""
        store = _store(tmp_path)
        try:
            service = CollectionService(store)
            saved = service.save("精选", exclude_below_star_or_unrated(3))
            # 12 张里：star>=3 的有 a01(5) a02(4) a03(3) a09(5) a10(4) a11(3) = 6
            assert saved["count"] == 6, f"实际 {saved['count']}"
            paths = service.paths(saved["id"])
            assert "/p/a07.CR3" not in paths, "未评级必须被排除"
            assert "/p/a08.CR3" not in paths
            assert "/p/a04.CR3" not in paths, "2 星必须被排除"
        finally:
            store.close()

    def test_star_at_least_matches_sql_count(self, tmp_path):
        """验收：计数与 SELECT COUNT(*) 一致。"""
        store = _store(tmp_path)
        try:
            service = CollectionService(store)
            saved = service.save("四星以上", star_at_least(4))
            expected = store.conn.execute(
                "SELECT COUNT(*) FROM photos WHERE star >= 4"
            ).fetchone()[0]
            assert saved["count"] == expected
            assert service.count(saved["id"]) == expected
        finally:
            store.close()

    def test_facet_conditions_merge_into_and(self):
        tree = facet_conditions(
            [{"field": "camera_model", "op": "eq", "value": "ILCE-7M4"},
             star_at_least(4)]
        )
        assert "and" in tree
        assert len(tree["and"]) == 2
        assert facet_conditions([]) is None

    def test_invalid_condition_is_rejected_before_saving(self, tmp_path):
        store = _store(tmp_path)
        try:
            service = CollectionService(store)
            with pytest.raises(CollectionError):
                service.save("坏集合", {"field": "no_such_column", "op": "eq", "value": 1})
            assert store.collections() == [], "非法条件不能落库"
        finally:
            store.close()

    def test_validate_condition_rejects_bad_field(self):
        with pytest.raises(CollectionError):
            validate_condition({"field": "photos; DROP", "op": "eq", "value": 1})

    def test_empty_name_rejected(self, tmp_path):
        store = _store(tmp_path)
        try:
            with pytest.raises(CollectionError):
                CollectionService(store).save("   ", star_at_least(3))
        finally:
            store.close()


class TestPersistence:
    def test_three_collections_survive_reopen(self, tmp_path):
        """验收：保存 3 个集合，重启后仍在。"""
        db_path = str(tmp_path / "cull.db")
        store = _store(tmp_path)
        service = CollectionService(store)
        service.save("精选", star_at_least(4))
        service.save("备胎", star_at_least(2))
        service.save("索尼机身", {"field": "camera_model", "op": "eq", "value": "ILCE-7M4"})
        store.close()

        reopened = PhotoStore(db_path, enable_wal=False)
        try:
            names = {row["name"] for row in CollectionService(reopened).list()}
            assert names == {"精选", "备胎", "索尼机身"}
        finally:
            reopened.close()

    def test_saving_same_name_overwrites(self, tmp_path):
        store = _store(tmp_path)
        try:
            service = CollectionService(store)
            first = service.save("精选", star_at_least(5))
            second = service.save("精选", star_at_least(2))
            assert first["id"] == second["id"], "同名应覆盖而不是再建一个"
            assert second["created"] is False
            assert len(store.collections()) == 1
        finally:
            store.close()

    def test_count_is_live_not_frozen(self, tmp_path):
        """集合存的是条件：新照片达标后计数要跟着涨。"""
        store = _store(tmp_path)
        try:
            service = CollectionService(store)
            saved = service.save("五星", star_at_least(5))
            assert saved["count"] == 2
            store.upsert_photo(
                {"path": "/p/new.CR3", "fname": "new.CR3", "asset_pair_id": "pair-new",
                 "asset_role": "raw", "star": 5, "blur_score": 88.0}
            )
            assert service.count(saved["id"]) == 3, "新照片必须自动纳入"
        finally:
            store.close()

    def test_count_for_condition_with_scope(self, tmp_path):
        store = _store(tmp_path)
        try:
            total = count_for_condition(store, star_at_least(3))
            scoped = count_for_condition(
                store, star_at_least(3),
                scope={"kind": "collection", "paths": ["/p/a01.CR3", "/p/a05.CR3"]},
            )
            assert total == 6
            assert scoped == 1, "作用域应把范围收窄到 a01"
        finally:
            store.close()


class TestCollectionCommands:
    def test_save_command_and_undo(self, tmp_path):
        store = _store(tmp_path)
        try:
            commands = build_registry(store)
            result = commands.execute(
                "collection.save", {"name": "精选", "condition": star_at_least(4)}
            )
            assert result.ok, result.error
            assert result.value["count"] == 4
            assert len(store.collections()) == 1
            assert commands.undo()
            assert store.collections() == [], "撤销应删掉新建的集合"
        finally:
            store.close()

    def test_undo_restores_overwritten_condition(self, tmp_path):
        store = _store(tmp_path)
        try:
            commands = build_registry(store)
            commands.execute("collection.save", {"name": "精选", "condition": star_at_least(5)})
            commands.execute("collection.save", {"name": "精选", "condition": star_at_least(1)})
            assert CollectionService(store).count(store.get_collection_by_name("精选")["id"]) == 9
            assert commands.undo()
            row = store.get_collection_by_name("精选")
            assert row is not None
            assert CollectionService(store).count(row["id"]) == 2, "撤销应回到 5 星条件"
        finally:
            store.close()

    def test_delete_command_and_undo_restores_row(self, tmp_path):
        store = _store(tmp_path)
        try:
            commands = build_registry(store)
            saved = commands.execute(
                "collection.save", {"name": "临时", "condition": star_at_least(1)}
            )
            collection_id = saved.value["id"]
            deleted = commands.execute("collection.delete", {"id": collection_id})
            assert deleted.ok
            assert store.get_collection(collection_id) is None
            assert commands.undo()
            assert store.get_collection(collection_id) is not None, "撤销应原样插回"
        finally:
            store.close()

    def test_delete_missing_collection_fails_loudly(self, tmp_path):
        store = _store(tmp_path)
        try:
            commands = build_registry(store)
            result = commands.execute("collection.delete", {"id": 999999})
            assert not result.ok, "删不存在的集合必须显式失败"
        finally:
            store.close()


class TestScopeIntegration:
    def test_collection_scope_is_usable_by_facets(self, tmp_path):
        store = _store(tmp_path)
        try:
            service = CollectionService(store)
            saved = service.save("索尼", {"field": "camera_model", "op": "eq", "value": "ILCE-7M4"})
            scope = service.scope(saved["id"])
            assert scope["kind"] == "collection"
            assert scope["name"] == "索尼"
            assert len(scope["paths"]) == 7
            assert all(path.startswith("/p/a") for path in scope["paths"])
        finally:
            store.close()

    def test_corrupt_condition_does_not_crash_listing(self, tmp_path):
        store = _store(tmp_path)
        try:
            store.create_collection("坏的", "{not json")
            rows = CollectionService(store).list()
            assert rows and rows[0]["description_text"] == "（条件损坏）"
            assert CollectionService(store).count(rows[0]["id"]) == 0
        finally:
            store.close()
