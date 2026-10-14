"""T-11 快捷键重绑定 + 工具状态记忆。

验收对照（PLAN.md T-11）：
    · 改键后跨重启生效
    · 当前集合/排序/缩放/视图模式被记住
另加两条红线式检查：
    · 既有 COMMAND_KEYS 必须原样保留（不能改坏肌肉记忆）
    · 尚未接线的命令**必须说清原因**，绝不静默 no-op
"""
from __future__ import annotations

import pytest

from engine.user_settings import UserSettings
from services.shortcut_settings import (
    COMMAND_LABELS,
    COMMAND_TABLE,
    DEFAULT_BINDINGS,
    ShortcutConflict,
    load_bindings,
    normalize_key,
    occupied_by,
    reset_bindings,
    save_bindings,
)
from ui.review.shortcuts import COMMAND_KEYS, install_review_shortcuts

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ---------------------------------------------------------------------------
# 默认键位与向后兼容
# ---------------------------------------------------------------------------
class TestLegacyKeysPreserved:
    def test_command_keys_still_has_the_original_eighteen(self):
        expected = {
            "P", "X", "0", "1", "2", "3", "4", "5", "A", "B", "C", "D",
            "Left", "Right", "Tab", "F", "I", "Ctrl+Z",
        }
        assert set(COMMAND_KEYS) == expected, "既有键位一个都不能少、不能改"

    def test_default_bindings_include_new_commands(self):
        for command in ("search", "command-palette", "confirm-best", "export",
                        "save-collection", "zoom-preview", "goto-face-group",
                        "assign-person"):
            assert command in DEFAULT_BINDINGS
            assert DEFAULT_BINDINGS[command]

    def test_every_command_has_a_label(self):
        for command, _key, _label, _note in COMMAND_TABLE:
            assert COMMAND_LABELS.get(command), f"{command} 缺中文标签"

    def test_install_without_bindings_keeps_legacy_behaviour(self, qtbot):
        from PySide6.QtWidgets import QWidget

        host = QWidget()
        qtbot.addWidget(host)
        calls = []
        actions = install_review_shortcuts(host, lambda c: calls.append(c))
        assert set(actions) == set(COMMAND_KEYS)

    def test_install_with_bindings_uses_custom_keys(self, qtbot):
        from PySide6.QtWidgets import QWidget

        host = QWidget()
        qtbot.addWidget(host)
        calls = []
        bindings = dict(DEFAULT_BINDINGS)
        bindings["pick"] = "W"
        actions = install_review_shortcuts(host, lambda c: calls.append(c), bindings=bindings)
        assert "W" in actions
        actions["W"].trigger()
        assert calls[-1] == "pick"


# ---------------------------------------------------------------------------
# 归一化与冲突
# ---------------------------------------------------------------------------
class TestNormalization:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("ctrl+z", "Ctrl+Z"),
            ("CTRL+SHIFT+N", "Ctrl+Shift+N"),
            ("  p  ", "P"),
            ("Shift+Ctrl+K", "Ctrl+Shift+K"),
            ("", ""),
        ],
    )
    def test_normalize_key(self, raw, expected):
        assert normalize_key(raw) == expected


class TestConflict:
    def test_duplicate_key_is_rejected_with_named_command(self, tmp_path):
        settings = UserSettings(str(tmp_path / "s.db"))
        bindings = dict(DEFAULT_BINDINGS)
        bindings["fullscreen"] = "P"  # 和 pick 撞
        with pytest.raises(ShortcutConflict) as info:
            save_bindings(settings, bindings)
        message = str(info.value)
        assert "保留" in message and "全屏" in message, f"报错要点名是哪两条命令：{message}"
        assert "P" in message

    def test_rejected_write_does_not_persist(self, tmp_path):
        settings = UserSettings(str(tmp_path / "s.db"))
        bindings = dict(DEFAULT_BINDINGS)
        bindings["fullscreen"] = "P"
        with pytest.raises(ShortcutConflict):
            save_bindings(settings, bindings)
        assert settings.get("shortcuts.bindings") is None, "冲突时不能写库"

    def test_occupied_by_reports_owner(self):
        assert occupied_by(DEFAULT_BINDINGS, "P") == "pick"
        assert occupied_by(DEFAULT_BINDINGS, "Ctrl+F") == "search"
        assert occupied_by(DEFAULT_BINDINGS, "Ctrl+Alt+Q") is None


# ---------------------------------------------------------------------------
# 持久化（跨重启）
# ---------------------------------------------------------------------------
class TestPersistence:
    def test_bindings_survive_reopen(self, tmp_path):
        """验收：改键后跨重启生效。"""
        path = str(tmp_path / "s.db")
        settings = UserSettings(path)
        bindings = dict(DEFAULT_BINDINGS)
        bindings["reject"] = "Ctrl+Backspace"
        save_bindings(settings, bindings)
        settings.close()

        reopened = UserSettings(path)
        try:
            restored = load_bindings(reopened)
            assert restored["reject"] == "Ctrl+Backspace", "改过的键必须还在"
            assert restored["pick"] == "P", "没改过的键保持默认"
        finally:
            reopened.close()

    def test_new_commands_get_defaults_after_upgrade(self, tmp_path):
        """旧版本存的绑定里没有新命令 —— 必须自动补上默认键，不能空着。"""
        path = str(tmp_path / "s.db")
        settings = UserSettings(path)
        settings.set("shortcuts.bindings", '{"pick": "Q"}')
        settings.close()
        reopened = UserSettings(path)
        try:
            restored = load_bindings(reopened)
            assert restored["pick"] == "Q"
            assert restored["search"] == "Ctrl+F", "新增命令应自动拿到默认键"
        finally:
            reopened.close()

    def test_unknown_commands_are_dropped(self, tmp_path):
        path = str(tmp_path / "s.db")
        settings = UserSettings(path)
        settings.set("shortcuts.bindings", '{"ghost.command": "G"}')
        settings.close()
        reopened = UserSettings(path)
        try:
            restored = load_bindings(reopened)
            assert "ghost.command" not in restored
        finally:
            reopened.close()

    def test_corrupt_payload_falls_back_to_defaults(self, tmp_path):
        path = str(tmp_path / "s.db")
        settings = UserSettings(path)
        settings.set("shortcuts.bindings", "{not json")
        settings.close()
        reopened = UserSettings(path)
        try:
            assert load_bindings(reopened) == DEFAULT_BINDINGS
        finally:
            reopened.close()

    def test_reset_restores_defaults(self, tmp_path):
        settings = UserSettings(str(tmp_path / "s.db"))
        save_bindings(settings, {"pick": "Q"})
        assert reset_bindings(settings) == DEFAULT_BINDINGS
        assert load_bindings(settings) == DEFAULT_BINDINGS


# ---------------------------------------------------------------------------
# 设置对话框
# ---------------------------------------------------------------------------
class TestSettingsDialog:
    def test_dialog_lists_every_command(self, qtbot):
        from ui.settings import SettingsDialog

        dialog = SettingsDialog(DEFAULT_BINDINGS)
        qtbot.addWidget(dialog)
        assert dialog.table.rowCount() == len(COMMAND_TABLE)
        assert dialog.bindings()["pick"] == "P"

    def test_editor_captures_and_clears_conflicting_owner(self, qtbot):
        """把一个已占用的键夺过来时，原占用方的键必须被清空（一键不能两用）。"""
        from PySide6.QtGui import QKeySequence
        from ui.settings import ShortcutEditor

        bindings = dict(DEFAULT_BINDINGS)
        editor = ShortcutEditor("fullscreen", "F", bindings)
        qtbot.addWidget(editor)
        editor.editor.setKeySequence(QKeySequence("P"))
        editor.accept()
        result = editor.result_bindings()
        assert result["fullscreen"] == "P"
        assert result["pick"] == "", "被夺走的命令必须清空，不能留着一键两用"


# ---------------------------------------------------------------------------
# 工具状态记忆
# ---------------------------------------------------------------------------
class TestToolStateMemory:
    def _store(self, tmp_path):
        from engine.store import PhotoStore

        store = PhotoStore(str(tmp_path / "state.db"), enable_wal=False)
        for index in range(3):
            store.upsert_photo(
                {
                    "path": f"/photos/t{index}.CR3",
                    "fname": f"t{index}.CR3",
                    "asset_pair_id": f"t{index}",
                    "asset_role": "raw",
                    "star": 0,
                    "blur_score": 88.0,
                }
            )
        return store

    def test_filter_and_threshold_survive_reopen(self, tmp_path):
        """验收：当前口径 / 星级阈值跨重启被记住。"""
        from engine.store import PhotoStore
        from services.review_service import ReviewFilter, ReviewService

        db_path = str(tmp_path / "state.db")
        store = self._store(tmp_path)
        service = ReviewService(store)
        service.set_filter(ReviewFilter.REJECTED)
        service.set_star_threshold(3)
        store.close()

        reopened = PhotoStore(db_path, enable_wal=False)
        try:
            restored = ReviewService(reopened)
            assert restored.active_filter == ReviewFilter.REJECTED
            assert restored.star_filter == 3
        finally:
            reopened.close()

    def test_illegal_saved_filter_is_ignored(self, tmp_path):
        """旧版本存过的不合法口径不能让程序卡死在一个不存在的视图上。"""
        from engine.store import PhotoStore
        from services.review_service import ReviewFilter, ReviewService

        store = self._store(tmp_path)
        store.set_preference("review.filter", "no-such-filter")
        service = ReviewService(store)
        assert service.active_filter == ReviewFilter.ALL
        store.close()


# ---------------------------------------------------------------------------
# 未接线命令必须说清原因
# ---------------------------------------------------------------------------
class TestPendingCommandsExplainThemselves:
    def test_review_page_reports_reason_instead_of_noop(self, qtbot, tmp_path):
        from engine.store import PhotoStore
        from ui.pages.review_page import ReviewPage

        store = PhotoStore(str(tmp_path / "p.db"), enable_wal=False)
        try:
            page = ReviewPage(store, bindings=DEFAULT_BINDINGS)
            qtbot.addWidget(page)
            notices = []
            page.notice_requested.connect(notices.append)
            for command in ("search", "command-palette", "save-collection",
                            "goto-face-group", "assign-person"):
                page._command(command)
                assert notices, f"{command} 静默 no-op 了"
                assert notices[-1], f"{command} 的提示不能是空的"
            assert all("T-" in message for message in notices), \
                f"提示必须说清在哪一期启用：{notices}"
        finally:
            store.close()

    def test_export_shortcut_navigates(self, qtbot, tmp_path):
        from engine.store import PhotoStore
        from ui.pages.review_page import ReviewPage

        store = PhotoStore(str(tmp_path / "p.db"), enable_wal=False)
        try:
            page = ReviewPage(store, bindings=DEFAULT_BINDINGS)
            qtbot.addWidget(page)
            targets = []
            page.navigate_requested.connect(targets.append)
            page._command("export")
            assert targets == ["export"]
        finally:
            store.close()
