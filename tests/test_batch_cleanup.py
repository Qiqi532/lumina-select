"""T-10 批量操作 + 清理已排除项。

验收对照（PLAN.md T-10）：
    · 批量 500 张写入 < 3 s
    · 清理后文件在隔离目录且可原样恢复
另加 N3 红线：清理路径上**不得出现任何删除调用**。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from engine.store import PhotoStore
from services.command_catalog import build_registry, last_quarantine
from services.quarantine import (
    QuarantineEntry,
    iter_quarantine_dirs,
    quarantine_files,
    quarantine_root,
    restore_quarantine,
)
from services.review_service import ReviewService

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
def _store(tmp_path, count: int = 6):
    """造一个含 count 个 asset 的库（每个 asset 一张照片）。"""
    store = PhotoStore(str(tmp_path / "review.db"), enable_wal=False)
    for index in range(count):
        path = f"/photos/p{index:03d}.CR3"
        store.upsert_photo(
            {
                "path": path,
                "fname": f"p{index:03d}.CR3",
                "asset_pair_id": f"pair-{index:03d}",
                "asset_role": "raw",
                "star": 0,
                "label": None,
                "blur_score": 88.0,
            }
        )
    return store


def _real_files(root: Path, count: int = 4) -> list[str]:
    """在 root 下造 count 个真实文件，返回路径列表。"""
    root.mkdir(parents=True, exist_ok=True)
    paths = []
    for index in range(count):
        path = root / f"img{index}.CR3"
        path.write_bytes(b"raw" + bytes([index]))
        paths.append(str(path))
    return paths


# ---------------------------------------------------------------------------
# 隔离引擎：搬移而非删除
# ---------------------------------------------------------------------------
class TestQuarantineMovesNeverDeletes:
    def test_files_land_in_trash_dir_and_sources_are_gone(self, tmp_path):
        project = tmp_path / "project"
        created = _real_files(project, 3)
        result = quarantine_files(created, project, stamp="20261014")
        assert result.moved_count == 3
        assert not result.failed
        trash = quarantine_root(project, stamp="20261014")
        assert trash.is_dir()
        for source in created:
            assert not Path(source).exists(), "原位置应已搬空"
        for entry in result.moved:
            assert Path(entry.target).exists()
            assert Path(entry.target).is_relative_to(trash)

    def test_manifest_is_written_for_human_recovery(self, tmp_path):
        project = tmp_path / "project"
        created = _real_files(project, 2)
        result = quarantine_files(created, project, stamp="20261014", reason="test")
        assert result.manifest_path
        payload = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
        assert payload["reason"] == "test"
        assert len(payload["moved"]) == 2

    def test_manifest_accumulates_across_runs(self, tmp_path):
        project = tmp_path / "project"
        first = _real_files(project, 2)
        quarantine_files(first, project, stamp="20261014")
        second = _real_files(project, 2)
        result = quarantine_files(second, project, stamp="20261014")
        payload = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
        assert len(payload["moved"]) == 4, "同一天多次清理的清单必须累加，不能覆盖"

    def test_missing_source_is_reported_not_failed(self, tmp_path):
        project = tmp_path / "project"
        result = quarantine_files([str(project / "nope.CR3")], project)
        assert result.moved_count == 0
        assert len(result.missing) == 1
        assert not result.failed, "源文件不在不算失败"

    def test_name_collision_gets_unique_suffix(self, tmp_path):
        project = tmp_path / "project"
        first = _real_files(project, 1)
        quarantine_files(first, project, stamp="20261014")
        # 再放一个同名文件（内容不同），第二次隔离不能覆盖第一次的
        again = _real_files(project, 1)
        result = quarantine_files(again, project, stamp="20261014")
        assert result.moved_count == 1
        targets = {entry.target for entry in result.moved}
        assert all(Path(t).exists() for t in targets)
        names = sorted(p.name for p in quarantine_root(project, stamp="20261014").iterdir()
                       if p.is_file() and p.suffix == ".CR3")
        assert len(names) == 2, "两个同名文件必须都在隔离目录里，一个带 ~N 后缀"

    def test_source_module_has_no_delete_calls(self):
        """N3 红线：隔离引擎里不得出现任何删除调用。

        按 **AST** 查调用而不是搜文本 —— 搜文本会命中本文件自己那句
        "不得含 os.remove"的说明文字，那是假阳性。
        """
        import ast

        module = ast.parse(
            (Path(__file__).resolve().parents[1] / "services" / "quarantine.py")
            .read_text(encoding="utf-8")
        )
        banned = {"remove", "unlink", "rmtree", "rmdir"}
        for node in ast.walk(module):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            assert name not in banned, f"隔离引擎不得调用 {name}()"

    def test_iter_quarantine_dirs_lists_trash_only(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir(parents=True)
        (project / ".trash_20261014").mkdir()
        (project / ".trash_20261015").mkdir()
        (project / "photos").mkdir()
        names = [p.name for p in iter_quarantine_dirs(project)]
        assert names == [".trash_20261014", ".trash_20261015"]


# ---------------------------------------------------------------------------
# 还原
# ---------------------------------------------------------------------------
class TestRestore:
    def test_restore_brings_files_back_byte_identical(self, tmp_path):
        project = tmp_path / "project"
        created = _real_files(project, 3)
        original = {path: Path(path).read_bytes() for path in created}
        result = quarantine_files(created, project, stamp="20261014")
        restored = restore_quarantine(result)
        assert restored.restored_count == 3
        assert not restored.failed
        for path, payload in original.items():
            assert Path(path).read_bytes() == payload, "还原必须逐字节一致"

    def test_restore_refuses_to_overwrite_existing(self, tmp_path):
        project = tmp_path / "project"
        created = _real_files(project, 1)
        result = quarantine_files(created, project, stamp="20261014")
        # 原位置被别的文件占了
        Path(created[0]).write_bytes(b"something-else")
        restored = restore_quarantine(result)
        assert restored.restored_count == 0
        assert len(restored.failed) == 1
        assert Path(result.moved[0].target).exists(), "不能为了还原就丢掉隔离副本"

    def test_restore_accepts_plain_entries(self, tmp_path):
        project = tmp_path / "project"
        created = _real_files(project, 1)
        result = quarantine_files(created, project, stamp="20261014")
        restored = restore_quarantine(result.moved)
        assert restored.restored_count == 1


# ---------------------------------------------------------------------------
# 单事务批量写
# ---------------------------------------------------------------------------
class TestBatchDecisionWrite:
    def test_batch_writes_all_rows(self, tmp_path):
        store = _store(tmp_path, 10)
        try:
            paths = [f"/photos/p{index:03d}.CR3" for index in range(10)]
            before = store.apply_decisions_batch(
                [(path, 4, "P") for path in paths], source="manual"
            )
            assert len(before) == 10
            for path in paths:
                row = store.get_photo(path)
                assert row["star"] == 4
                assert row["label"] == "P"
        finally:
            store.close()

    def test_batch_is_atomic_on_failure(self, tmp_path):
        """中途出错必须整体回滚 —— 不能留下"改了一半"的库。"""
        store = _store(tmp_path, 5)
        try:
            paths = [f"/photos/p{index:03d}.CR3" for index in range(5)]
            # 掺一个非法 star（SQLite 之外的人为失败点：star 非整数）
            with pytest.raises((TypeError, ValueError)):
                store.apply_decisions_batch(
                    [(paths[0], 4, "P"), (paths[1], "boom", None)], source="manual"
                )
            for path in paths:
                assert int(store.get_photo(path)["star"]) == 0, "失败后不得留下部分写入"
        finally:
            store.close()

    def test_batch_follows_paired_assets(self, tmp_path):
        """批量给 RAW 打标，配对的 JPEG 必须跟着变。"""
        store = PhotoStore(str(tmp_path / "pair.db"), enable_wal=False)
        try:
            for role, ext in (("raw", "CR3"), ("jpeg", "JPG")):
                store.upsert_photo(
                    {
                        "path": f"/photos/x.{ext}",
                        "fname": f"x.{ext}",
                        "asset_pair_id": "pair-x",
                        "asset_role": role,
                        "star": 0,
                    }
                )
            before = store.apply_decisions_batch(
                [("/photos/x.CR3", 5, "P")], source="manual"
            )
            assert len(before) == 2, "配对成员必须一起写入"
            assert store.get_photo("/photos/x.JPG")["label"] == "P"
        finally:
            store.close()

    def test_batch_500_rows_under_three_seconds(self, tmp_path):
        """PLAN 验收：批量 500 张写入 < 3 s。"""
        store = _store(tmp_path, 500)
        try:
            paths = [f"/photos/p{index:03d}.CR3" for index in range(500)]
            started = time.perf_counter()
            before = store.apply_decisions_batch(
                [(path, 3, None) for path in paths], source="manual"
            )
            elapsed = time.perf_counter() - started
            assert len(before) == 500
            assert elapsed < 3.0, f"500 张批量写入 {elapsed:.3f}s 超过 3s 门禁"
        finally:
            store.close()


# ---------------------------------------------------------------------------
# 命令表：photo.batch_update / cleanup.rejected
# ---------------------------------------------------------------------------
class TestBatchCommand:
    def test_batch_update_and_undo_roundtrip(self, tmp_path):
        store = _store(tmp_path, 8)
        try:
            commands = build_registry(store)
            paths = [f"/photos/p{index:03d}.CR3" for index in range(8)]
            result = commands.execute("photo.batch_update", {"paths": paths, "star": 5, "flag": "P"})
            assert result.ok
            assert result.value["count"] == 8
            for path in paths:
                assert store.get_photo(path)["label"] == "P"
            assert commands.undo()
            for path in paths:
                row = store.get_photo(path)
                assert row["label"] is None
                assert int(row["star"]) == 0, "撤销必须回到批量前的状态"
        finally:
            store.close()

    def test_cleanup_quarantines_real_files(self, tmp_path):
        project = tmp_path / "project"
        created = _real_files(project, 3)
        store = _store(tmp_path, 3)
        try:
            for path in created:
                store.upsert_photo({"path": path, "fname": Path(path).name, "star": 0, "label": "X"})
            commands = build_registry(store)
            result = commands.execute(
                "cleanup.rejected", {"paths": created, "project_dir": str(project)}
            )
            assert result.ok
            assert result.value["quarantined"] == 3
            assert all(not Path(p).exists() for p in created)
            assert last_quarantine(store) is not None
        finally:
            store.close()

    def test_cleanup_undo_restores_files(self, tmp_path):
        """验收核心：清理后文件在隔离目录，且可原样恢复。"""
        project = tmp_path / "project"
        created = _real_files(project, 3)
        payloads = {p: Path(p).read_bytes() for p in created}
        store = _store(tmp_path, 3)
        try:
            for path in created:
                store.upsert_photo({"path": path, "fname": Path(path).name, "star": 0, "label": "X"})
            commands = build_registry(store)
            commands.execute("cleanup.rejected", {"paths": created, "project_dir": str(project)})
            assert all(not Path(p).exists() for p in created)
            assert commands.undo()
            for path, payload in payloads.items():
                assert Path(path).exists(), "撤销清理必须把文件搬回原处"
                assert Path(path).read_bytes() == payload
        finally:
            store.close()

    def test_cleanup_without_project_dir_fails_loudly(self, tmp_path):
        store = _store(tmp_path, 1)
        try:
            commands = build_registry(store)
            result = commands.execute("cleanup.rejected", {"paths": ["/photos/p000.CR3"]})
            assert not result.ok, "缺 project_dir 必须显式失败，不能静默 no-op"
        finally:
            store.close()


# ---------------------------------------------------------------------------
# ReviewService 多选与批量
# ---------------------------------------------------------------------------
class TestSelection:
    def _service(self, tmp_path) -> ReviewService:
        store = PhotoStore(str(tmp_path / "sel.db"), enable_wal=False)
        for index in range(5):
            store.upsert_photo(
                {
                    "path": f"/photos/s{index}.CR3",
                    "fname": f"s{index}.CR3",
                    "asset_pair_id": f"s{index}",
                    "asset_role": "raw",
                    "star": 0,
                    "blur_score": 88.0,
                }
            )
        return ReviewService(store)

    def test_select_only_toggle_and_range(self, tmp_path):
        service = self._service(tmp_path)
        service.select_only("s0")
        service.select_range("s2")
        assert service.selection == ["s0", "s1", "s2"]
        service.select_toggle("s1")
        assert service.selection == ["s0", "s2"]
        service.select_only("s4")
        assert service.selection == ["s4"]

    def test_select_all_covers_queue_only(self, tmp_path):
        service = self._service(tmp_path)
        service.select_all()
        assert len(service.selection) == 5
        service.set_filter("rejected")
        service.select_all()
        assert service.selection == [], "全选只覆盖当前队列（筛后为空就是空）"

    def test_set_selection_drops_unknown_ids(self, tmp_path):
        service = self._service(tmp_path)
        service.set_selection(["s0", "ghost", "s1", "s0"])
        assert service.selection == ["s0", "s1"], "不存在的 id 必须剔除，避免已选数虚高"

    def test_selected_paths_expand_paired_members(self, tmp_path):
        store = PhotoStore(str(tmp_path / "pair.db"), enable_wal=False)
        for role, ext in (("raw", "CR3"), ("jpeg", "JPG")):
            store.upsert_photo(
                {
                    "path": f"/photos/y.{ext}",
                    "fname": f"y.{ext}",
                    "asset_pair_id": "y",
                    "asset_role": role,
                    "star": 0,
                    "blur_score": 88.0,
                }
            )
        service = ReviewService(store)
        service.select_only("y")
        assert len(service.selected_paths()) == 2, "RAW+JPEG 必须成对展开"


class TestBatchApply:
    def test_batch_applies_and_undo_restores(self, tmp_path):
        store = PhotoStore(str(tmp_path / "b.db"), enable_wal=False)
        for index in range(6):
            store.upsert_photo(
                {
                    "path": f"/photos/b{index}.CR3",
                    "fname": f"b{index}.CR3",
                    "asset_pair_id": f"b{index}",
                    "asset_role": "raw",
                    "star": 1,
                    "blur_score": 88.0,
                }
            )
        service = ReviewService(store)
        service.select_all()
        written = service.apply_batch(star=5, flag="P")
        assert written == 6
        assert all(store.get_photo(f"/photos/b{i}.CR3")["label"] == "P" for i in range(6))
        assert service.undo()
        assert all(int(store.get_photo(f"/photos/b{i}.CR3")["star"]) == 1 for i in range(6))

    def test_batch_without_selection_is_noop(self, tmp_path):
        store = PhotoStore(str(tmp_path / "b2.db"), enable_wal=False)
        store.upsert_photo({"path": "/photos/z.CR3", "fname": "z.CR3", "asset_pair_id": "z", "star": 2})
        service = ReviewService(store)
        assert service.apply_batch(star=5) == 0
        assert int(store.get_photo("/photos/z.CR3")["star"]) == 2


class TestBatchBar:
    """批量条只负责收集意图；没选中时"作用于选中"的按钮必须全部置灰。"""

    def test_buttons_disabled_without_selection(self, qtbot):
        from ui.review.batch_bar import BatchBar

        bar = BatchBar()
        qtbot.addWidget(bar)
        bar.set_total_count(10)
        bar.set_selection_count(0)
        bar.set_rejected_count(0)
        assert not bar.flag_P_button.isEnabled()
        assert not bar.flag_X_button.isEnabled()
        assert not bar.star_button.isEnabled()
        assert not bar.cleanup_button.isEnabled(), "没有判废照片时清理不可用"
        assert bar.count_label.text() == "未选择"

    def test_buttons_enabled_with_selection(self, qtbot):
        from ui.review.batch_bar import BatchBar

        bar = BatchBar()
        qtbot.addWidget(bar)
        bar.set_selection_count(3)
        assert bar.flag_P_button.isEnabled()
        assert bar.star_button.isEnabled()
        assert bar.count_label.text() == "已选 3 张"

    def test_cleanup_enabled_by_rejected_not_by_selection(self, qtbot):
        """清理与选中无关 —— 只看有没有人工判废的照片。"""
        from ui.review.batch_bar import BatchBar

        bar = BatchBar()
        qtbot.addWidget(bar)
        bar.set_selection_count(0)
        bar.set_rejected_count(7)
        assert not bar.flag_P_button.isEnabled(), "没选中仍不能批量"
        assert bar.cleanup_button.isEnabled(), "有判废即可清理"
        assert bar.rejected_label.text() == "已判废 7 张"

    def test_signals_carry_intent(self, qtbot):
        from ui.review.batch_bar import BatchBar

        bar = BatchBar()
        qtbot.addWidget(bar)
        bar.set_selection_count(2)
        seen: list = []
        bar.flag_requested.connect(seen.append)
        bar.flag_X_button.click()
        assert seen == ["X"]
        stars: list = []
        bar.star_requested.connect(stars.append)
        bar.star_combo.setCurrentIndex(0)
        bar.star_button.click()
        assert stars == [0]


class TestRejectedScope:
    def test_only_manual_rejects_are_cleaned(self, tmp_path):
        """AI 判废但用户没看过的照片绝不能被清理。"""
        store = PhotoStore(str(tmp_path / "r.db"), enable_wal=False)
        rows = [
            ("/photos/manual.CR3", 0, "X", 0),      # 人工判废
            ("/photos/ai.CR3", 0, None, 1),         # AI 判废，未复核
            ("/photos/keep.CR3", 4, "P", 0),        # 保留
        ]
        for path, star, label, waste in rows:
            store.upsert_photo(
                {
                    "path": path,
                    "fname": Path(path).name,
                    "asset_pair_id": Path(path).stem,
                    "asset_role": "raw",
                    "star": star,
                    "label": label,
                    "is_waste": waste,
                    "blur_score": 88.0,
                }
            )
        service = ReviewService(store)
        paths = service.rejected_paths()
        assert paths == ["/photos/manual.CR3"], f"只应清理人工判废，实际 {paths}"
