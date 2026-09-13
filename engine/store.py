# -*- coding: utf-8 -*-
"""
store.py —— SQLite 读写（含 is_uncertain / is_candidate 字段）

数据模型（设计文档 4.5）：
    photos 表：path 主键，记录场景、质量指标、相似组、评分、废片/最佳帧/甄选标记、星级；
    groups 表：相似组聚合信息（size / best_path / is_uncertain / n_candidates）。

说明：
    - photo 表按 path 主键，重复分析时可增量更新（先比较 mtime）。
    - 所有人工操作（标星/改判/甄选）实时写库，界面重开可续做。

独立命令行调试：python -m engine.store <db路径> <操作...>
"""
from __future__ import annotations

import os
import sqlite3
import time

from . import config
from .log import get_logger
from .migrations import migrate_database

_log = get_logger("store")

PHOTOS_SCHEMA = """
CREATE TABLE IF NOT EXISTS photos (
    path TEXT PRIMARY KEY,
    fname TEXT, ts REAL, mtime REAL,
    width INT, height INT,
    scene TEXT, scene_conf REAL,
    blur_score REAL, over_ratio REAL, under_ratio REAL,
    aesthetic REAL, eye_open REAL, is_face INT,
    phash TEXT, group_id INT,
    comp_score REAL, is_waste INT, is_best INT,
    is_uncertain INT, is_candidate INT, candidate_rank INT,
    star INT DEFAULT 0, label TEXT, waste_reasons TEXT,
    brisque REAL, eye_close_prob REAL,
    scene_manual TEXT,
    is_similar_loser INT
);
"""

# 老库升级时需补齐的新列（ALTER TABLE ADD COLUMN）
MIGRATE_COLUMNS = [
    ("brisque", "REAL"),        # 画质模型分（v0.4 起为 musiq 等，0-100【越高越好】）
    ("eye_close_prob", "REAL"), # 闭眼分类器最大闭眼概率（0-1）
    ("scene_manual", "TEXT"),   # 人工修正的场景（NULL=用自动识别结果）
    ("is_similar_loser", "INT"),  # 相似组内排名落败者（非废片，默认可收起/可找回）
]

GROUPS_SCHEMA = """
CREATE TABLE IF NOT EXISTS groups (
    id INTEGER PRIMARY KEY, size INT, best_path TEXT,
    is_uncertain INT DEFAULT 0, n_candidates INT DEFAULT 0
);
"""

META_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY, value TEXT
);
"""

# photos 表全部字段（用于 upsert 白名单）
PHOTO_FIELDS = [
    "path", "fname", "ts", "mtime", "width", "height",
    "scene", "scene_conf", "blur_score", "over_ratio", "under_ratio",
    "aesthetic", "eye_open", "is_face", "phash", "group_id",
    "comp_score", "is_waste", "is_best",
    "is_uncertain", "is_candidate", "candidate_rank", "star", "label", "waste_reasons",
    "brisque", "eye_close_prob", "scene_manual", "is_similar_loser",
    "asset_pair_id", "asset_role", "camera_model", "lens_model", "focal_length",
    "shutter_speed", "aperture", "iso", "analysis_backend", "quality_model",
    "scene_model", "analysis_ms", "decision_source", "decision_updated_at",
]


class PhotoStore:
    def __init__(self, db_path: str, enable_wal: bool = True):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self.conn = sqlite3.connect(db_path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        # 健壮性（PRD 第 7 章）：WAL 日志模式 + 事务化写入，异常退出不损坏索引
        if enable_wal:
            try:
                self.conn.execute("PRAGMA journal_mode=WAL")
                self.conn.execute("PRAGMA synchronous=NORMAL")
            except Exception as e:  # pragma: no cover
                _log.warning("启用 WAL 失败（不影响使用）：%s", e)
        self.conn.execute("PRAGMA busy_timeout=30000")
        try:
            self._create_schema()
        except Exception:
            self.conn.close()
            raise
        self.integrity_check()

    # ------------------------------------------------------------------
    # 基础
    # ------------------------------------------------------------------
    def integrity_check(self) -> bool:
        """启动时自检库完整性；发现损坏则返回 False（由调用方决定重建）。"""
        try:
            row = self.conn.execute("PRAGMA integrity_check").fetchone()
            ok = row is not None and row[0] == "ok"
            if not ok:
                _log.error("SQLite 完整性自检未通过：%s", row[0] if row else "未知")
            return ok
        except Exception as e:
            _log.error("SQLite 完整性自检异常：%s", e)
            return False

    # ------------------------------------------------------------------
    # 基础
    # ------------------------------------------------------------------
    def _create_schema(self):
        migrate_database(self.conn, self.db_path)

    # ------------------------------------------------------------------
    # 元信息
    # ------------------------------------------------------------------
    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str):
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value))
        self.conn.commit()

    def clear_all(self):
        """清空照片与分组（切换分析源时使用）。"""
        self.conn.execute("DELETE FROM photos")
        self.conn.execute("DELETE FROM groups")
        self.conn.commit()

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def _row_to_dict(self, row) -> dict:
        return dict(row) if row is not None else {}

    # ------------------------------------------------------------------
    # 照片写入
    # ------------------------------------------------------------------
    def upsert_photo(self, photo: dict):
        """按 path 主键插入或替换整行。只取白名单字段。"""
        row = {k: photo.get(k) for k in PHOTO_FIELDS if k in photo}
        if "path" not in row:
            raise ValueError("photo 必须包含 path")
        cols = ", ".join(row.keys())
        placeholders = ", ".join(["?"] * len(row))
        sql = (f"INSERT INTO photos ({cols}) VALUES ({placeholders}) "
               f"ON CONFLICT(path) DO UPDATE SET "
               + ", ".join(f"{k}=excluded.{k}" for k in row if k != "path"))
        self.conn.execute(sql, list(row.values()))
        self.conn.commit()

    def upsert_photos_batch(self, rows: list[dict]):
        """批量 upsert（单事务提交，写入更快；用于逐张入库/断点续跑）。"""
        if not rows:
            return
        # 统一按 PHOTO_FIELDS 白名单列顺序构造，避免每条字段集合不一致
        first = {k: rows[0].get(k) for k in PHOTO_FIELDS if k in rows[0]}
        if "path" not in first:
            raise ValueError("photo 必须包含 path")
        cols = list(first.keys())
        cols_sql = ", ".join(cols)
        placeholders = ", ".join(["?"] * len(cols))
        sql = (f"INSERT INTO photos ({cols_sql}) VALUES ({placeholders}) "
               f"ON CONFLICT(path) DO UPDATE SET "
               + ", ".join(f"{k}=excluded.{k}" for k in cols if k != "path"))
        # 行字段按 cols 顺序取（缺失字段补 None）
        with self.conn:
            self.conn.executemany(
                sql, [[r.get(k) for k in cols] for r in rows])

    def photos_missing(self, field: str) -> set[str]:
        """断点续跑辅助：返回指定字段为空(NULL)的照片 path 集合。
        例如 field='aesthetic' 可找出还没跑 CLIP 的照片。"""
        if field not in PHOTO_FIELDS:
            return set()
        rows = self.conn.execute(
            f"SELECT path FROM photos WHERE {field} IS NULL").fetchall()
        return {r[0] for r in rows}

    def count_photos_where(self, condition: str, *args) -> int:
        """按条件计数（condition 为 SQL 片段，仅限内部可信调用）。"""
        row = self.conn.execute(
            f"SELECT COUNT(*) FROM photos WHERE {condition}", args).fetchone()
        return row[0] if row else 0

    def update_photo(self, path: str, **fields):
        """更新指定字段（白名单过滤）。"""
        allowed = {k: v for k, v in fields.items() if k in PHOTO_FIELDS and k != "path"}
        if not allowed:
            return
        sets = ", ".join(f"{k}=?" for k in allowed)
        self.conn.execute(f"UPDATE photos SET {sets} WHERE path=?", (*allowed.values(), path))
        self.conn.commit()

    def set_star(self, path: str, star: int):
        self.update_photo(path, star=int(star))

    def set_label(self, path: str, label: str):
        self.update_photo(path, label=label)

    def set_pick(self, path: str, label: str):
        """P 保留 / X 排除：写入 label，同时把 star 置为 5（保留）或 0（排除）。"""
        if label == "P":
            self.update_photo(path, label="P", star=5)
        elif label == "X":
            self.update_photo(path, label="X", star=0)
        else:
            self.update_photo(path, label=label)

    def set_scene_manual(self, path: str, scene: str | None):
        """场景手动修正：写入 scene_manual（NULL 表示回到自动识别）。"""
        self.update_photo(path, scene_manual=scene)

    def scene_overrides(self) -> dict[str, str]:
        """返回 {path: scene} 的人工场景修正表（供分析时覆盖自动场景）。"""
        rows = self.conn.execute(
            "SELECT path, scene_manual FROM photos WHERE scene_manual IS NOT NULL").fetchall()
        return {r["path"]: r["scene_manual"] for r in rows}

    def apply_decision(
        self,
        paths: list[str],
        *,
        star: int,
        label: str | None,
        source: str,
    ) -> list[dict]:
        """Atomically apply a decision to paths and every paired asset member."""
        if not paths:
            return []
        placeholders = ",".join("?" for _ in paths)
        selected = self.conn.execute(
            f"SELECT path, asset_pair_id FROM photos WHERE path IN ({placeholders})",
            paths,
        ).fetchall()
        pair_ids = sorted({row["asset_pair_id"] for row in selected if row["asset_pair_id"]})
        conditions = [f"path IN ({placeholders})"]
        arguments: list[object] = list(paths)
        if pair_ids:
            pair_placeholders = ",".join("?" for _ in pair_ids)
            conditions.append(f"asset_pair_id IN ({pair_placeholders})")
            arguments.extend(pair_ids)
        where = " OR ".join(conditions)
        before = [
            self._row_to_dict(row)
            for row in self.conn.execute(
                "SELECT path, star, label, decision_source, decision_updated_at "
                f"FROM photos WHERE {where} ORDER BY path",
                arguments,
            ).fetchall()
        ]
        updated_at = time.time()
        with self.conn:
            self.conn.execute(
                "UPDATE photos SET star=?, label=?, decision_source=?, "
                f"decision_updated_at=? WHERE {where}",
                [int(star), label, source, updated_at, *arguments],
            )
        return before

    def restore_decisions(self, before: tuple[dict, ...] | list[dict]) -> None:
        """Restore the exact pre-decision state for all paired members atomically."""
        with self.conn:
            self.conn.executemany(
                "UPDATE photos SET star=?, label=?, decision_source=?, "
                "decision_updated_at=? WHERE path=?",
                [
                    (
                        row["star"],
                        row["label"],
                        row["decision_source"],
                        row["decision_updated_at"],
                        row["path"],
                    )
                    for row in before
                ],
            )

    def review_snapshot(self) -> dict[str, list[dict]]:
        """Read all review data in three bulk queries, never once per photo."""
        return {
            "photos": [self._row_to_dict(row) for row in self.conn.execute(
                "SELECT * FROM photos ORDER BY ts, path"
            ).fetchall()],
            "groups": [self._row_to_dict(row) for row in self.conn.execute(
                "SELECT * FROM groups ORDER BY id"
            ).fetchall()],
            "face_regions": [self._row_to_dict(row) for row in self.conn.execute(
                "SELECT * FROM face_regions ORDER BY path, face_index"
            ).fetchall()],
        }

    def replace_face_regions(self, path: str, regions: list[dict]) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM face_regions WHERE path=?", (path,))
            self.conn.executemany(
                "INSERT INTO face_regions (path, face_index, x, y, width, height, "
                "ear, eye_close_prob, sharpness) VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    (
                        path,
                        region["face_index"],
                        region.get("x"),
                        region.get("y"),
                        region.get("width"),
                        region.get("height"),
                        region.get("ear"),
                        region.get("eye_close_prob"),
                        region.get("sharpness"),
                    )
                    for region in regions
                ],
            )

    def face_regions(self, path: str) -> list[dict]:
        return [
            self._row_to_dict(row)
            for row in self.conn.execute(
                "SELECT * FROM face_regions WHERE path=? ORDER BY face_index", (path,)
            ).fetchall()
        ]

    def get_preference(self, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM ui_preferences WHERE key=?", (key,)
        ).fetchone()
        return row[0] if row else None

    def set_preference(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO ui_preferences(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def record_export_item(
        self,
        path: str,
        target_path: str,
        xmp_path: str | None,
        status: str,
        error: str | None,
    ) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO export_items(path, target_path, xmp_path, status, error, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(path, target_path) DO UPDATE SET "
                "xmp_path=excluded.xmp_path, status=excluded.status, error=excluded.error, "
                "updated_at=excluded.updated_at",
                (path, target_path, xmp_path, status, error, time.time()),
            )

    def failed_export_items(self, target_dir: str | None = None) -> list[dict]:
        """Return failed rows, optionally limited to one delivery directory."""
        from pathlib import Path

        requested = Path(target_dir).resolve(strict=False) if target_dir else None
        rows = self.conn.execute(
            "SELECT * FROM export_items WHERE status='failed' ORDER BY updated_at, path"
        ).fetchall()
        return [
            self._row_to_dict(row)
            for row in rows
            if requested is None
            or Path(row["target_path"]).resolve(strict=False).parent == requested
        ]

    # ------------------------------------------------------------------
    # 相似组
    # ------------------------------------------------------------------
    def set_group(self, group_id: int, size: int, best_path: str,
                  is_uncertain: bool = False, n_candidates: int = 0):
        self.conn.execute(
            "INSERT INTO groups (id, size, best_path, is_uncertain, n_candidates) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET size=excluded.size, best_path=excluded.best_path, "
            "is_uncertain=excluded.is_uncertain, n_candidates=excluded.n_candidates",
            (group_id, size, best_path, int(is_uncertain), n_candidates),
        )
        self.conn.commit()

    def get_group(self, group_id: int) -> dict:
        row = self.conn.execute("SELECT * FROM groups WHERE id=?", (group_id,)).fetchone()
        return self._row_to_dict(row)

    def all_groups(self, uncertain_only: bool = False, min_size: int = 1) -> list[dict]:
        sql = "SELECT * FROM groups WHERE size >= ?"
        args = [min_size]
        if uncertain_only:
            sql += " AND is_uncertain = 1"
        sql += " ORDER BY id"
        return [self._row_to_dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def group_members(self, group_id: int, order_by: str = "comp_score DESC") -> list[dict]:
        return [self._row_to_dict(r) for r in self.conn.execute(
            f"SELECT * FROM photos WHERE group_id=? ORDER BY {order_by}", (group_id,)).fetchall()]

    def clear_groups(self):
        self.conn.execute("DELETE FROM groups")
        self.conn.commit()

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def get_photo(self, path: str) -> dict:
        return self._row_to_dict(self.conn.execute(
            "SELECT * FROM photos WHERE path=?", (path,)).fetchone())

    def photos_map(self) -> dict[str, dict]:
        """一次性读回全表 {path: row}，避免 1000+ 次逐行查询（性能关键路径）。"""
        rows = self.conn.execute("SELECT * FROM photos").fetchall()
        return {r["path"]: self._row_to_dict(r) for r in rows}

    def all_photos(self, order_by: str = "comp_score DESC") -> list[dict]:
        return [self._row_to_dict(r) for r in
                self.conn.execute(f"SELECT * FROM photos ORDER BY {order_by}").fetchall()]

    def query_photos(self, filters: dict | None = None,
                     order_by: str = "comp_score DESC") -> list[dict]:
        """按条件过滤查询。filters 支持：scene, star, star_min, is_waste, is_best,
        is_uncertain, is_candidate, group_id, label。"""
        sql = "SELECT * FROM photos WHERE 1=1"
        args = []
        if filters:
            f = dict(filters)
            if "scene" in f and f["scene"]:
                sql += " AND scene=?"
                args.append(f["scene"])
            if "star" in f:
                sql += " AND star=?"
                args.append(f["star"])
            if "star_min" in f:
                sql += " AND star>=?"
                args.append(f["star_min"])
            if "is_waste" in f:
                sql += " AND is_waste=?"
                args.append(int(f["is_waste"]))
            if "is_best" in f:
                sql += " AND is_best=?"
                args.append(int(f["is_best"]))
            if "is_uncertain" in f:
                sql += " AND is_uncertain=?"
                args.append(int(f["is_uncertain"]))
            if "is_candidate" in f:
                sql += " AND is_candidate=?"
                args.append(int(f["is_candidate"]))
            if "group_id" in f and f["group_id"] is not None:
                sql += " AND group_id=?"
                args.append(f["group_id"])
            if "label" in f and f["label"]:
                sql += " AND label=?"
                args.append(f["label"])
            if "search" in f and f["search"]:
                sql += " AND fname LIKE ?"
                args.append(f"%{f['search']}%")
        sql += f" ORDER BY {order_by}"
        return [self._row_to_dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def count_photos(self, filters: dict | None = None) -> int:
        return len(self.query_photos(filters, order_by="path"))

    def candidates(self, group_id: int) -> list[dict]:
        """某不确定组的候选照片（按 candidate_rank 排序）。"""
        return [self._row_to_dict(r) for r in self.conn.execute(
            "SELECT * FROM photos WHERE group_id=? AND is_candidate=1 "
            "ORDER BY candidate_rank", (group_id,)).fetchall()]

    def uncertain_groups(self) -> list[dict]:
        """待甄选的组（按 id 排序）。"""
        return self.all_groups(uncertain_only=True)

    # ------------------------------------------------------------------
    # 统计与清理
    # ------------------------------------------------------------------
    def stats(self) -> dict:
        def scalar(sql, *a):
            row = self.conn.execute(sql, a).fetchone()
            return row[0] if row else 0
        return {
            "total": scalar("SELECT COUNT(*) FROM photos"),
            "waste": scalar("SELECT COUNT(*) FROM photos WHERE is_waste=1"),
            "starred": scalar("SELECT COUNT(*) FROM photos WHERE star>0"),
            "star5": scalar("SELECT COUNT(*) FROM photos WHERE star=5"),
            "best": scalar("SELECT COUNT(*) FROM photos WHERE is_best=1"),
            "uncertain_photos": scalar("SELECT COUNT(*) FROM photos WHERE is_uncertain=1"),
            "candidate_photos": scalar("SELECT COUNT(*) FROM photos WHERE is_candidate=1"),
            "uncertain_groups": scalar("SELECT COUNT(*) FROM groups WHERE is_uncertain=1"),
            "groups": scalar("SELECT COUNT(*) FROM groups"),
            "scenes": {r["scene"]: r["n"] for r in self.conn.execute(
                "SELECT scene, COUNT(*) n FROM photos GROUP BY scene")},
        }

# ---------------------------------------------------------------------------
# 命令行调试入口
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    db = sys.argv[1] if len(sys.argv) > 1 else "data/cull.db"
    op = sys.argv[2] if len(sys.argv) > 2 else "stats"
    with PhotoStore(db) as s:
        if op == "stats":
            st = s.stats()
            print("统计：", st)
        elif op == "groups":
            for g in s.all_groups():
                print(f"  组{g['id']}: size={g['size']} best={g['best_path']} "
                      f"uncertain={g['is_uncertain']} n_cand={g['n_candidates']}")
        elif op == "photos":
            for p in s.all_photos():
                print(f"  {p['fname']} scene={p['scene']} comp={p['comp_score']:.1f} "
                      f"waste={p['is_waste']} star={p['star']} grp={p['group_id']}")
