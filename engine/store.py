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

import csv
import json
import os
from pathlib import Path
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

# 【P0-1】由【模型 / 算法】派生的度量列。
# 这些值的含义随「美学模型 + 画质模型 + 闭眼模型」的签名而变——换了模型，
# 旧值就不再可比，必须重算。模型签名变化时只清空本清单，其余一律保留。
#
# 刻意【不】包含：path / fname / ts / mtime / width / height（文件事实）、
# asset_pair_id / asset_role（由文件名+EXIF 配对派生）、camera_model / lens_model /
# focal_length / shutter_speed / aperture / iso（EXIF 事实），以及摄影师的人工
# 作业 star / label / decision_source / decision_updated_at / scene_manual。
METRIC_FIELDS = [
    "scene", "scene_conf",
    "blur_score", "over_ratio", "under_ratio",
    "aesthetic", "eye_open", "is_face", "eye_close_prob", "brisque",
    "phash", "group_id", "comp_score",
    "is_waste", "is_best", "is_uncertain", "is_candidate", "candidate_rank",
    "is_similar_loser", "waste_reasons",
    "analysis_backend", "quality_model", "scene_model", "analysis_ms",
]

# 人工决策字段（换分析源时归档/恢复的对象）
DECISION_FIELDS = (
    "path", "star", "label", "decision_source", "decision_updated_at", "scene_manual",
)

# 判定"这一行有人工作业"的口径：任一人工作业字段非空即算。
_HAS_DECISION_SQL = (
    "star > 0 OR label IS NOT NULL OR decision_source IS NOT NULL "
    "OR scene_manual IS NOT NULL"
)


def _format_timestamp(value) -> str:
    """epoch 秒 → 本地可读时间；空值/非法值返回空串（供导出清单使用）。"""
    if value is None:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(value)))
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


class PhotoStore:
    def __init__(self, db_path: str, enable_wal: bool = True):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        # 【v0.6 S4-1】旧库（schema v4）升级前会自动备份，备份路径留在实例上：
        # UI 必须能如实告诉用户"这个项目被升级过、备份在哪"，而不是悄悄改库。
        self.upgrade_backup: Path | None = None
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
        self.upgrade_backup = migrate_database(self.conn, self.db_path)

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

    def clear_meta(self, key: str) -> None:
        """删除一个 meta 键（回到"从未设置"）。

        与 `clear_preference` 同理：`set_meta` 只能写字符串，无法表达"原来是
        None"。撤销"首次设置"时必须删行，否则会留下空串，与初始状态对不上。
        """
        with self.conn:
            self.conn.execute("DELETE FROM meta WHERE key=?", (key,))

    def clear_all(self):
        """清空照片与分组（切换分析源时使用）。"""
        self.conn.execute("DELETE FROM photos")
        self.conn.execute("DELETE FROM groups")
        self.conn.commit()

    # ------------------------------------------------------------------
    # 【P0-1】模型签名变化时的数据保护
    # ------------------------------------------------------------------
    def reset_metrics(self):
        """清空【模型派生】的度量列与派生表，保留人工决策。

        与 clear_all() 的差别正是 P0-1 的修复点：
          * **不删行** → 摄影师的复核结果（star / label / decision_source /
            decision_updated_at）与人工场景修正（scene_manual）全部保留；
          * 度量列置 NULL、groups / face_regions 清空 → pipeline 的增量判断
            （blur_score IS NULL / phash 为空 → 重新纳入阶段一；
             aesthetic 或 scene 为空 → 重新纳入 CLIP）会自动把这些照片重算一遍。

        净效果：换后端 / 换模型 = 「重算，但不丢人工作业」。
        旧实现调用 clear_all()，会连同人工标星与判废一起删掉，且全程无提示。
        """
        assignments = ", ".join(f"{field}=NULL" for field in METRIC_FIELDS)
        with self.conn:
            self.conn.execute(f"UPDATE photos SET {assignments}")
            self.conn.execute("DELETE FROM groups")
            self.conn.execute("DELETE FROM face_regions")

    def stash_decisions(self, key: str) -> int:
        """把库中全部人工决策快照写入 meta（换分析源前调用），返回条数。

        换分析源会整库清空。先归档，才可能在用户切回同一目录时还原。
        """
        rows = [
            self._row_to_dict(row)
            for row in self.conn.execute(
                f"SELECT {', '.join(DECISION_FIELDS)} FROM photos WHERE {_HAS_DECISION_SQL}"
            ).fetchall()
        ]
        self.set_meta(key, json.dumps(rows, ensure_ascii=False))
        return len(rows)

    def rehydrate_decisions(self, key: str) -> int:
        """用 meta 里的决策快照重建「决策骨架行」（只含 path + 人工字段）。

        度量列留 NULL，随后的增量分析会把它们重新算出来；而 star / label /
        scene_manual 已经在位，因此「切走目录 A、又切回 A」不会丢掉之前的复核。
        已存在的行不覆盖（DO NOTHING），避免压掉本次会话刚做的判断。
        """
        raw = self.get_meta(key)
        if not raw:
            return 0
        try:
            rows = json.loads(raw)
        except (TypeError, ValueError):
            _log.warning("决策快照损坏，已忽略：%s", key)
            return 0
        if not isinstance(rows, list):
            return 0
        payload = [
            (
                row.get("path"), row.get("star"), row.get("label"),
                row.get("decision_source"), row.get("decision_updated_at"),
                row.get("scene_manual"),
            )
            for row in rows
            if isinstance(row, dict) and row.get("path")
        ]
        if not payload:
            return 0
        with self.conn:
            self.conn.executemany(
                "INSERT INTO photos (path, star, label, decision_source, "
                "decision_updated_at, scene_manual) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(path) DO NOTHING",
                payload,
            )
        return len(payload)

    # ------------------------------------------------------------------
    # 【v0.6 S4-1 / G4】旧库升级提示 + 已复核清单导出（兜底）
    # ------------------------------------------------------------------
    def count_decisions(self) -> int:
        """有人工作业的照片数（star / label / decision_source / scene_manual 任一非空）。"""
        row = self.conn.execute(
            f"SELECT COUNT(*) FROM photos WHERE {_HAS_DECISION_SQL}"
        ).fetchone()
        return row[0] if row else 0

    def legacy_backends(self) -> list[str]:
        """返回库中【已不再发行】的分析后端名（旧库升级提示用）。

        判定口径是"是否还在后端注册表里"，而不是硬编码 "heuristic"：
        轻量后端已随 v0.6 S2 下线、不在注册表中，于是自然被判为过时；
        将来新增的 onnx 后端只要注册过就不会被误报。
        """
        from .inference import backend_names

        known = set(backend_names())
        rows = self.conn.execute(
            "SELECT DISTINCT analysis_backend FROM photos "
            "WHERE analysis_backend IS NOT NULL AND analysis_backend <> ''"
        ).fetchall()
        return sorted(name for (name,) in rows if name not in known)

    def upgrade_report(self) -> dict:
        """给 UI 用的「这个项目是不是旧库、升级时发生了什么」报告。

        两种"旧"都要如实说清楚，不能让用户在无感知的情况下被换掉一套分数：

          * **schema 升级**：v4 → v5，升级前已自动备份（备份路径一并返回）；
          * **分析源过时**：库里记录的 analysis_backend 已不再发行（典型是轻量版
            heuristic），它的指标与当前版本不可比，下次分析会重算 —— 而人工决策
            由 reset_metrics() 保住。
        """
        from .inference import DEFAULT_BACKEND

        legacy = self.legacy_backends()
        backup = str(self.upgrade_backup) if self.upgrade_backup else None
        decisions = self.count_decisions()
        return {
            "schema_upgraded": backup is not None,
            "backup_path": backup,
            "legacy_backends": legacy,
            "current_backend": DEFAULT_BACKEND,
            "decision_count": decisions,
            "needs_attention": bool(backup or legacy),
            "source_root": self.get_meta("source_root"),
        }

    def upgrade_summary(self) -> str:
        """把 upgrade_report() 变成一句可直接显示给用户的中文说明。"""
        report = self.upgrade_report()
        if not report["needs_attention"]:
            return "项目为当前版本，无需升级。"
        parts = []
        if report["schema_upgraded"]:
            parts.append(
                f"项目数据库已从旧版 schema 升级；升级前的完整备份在 {report['backup_path']}"
            )
        if report["legacy_backends"]:
            names = "、".join(report["legacy_backends"])
            parts.append(
                f"这批照片由已下线的分析后端（{names}）产生，指标与当前版本"
                f"（{report['current_backend']}）不可比，下次分析会自动重算"
            )
        parts.append(f"你的人工复核（{report['decision_count']} 张）会全部保留")
        return "；".join(parts) + "。"

    def export_decisions(self, target: str | Path) -> dict:
        """把全部人工复核导出为 CSV（UTF-8 BOM，Excel 可直接打开中文）。

        【v0.6 S4-1】这是"兜底"：库文件损坏、被误删、或升级后表现异常时，摄影师
        手里仍有一份**可直接阅读**的复核清单（路径 / 星级 / 保留排除 / 人工场景 /
        决策来源 / 决策时间），不必依赖任何程序才能看一眼自己做过什么。
        """
        path = Path(target)
        if path.parent and str(path.parent):
            path.parent.mkdir(parents=True, exist_ok=True)
        fields = ", ".join(DECISION_FIELDS)
        rows = self.conn.execute(
            f"SELECT {fields} FROM photos WHERE {_HAS_DECISION_SQL} ORDER BY path"
        ).fetchall()
        header = ["文件路径", "文件名", "星级", "保留/排除", "人工场景", "决策来源", "决策时间"]
        # newline="" 是 csv 模块的要求；utf-8-sig 让 Excel 正确识别中文
        with open(path, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(header)
            for row in rows:
                writer.writerow([
                    row["path"],
                    os.path.basename(row["path"] or ""),
                    row["star"] if row["star"] is not None else 0,
                    row["label"] or "",
                    row["scene_manual"] or "",
                    row["decision_source"] or "",
                    _format_timestamp(row["decision_updated_at"]),
                ])
        return {"path": str(path), "rows": len(rows), "bytes": path.stat().st_size}

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
        """批量 upsert（单事务提交，写入更快；用于逐张入库/断点续跑）。

        列集合取【所有行键的并集】（按 PHOTO_FIELDS 顺序），而不是只看首行。
        【P1-1】旧实现取 rows[0] 的键：一旦首行是"解码失败"的骨架行（不含任何
        度量字段），同一块里其余照片的 blur / phash / 曝光 / 人脸指标就会被
        静默丢弃，而且断点续跑还会把它们判成"缺指标"反复重算。
        """
        if not rows:
            return
        present: set[str] = set()
        for row in rows:
            present.update(row.keys())
        cols = [field for field in PHOTO_FIELDS if field in present]
        if "path" not in cols:
            raise ValueError("photo 必须包含 path")
        cols_sql = ", ".join(cols)
        placeholders = ", ".join(["?"] * len(cols))
        sql = (f"INSERT INTO photos ({cols_sql}) VALUES ({placeholders}) "
               f"ON CONFLICT(path) DO UPDATE SET "
               + ", ".join(f"{k}=excluded.{k}" for k in cols if k != "path"))
        # 行字段按 cols 顺序取（该行缺失的字段补 None）
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

    def clear_preference(self, key: str) -> None:
        """删除一个偏好键（回到"从未设置"）。

        撤销需要它：`set_preference` 只能写入字符串，无法表达"原来是 None"。
        复原到"没设过这个键"必须删行，而不是写空串 —— 两者在 `get_preference`
        的返回值上不同（None vs ""），会让撤销后的状态与原状对不上。
        """
        with self.conn:
            self.conn.execute("DELETE FROM ui_preferences WHERE key=?", (key,))

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
