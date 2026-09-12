"""Versioned, recoverable SQLite schema migrations for Lumina Select."""
from __future__ import annotations

from pathlib import Path
import sqlite3
from typing import Callable


LATEST_SCHEMA_VERSION = 5


class MigrationError(RuntimeError):
    """Raised when a database cannot be upgraded without risking user data."""


PHOTO_COLUMNS: tuple[tuple[str, str], ...] = (
    ("path", "TEXT PRIMARY KEY"),
    ("fname", "TEXT"),
    ("ts", "REAL"),
    ("mtime", "REAL"),
    ("width", "INT"),
    ("height", "INT"),
    ("scene", "TEXT"),
    ("scene_conf", "REAL"),
    ("blur_score", "REAL"),
    ("over_ratio", "REAL"),
    ("under_ratio", "REAL"),
    ("aesthetic", "REAL"),
    ("eye_open", "REAL"),
    ("is_face", "INT"),
    ("phash", "TEXT"),
    ("group_id", "INT"),
    ("comp_score", "REAL"),
    ("is_waste", "INT"),
    ("is_best", "INT"),
    ("is_uncertain", "INT"),
    ("is_candidate", "INT"),
    ("candidate_rank", "INT"),
    ("star", "INT DEFAULT 0"),
    ("label", "TEXT"),
    ("waste_reasons", "TEXT"),
    ("brisque", "REAL"),
    ("eye_close_prob", "REAL"),
    ("scene_manual", "TEXT"),
    ("is_similar_loser", "INT"),
    ("asset_pair_id", "TEXT"),
    ("asset_role", "TEXT"),
    ("camera_model", "TEXT"),
    ("lens_model", "TEXT"),
    ("focal_length", "REAL"),
    ("shutter_speed", "TEXT"),
    ("aperture", "REAL"),
    ("iso", "INTEGER"),
    ("analysis_backend", "TEXT"),
    ("quality_model", "TEXT"),
    ("scene_model", "TEXT"),
    ("analysis_ms", "REAL"),
    ("decision_source", "TEXT"),
    ("decision_updated_at", "REAL"),
)


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _create_supporting_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS groups ("
        "id INTEGER PRIMARY KEY, size INT, best_path TEXT, "
        "is_uncertain INT DEFAULT 0, n_candidates INT DEFAULT 0)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS face_regions ("
        "path TEXT NOT NULL, face_index INTEGER NOT NULL, "
        "x REAL, y REAL, width REAL, height REAL, ear REAL, "
        "eye_close_prob REAL, sharpness REAL, PRIMARY KEY(path, face_index))"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS export_items ("
        "path TEXT NOT NULL, target_path TEXT NOT NULL, xmp_path TEXT, "
        "status TEXT NOT NULL, error TEXT, updated_at REAL NOT NULL, "
        "PRIMARY KEY(path, target_path))"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS ui_preferences ("
        "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )


def _ensure_photo_columns(connection: sqlite3.Connection) -> None:
    existing = {
        row[1] for row in connection.execute("PRAGMA table_info(photos)").fetchall()
    }
    for name, declaration in PHOTO_COLUMNS:
        if name not in existing:
            connection.execute(f'ALTER TABLE photos ADD COLUMN "{name}" {declaration}')


def _create_indexes(connection: sqlite3.Connection) -> None:
    for column in ("asset_pair_id", "star", "label", "group_id"):
        connection.execute(
            f"CREATE INDEX IF NOT EXISTS idx_photos_{column} ON photos({column})"
        )


def _migrate_to_v5(connection: sqlite3.Connection) -> None:
    _ensure_photo_columns(connection)
    _create_supporting_schema(connection)
    _create_indexes(connection)


MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {5: _migrate_to_v5}


def _create_latest_schema(connection: sqlite3.Connection) -> None:
    columns = ", ".join(f'"{name}" {declaration}' for name, declaration in PHOTO_COLUMNS)
    connection.execute(f"CREATE TABLE photos ({columns})")
    _create_supporting_schema(connection)
    _create_indexes(connection)


def _backup_path(db_path: Path) -> Path:
    candidate = db_path.with_name(f"{db_path.name}.pre-v05.bak")
    suffix = 1
    while candidate.exists():
        candidate = db_path.with_name(f"{db_path.name}.pre-v05-{suffix}.bak")
        suffix += 1
    return candidate


def _backup_database(connection: sqlite3.Connection, db_path: Path) -> Path:
    target = _backup_path(db_path)
    backup = sqlite3.connect(target)
    try:
        connection.backup(backup)
    finally:
        backup.close()
    return target


def migrate_database(connection: sqlite3.Connection, db_path: str | Path) -> Path | None:
    """Bring a database to v5, returning the backup path for upgraded old DBs."""
    path = Path(db_path)
    current = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if current > LATEST_SCHEMA_VERSION:
        raise MigrationError(
            f"database schema {current} is newer than supported {LATEST_SCHEMA_VERSION}"
        )
    if current == LATEST_SCHEMA_VERSION:
        return None
    if current == 0 and not _table_exists(connection, "photos"):
        try:
            connection.execute("BEGIN IMMEDIATE")
            _create_latest_schema(connection)
            connection.execute(f"PRAGMA user_version={LATEST_SCHEMA_VERSION}")
            connection.commit()
            return None
        except Exception as exc:
            connection.rollback()
            raise MigrationError(f"failed to create database schema: {exc}") from exc
    if current == 0:
        current = 4
    if current != 4:
        raise MigrationError(f"unsupported database schema version: {current}")

    backup_path = _backup_database(connection, path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        for version in range(current + 1, LATEST_SCHEMA_VERSION + 1):
            migration = MIGRATIONS.get(version)
            if migration is None:
                raise RuntimeError(f"missing migration step {version}")
            migration(connection)
            connection.execute(f"PRAGMA user_version={version}")
        connection.commit()
    except Exception as exc:
        connection.rollback()
        raise MigrationError(f"database upgrade failed: {exc}") from exc
    return backup_path
