from __future__ import annotations

import sqlite3

import pytest

from engine import migrations
from engine.store import PhotoStore


def _create_minimal_v04(path):
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE photos (path TEXT PRIMARY KEY, fname TEXT, star INT, label TEXT)"
    )
    connection.execute(
        "INSERT INTO photos(path, fname, star, label) VALUES (?, ?, ?, ?)",
        ("C:/photos/IMG_0001.CR3", "IMG_0001.CR3", 4, "P"),
    )
    connection.commit()
    connection.close()


def test_v04_database_upgrades_without_losing_manual_decisions(tmp_path):
    db_path = tmp_path / "legacy.db"
    _create_minimal_v04(db_path)

    with PhotoStore(str(db_path), enable_wal=False) as store:
        row = store.get_photo("C:/photos/IMG_0001.CR3")
        assert row["star"] == 4
        assert row["label"] == "P"
        assert store.conn.execute("PRAGMA user_version").fetchone()[0] == 5
        columns = {
            item["name"] for item in store.conn.execute("PRAGMA table_info(photos)")
        }
        assert {
            "asset_pair_id",
            "asset_role",
            "camera_model",
            "analysis_backend",
            "decision_source",
        } <= columns
        tables = {
            item[0]
            for item in store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"face_regions", "export_items", "ui_preferences"} <= tables

    with PhotoStore(str(db_path), enable_wal=False) as reopened:
        assert reopened.conn.execute("PRAGMA user_version").fetchone()[0] == 5
        assert reopened.get_photo("C:/photos/IMG_0001.CR3")["star"] == 4


def test_failed_migration_rolls_back_and_keeps_backup(tmp_path, monkeypatch):
    db_path = tmp_path / "broken-upgrade.db"
    _create_minimal_v04(db_path)

    def fail(_connection):
        raise sqlite3.OperationalError("simulated migration failure")

    monkeypatch.setitem(migrations.MIGRATIONS, 5, fail)

    with pytest.raises(migrations.MigrationError, match="simulated migration failure"):
        PhotoStore(str(db_path), enable_wal=False)

    backups = list(tmp_path.glob("broken-upgrade.db.pre-v05*.bak"))
    assert len(backups) == 1
    connection = sqlite3.connect(db_path)
    assert connection.execute("SELECT star FROM photos").fetchone()[0] == 4
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
    connection.close()


def test_new_database_starts_at_latest_schema(tmp_path):
    db_path = tmp_path / "new.db"

    with PhotoStore(str(db_path), enable_wal=False) as store:
        assert store.conn.execute("PRAGMA user_version").fetchone()[0] == 5
        indexes = {
            item[0]
            for item in store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        assert {
            "idx_photos_asset_pair_id",
            "idx_photos_star",
            "idx_photos_label",
            "idx_photos_group_id",
        } <= indexes
