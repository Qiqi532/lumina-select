# -*- coding: utf-8 -*-
"""store.py 单测：迁移幂等、增量更新、断点辅助、WAL 与完整性。"""
from __future__ import annotations

from engine import store


def _photo(path, **kw):
    p = {"path": path, "fname": "x.jpg", "ts": 0.0, "mtime": 123.0, "width": 10, "height": 10}
    p.update(kw)
    return p


def test_schema_version_is_idempotent(tmp_db_path):
    with store.PhotoStore(tmp_db_path, enable_wal=False) as first:
        assert first.conn.execute("PRAGMA user_version").fetchone()[0] == 5
    with store.PhotoStore(tmp_db_path, enable_wal=False) as second:
        assert second.conn.execute("PRAGMA user_version").fetchone()[0] == 5


def test_upsert_and_incremental_update(tmp_db_path):
    s = store.PhotoStore(tmp_db_path, enable_wal=False)
    s.upsert_photo(_photo("/a/1.jpg"))
    assert s.count_photos() == 1
    # 更新同 path（mtime 变化 + 新指标）
    s.upsert_photo(_photo("/a/1.jpg", mtime=999.0, blur_score=88.0, phash="abc"))
    row = s.get_photo("/a/1.jpg")
    assert row["mtime"] == 999.0
    assert row["blur_score"] == 88.0
    assert s.count_photos() == 1
    s.close()


def test_upsert_photos_batch(tmp_db_path):
    s = store.PhotoStore(tmp_db_path, enable_wal=False)
    rows = [_photo(f"/a/{i}.jpg") for i in range(50)]
    s.upsert_photos_batch(rows)
    assert s.count_photos() == 50
    # 第二次批量更新（不同字段集也应兼容）
    rows2 = [_photo(f"/a/{i}.jpg", blur_score=float(i), phash="x") for i in range(50)]
    s.upsert_photos_batch(rows2)
    assert s.count_photos() == 50
    assert s.get_photo("/a/0.jpg")["blur_score"] == 0.0
    s.close()


def test_photos_missing_for_breakpoint(tmp_db_path):
    s = store.PhotoStore(tmp_db_path, enable_wal=False)
    s.upsert_photo(_photo("/a/1.jpg", blur_score=1.0, phash="p"))
    s.upsert_photo(_photo("/a/2.jpg", blur_score=2.0))      # 缺 phash
    s.upsert_photo(_photo("/a/3.jpg"))                      # 全缺
    missing = s.photos_missing("aesthetic")
    assert missing == {"/a/1.jpg", "/a/2.jpg", "/a/3.jpg"}
    missing_phash = s.photos_missing("phash")
    assert missing_phash == {"/a/2.jpg", "/a/3.jpg"}
    s.close()


def test_wal_mode_and_integrity(tmp_db_path):
    s = store.PhotoStore(tmp_db_path, enable_wal=True)
    mode = s.conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    assert s.integrity_check() is True
    s.close()


def test_scene_manual_roundtrip(tmp_db_path):
    s = store.PhotoStore(tmp_db_path, enable_wal=False)
    s.upsert_photo(_photo("/a/1.jpg"))
    s.set_scene_manual("/a/1.jpg", "人像")
    assert s.scene_overrides() == {"/a/1.jpg": "人像"}
    s.set_scene_manual("/a/1.jpg", None)
    assert s.scene_overrides() == {}
    s.close()


def test_context_manager(tmp_db_path):
    with store.PhotoStore(tmp_db_path) as s:
        s.upsert_photo(_photo("/a/1.jpg"))
    # with 退出后连接已关闭
    import sqlite3
    conn = sqlite3.connect(tmp_db_path)
    assert conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0] == 1
    conn.close()


def test_repository_v05_roundtrips_and_asset_decision(tmp_db_path):
    with store.PhotoStore(tmp_db_path, enable_wal=False) as photo_store:
        photo_store.upsert_photos_batch(
            [
                _photo(
                    "/a/IMG_0001.CR3",
                    asset_pair_id="pair-1",
                    asset_role="raw",
                    star=1,
                ),
                _photo(
                    "/a/IMG_0001.JPG",
                    asset_pair_id="pair-1",
                    asset_role="jpeg",
                    star=1,
                ),
            ]
        )

        before = photo_store.apply_decision(
            ["/a/IMG_0001.CR3"], star=5, label="P", source="manual"
        )

        assert {item["path"] for item in before} == {
            "/a/IMG_0001.CR3",
            "/a/IMG_0001.JPG",
        }
        for path in ("/a/IMG_0001.CR3", "/a/IMG_0001.JPG"):
            row = photo_store.get_photo(path)
            assert (row["star"], row["label"], row["decision_source"]) == (
                5,
                "P",
                "manual",
            )
            assert row["decision_updated_at"] is not None

        photo_store.replace_face_regions(
            "/a/IMG_0001.CR3",
            [
                {
                    "face_index": 0,
                    "x": 0.1,
                    "y": 0.2,
                    "width": 0.3,
                    "height": 0.4,
                    "ear": 0.25,
                    "eye_close_prob": None,
                    "sharpness": 88.0,
                }
            ],
        )
        assert photo_store.face_regions("/a/IMG_0001.CR3")[0]["sharpness"] == 88.0

        photo_store.set_preference("review.view_mode", "compare")
        assert photo_store.get_preference("review.view_mode") == "compare"

        photo_store.record_export_item(
            "/a/IMG_0001.CR3",
            "D:/delivery/IMG_0001.CR3",
            "D:/delivery/IMG_0001.xmp",
            "success",
            None,
        )
        export_row = photo_store.conn.execute(
            "SELECT * FROM export_items WHERE path=?",
            ("/a/IMG_0001.CR3",),
        ).fetchone()
        assert export_row["status"] == "success"
