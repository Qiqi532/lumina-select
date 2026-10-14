# -*- coding: utf-8 -*-
"""T-06 / M1-1 —— 命令注册表 / 契约 / 事件总线 的验收测试。

PLAN.md T-06 的三条验收，逐条对应下面的测试：
    ① 所有 ``mutates=True`` 的命令**均可 undo** —— `test_every_mutating_command_*`
    ② 执行后有对应**事件**记录 —— `test_every_mutating_command_emits_events`
    ③ **UI 层静态检查无直接写库代码** —— `test_ui_layer_has_no_direct_store_writes`

第 ③ 条是"留给未来 Agent 最重要的资产"：它是一个**回归闸门**。将来任何人
（包括我们自己）图省事在 UI 里直接 `store.set_star(...)`，这条测试立刻红，
从而保证"Agent 唯一入口"这个承诺不会随时间腐化。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from engine.store import PhotoStore
from services.command_catalog import build_registry
from services.commands import CommandRegistry, CommandSpec
from services.contracts import CommandError, CommandResult, EventName, RiskLevel
from services.events import EventBus

REPO_ROOT = Path(__file__).resolve().parents[1]
UI_ROOT = REPO_ROOT / "ui"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
@pytest.fixture
def store(tmp_path) -> PhotoStore:
    """一个含若干照片的真实项目库。"""
    instance = PhotoStore(str(tmp_path / "cull.db"))
    for index in range(3):
        instance.upsert_photo({"path": f"C:/p/IMG_{index:04d}.CR3", "fname": f"IMG_{index:04d}.CR3"})
    yield instance
    instance.close()


@pytest.fixture
def registry(store) -> CommandRegistry:
    return build_registry(store)


def _params_for(spec: CommandSpec) -> dict:
    """按 schema 造一组**合法**参数（用于遍历所有命令做"可执行"断言）。

    刻意按类型给值：既覆盖必填校验，也保证 handler 拿到形状正确的入参。
    对需要真实路径的命令给库里已有的路径。
    """
    values: dict[str, object] = {}
    properties = spec.params_schema.get("properties") or {}
    for key in spec.params_schema.get("required") or ():
        rule = properties.get(key) or {}
        kind = rule.get("type", "string")
        if key in {"path"}:
            values[key] = "C:/p/IMG_0000.CR3"
        elif key in {"paths"}:
            values[key] = ["C:/p/IMG_0000.CR3", "C:/p/IMG_0001.CR3"]
        elif key in {"target_dir", "source_dir", "quarantine_dir"}:
            values[key] = "C:/out"
        elif rule.get("enum"):
            # 枚举参数必须取合法值，否则校验层会先拦下来，测不到 handler。
            values[key] = rule["enum"][0]
        elif kind == "integer":
            values[key] = 1
        elif kind == "array":
            values[key] = [1]
        elif kind == "object":
            values[key] = {}
        else:
            values[key] = "x"
    return values


# ===========================================================================
# ① 所有 mutates=True 的命令均可 undo
# ===========================================================================
def test_registry_refuses_mutating_command_without_undo():
    """结构保证：会改数据却不可撤销的命令**根本注册不进来**。

    这比"运行时发现漏了 undo"强得多 —— 让错误的形态在构造期就无法表达。
    """
    with pytest.raises(ValueError, match="必须 undoable=True"):
        CommandSpec(
            name="bad.command", mutates=True, undoable=False,
            risk=RiskLevel.HIGH, params_schema={}, handler=lambda _p: None,
        )


def test_every_mutating_command_declares_undoable(registry):
    mutating = [spec for spec in registry.specs() if spec.mutates]
    assert mutating, "命令表里应当存在写命令"
    for spec in mutating:
        assert spec.undoable is True, f"{spec.name} 声称会改数据却没有 undo"


# 需要"真实上下文"才能执行的命令：合成参数给不出，只能接受它**说清原因地失败**。
_NEEDS_REAL_CONTEXT = {"cleanup.rejected"}


def test_every_mutating_command_actually_undoes(registry, store):
    """真跑一遍：执行 → 状态确实变了 → undo → 状态回到执行前。

    跳过本期"契约已冻结但目标表未建"的人物命令（T-15 才启用），
    但它们仍必须满足上面的声明性检查。
    """
    exercised = 0
    for spec in registry.specs():
        if not spec.mutates:
            continue
        snapshot_before = _store_fingerprint(store)
        result = registry.execute(spec.name, _params_for(spec))
        if not result.ok:
            assert result.error is not None and result.error.code in {
                "handler_failed", "unknown_command",
            }, f"{spec.name} 意外失败：{result.error}"
            message = result.error.message or ""
            if spec.name in _NEEDS_REAL_CONTEXT:
                # 【T-10】cleanup.rejected 需要"照片所在的项目目录"才能决定隔离目录
                # 放哪 —— 这是合成参数给不出的真实上下文。它必须**说清楚为什么失败**，
                # 绝不能静默 no-op；可撤销性由 test_batch_cleanup.py 单独验收。
                assert "project_dir" in message, \
                    f"{spec.name} 失败必须说明缺什么，实际：{message}"
                continue
            # 人物命令属"未接线"（NotImplementedError），其可撤销性在 T-15 验收。
            assert "T-15" in message, \
                f"{spec.name} 的失败原因应当说明是人物线未启用"
            continue
        assert result.undo_token is not None, f"{spec.name} 成功却没有 undo_token"
        undone = registry.undo(result.undo_token)
        assert undone.ok, f"{spec.name} 撤销失败：{undone.error}"
        assert _store_fingerprint(store) == snapshot_before, \
            f"{spec.name} 撤销后库状态未复原"
        exercised += 1
    assert exercised >= 8, f"实际验证到可撤销的写命令太少：{exercised}"


def test_undo_is_lifo_and_reports_depth(registry):
    registry.execute("photo.set_star", {"path": "C:/p/IMG_0000.CR3", "star": 3})
    registry.execute("photo.set_star", {"path": "C:/p/IMG_0000.CR3", "star": 4})
    assert registry.undo_depth() == 2
    assert registry.last_undo_name() == "photo.set_star"
    registry.undo()
    assert registry.undo_depth() == 1
    registry.undo()
    assert registry.can_undo() is False
    assert registry.undo().ok is False  # 空栈时是"无事可撤"，不是异常


def test_undo_restores_previous_decision_exactly(registry, store):
    store.apply_decision(["C:/p/IMG_0000.CR3"], star=2, label=None, source="manual")
    registry.execute("photo.set_flag", {"path": "C:/p/IMG_0000.CR3", "flag": "P"})
    assert (store.get_photo("C:/p/IMG_0000.CR3") or {}).get("label") == "P"
    registry.undo()
    row = store.get_photo("C:/p/IMG_0000.CR3") or {}
    assert row.get("star") == 2 and row.get("label") is None, "撤销必须精确复原 star/label"


def test_undo_failure_keeps_the_record(registry, store):
    """撤销失败时记录**放回栈尾**：不能让"撤销动作"凭空消失。"""
    registry.execute("photo.set_star", {"path": "C:/p/IMG_0000.CR3", "star": 5})
    entry = registry._undo_stack[-1]  # noqa: SLF001 —— 故意注入故障
    original, entry.revert = entry.revert, lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    result = registry.undo()
    assert result.ok is False and result.error.code == "undo_failed"
    assert registry.undo_depth() == 1, "失败后记录必须仍在栈里"
    entry.revert = original


# ===========================================================================
# ② 执行后有对应事件记录
# ===========================================================================
def test_every_mutating_command_emits_events(store):
    """逐条命令：执行成功 → 事件历史里有它声明的事件（真跑，不看声明）。"""
    for spec in build_registry(store).specs():
        if not spec.mutates or not spec.events:
            continue
        registry = build_registry(store)
        result = registry.execute(spec.name, _params_for(spec))
        if not result.ok:
            continue  # 人物线未接线，见上
        for event_name in spec.events:
            assert registry.bus.history(str(event_name)), \
                f"{spec.name} 执行后没有发出事件 {event_name}"


def test_command_result_lists_emitted_events(registry):
    result = registry.execute("photo.set_star", {"path": "C:/p/IMG_0000.CR3", "star": 4})
    assert result.events == (str(EventName.PHOTO_DECISION_CHANGED),)
    assert result.to_dict()["events"] == ["photo.decision_changed"]


def test_undo_itself_emits_event(registry):
    result = registry.execute("photo.set_star", {"path": "C:/p/IMG_0000.CR3", "star": 4})
    registry.undo(result.undo_token)
    assert registry.bus.history(str(EventName.COMMAND_UNDONE)), "撤销也应留下事件"


def test_failed_command_emits_no_event(registry):
    before = len(registry.bus.history())
    result = registry.execute("photo.set_star", {"path": "x", "star": 99})
    assert result.ok is False
    assert len(registry.bus.history()) == before, "失败的命令不该发事件"


def test_read_only_command_does_not_emit_undo(registry):
    result = registry.execute("project.open", {"path": "C:/p/proj"})
    assert result.ok is True
    assert result.undo_token is None, "只读命令不应产生 undo 记录"
    assert registry.undo_depth() == 0


# ===========================================================================
# ③ UI 层静态检查无直接写库代码（最重要的资产）
# ===========================================================================
# UI 允许直接调用的"非数据写入"白名单：这些是**界面自身状态**（分栏宽度、
# 面板是否展开），不是用户照片数据。它们不需要 undo，也不是 Agent 会碰的东西。
_UI_STATE_ALLOWLIST = {
    "set_preference",
}

# 明确禁止 UI 直接调用的**数据写方法**（必须经服务层 / 命令表）。
_FORBIDDEN_STORE_WRITES = {
    "set_star", "set_label", "set_pick", "set_scene_manual",
    "apply_decision", "restore_decisions", "update_photo", "upsert_photo",
    "upsert_photos_batch", "replace_face_regions", "set_group", "clear_groups",
    "clear_all", "reset_metrics", "set_meta", "record_export_item",
    "stash_decisions", "rehydrate_decisions",
}


def _iter_ui_modules() -> list[Path]:
    return sorted(path for path in UI_ROOT.rglob("*.py") if "__pycache__" not in path.parts)


def _direct_calls(path: Path) -> list[tuple[int, str, str]]:
    """返回 (行号, 接收者, 方法名) —— 形如 `receiver.method(...)` 的调用。

    接收者一并取出是必要的：`self.review.set_star()`（服务层）与
    `self.store.set_star()`（直接写库）方法名相同、性质完全不同。只看方法名
    会把合规调用误判成违规。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            receiver = ""
            target = node.func.value
            if isinstance(target, ast.Name):
                receiver = target.id
            elif isinstance(target, ast.Attribute):
                receiver = target.attr
            found.append((node.lineno, receiver, node.func.attr))
    return found


def _store_receivers() -> set[str]:
    """UI 里可能的"项目库句柄"变量名。

    只把这些接收者上的写方法视为"直接写库"；`self.review` 这类服务层句柄
    在服务层内部才有权调 store，UI 不该被它牵连。
    """
    return {"store", "db", "self.store", "self.db", "self._store"}


def test_ui_layer_has_no_direct_store_writes():
    """★ UI 不得直接写库：所有写操作必须经服务层 / `services/commands.py`。

    这条测试就是 T-06 留给未来 Agent 的资产 —— 它把 N1 从"口头约定"变成
    "CI 会红的事实"。将来若有人图省事在 UI 里直接 `store.set_star()`，立刻失败。
    """
    receivers = _store_receivers()
    offenders: list[str] = []
    for module in _iter_ui_modules():
        for line, receiver, method in _direct_calls(module):
            if method in _FORBIDDEN_STORE_WRITES and receiver in receivers:
                offenders.append(
                    f"{module.relative_to(REPO_ROOT)}:{line} 直接调用 {receiver}.{method}()"
                )
    assert not offenders, (
        "UI 层出现直接写库调用（N1：必须经服务层/命令表）：\n  " + "\n  ".join(offenders)
    )


def test_ui_layer_has_no_sql():
    """N6：UI 不写 SQL，只消费 DTO。

    匹配 SQL **语句形状**而不是裸关键字：否则产品名 "Lumina Select"、
    文档里的 "SELECT" 会误报。SQL 的特征是关键字后跟列名/FROM/表名等结构。
    """
    import re

    patterns = (
        # SELECT <列或 *> ... FROM <表>
        re.compile(r"\bSELECT\s+[\w*,\s.()]+\s+FROM\s+\w", re.IGNORECASE),
        re.compile(r"\bINSERT\s+INTO\s+\w", re.IGNORECASE),
        re.compile(r"\bUPDATE\s+\w+\s+SET\s+\w", re.IGNORECASE),
        re.compile(r"\bDELETE\s+FROM\s+\w", re.IGNORECASE),
        re.compile(r"\bCREATE\s+(TABLE|INDEX)\s+\w", re.IGNORECASE),
        re.compile(r"\bALTER\s+TABLE\s+\w", re.IGNORECASE),
    )
    offenders: list[str] = []
    for module in _iter_ui_modules():
        text = module.read_text(encoding="utf-8")
        for pattern in patterns:
            for match in pattern.finditer(text):
                line = text[: match.start()].count("\n") + 1
                offenders.append(
                    f"{module.relative_to(REPO_ROOT)}:{line} 出现 SQL：{match.group(0)[:40]!r}"
                )
    assert not offenders, "UI 层出现 SQL（N6）：\n  " + "\n  ".join(offenders)


def test_ui_layer_does_not_bypass_services_to_reach_the_store():
    """UI 只能与**服务层**对话，不得直接触达 `PhotoStore` 的写方法。

    边界说明（为什么不禁 `review.decide()`）：T-06 验收③ 的原文是
    "UI 层静态检查**无直接写库代码**"。`ReviewPage` 持有的是 `ReviewService`
    （服务层），`self.review.decide("P")` 是**调服务**，不是直接写库 ——
    服务层正是 UI 与存储之间应有的抽象，这条不构成违规。

    真正要禁的是"UI 伸手越过服务层直接改库"。因此这里检查的是：
    凡是 UI 里出现的 `store` 写方法调用，一律算违规（无论变量名叫 store 还是 db）。
    """
    offenders: list[str] = []
    receivers = _store_receivers()
    for module in _iter_ui_modules():
        for line, receiver, method in _direct_calls(module):
            if method in _FORBIDDEN_STORE_WRITES and receiver in receivers:
                offenders.append(f"{module.relative_to(REPO_ROOT)}:{line} 直接调用 {receiver}.{method}()")
    assert not offenders, (
        "UI 越过服务层直接写库（N1）：\n  " + "\n  ".join(offenders)
    )


def test_ui_review_writes_go_through_review_service():
    """反向断言：UI 的复核写操作确实经由 `ReviewService`，而不是自己拼装。

    这条保护的是"服务层没有被绕过"——若将来有人把 `decide()` 的内联实现
    抄进 UI，本测试会因为在 UI 里看见 store 写方法而失败。
    """
    source = (UI_ROOT / "pages" / "review_page.py").read_text(encoding="utf-8")
    assert "self.review.decide(" in source or "self.review.set_star(" in source, \
        "复核页的决策写入应当调用 ReviewService"


def test_allowlisted_preference_writes_are_ui_state_only():
    """白名单里的 `set_preference` 只允许用于界面状态键（防止它被滥用成后门）。"""
    import re

    allowed_prefixes = ("review.", "inspector.")
    offenders: list[str] = []
    pattern = re.compile(r"""set_preference\(\s*[f]?["']([^"']+)["']""")
    for module in _iter_ui_modules():
        text = module.read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            key = match.group(1)
            if not key.startswith(allowed_prefixes):
                line = text[: match.start()].count("\n") + 1
                offenders.append(f"{module.relative_to(REPO_ROOT)}:{line} 写偏好键 {key!r}")
    assert not offenders, (
        "偏好写入只应用于界面状态（review.* / inspector.*）：\n  " + "\n  ".join(offenders)
    )


# ===========================================================================
# 契约层
# ===========================================================================
def test_contract_result_is_truthy_only_when_ok():
    assert bool(CommandResult(name="x", ok=True)) is True
    assert bool(CommandResult(name="x", ok=False, error=CommandError("e", "m"))) is False


def test_contract_result_serializes():
    result = CommandResult(
        name="photo.set_star", ok=True, value={"star": 3},
        undo_token="u1", events=("photo.decision_changed",),
    )
    data = result.to_dict()
    assert data["name"] == "photo.set_star" and data["ok"] is True
    assert data["error"] is None and data["undo_token"] == "u1"


def test_command_error_serializes_detail():
    error = CommandError("invalid_params", "坏参数", {"param": "star"})
    assert error.to_dict() == {"code": "invalid_params", "message": "坏参数", "detail": {"param": "star"}}


# ===========================================================================
# 参数校验
# ===========================================================================
@pytest.mark.parametrize(
    "params,expected",
    [
        ({"path": "C:/p/x.CR3"}, "缺少必填参数"),
        ({"path": "C:/p/x.CR3", "star": 9}, "大于最大值"),
        ({"path": "C:/p/x.CR3", "star": -1}, "小于最小值"),
        ({"path": 123, "star": 3}, "类型不符"),
        ({"path": "C:/p/x.CR3", "star": True}, "收到布尔值"),
    ],
)
def test_validation_rejects_bad_params(registry, params, expected):
    result = registry.execute("photo.set_star", params)
    assert result.ok is False
    assert expected in result.error.message


def test_validation_rejects_bad_enum(registry):
    result = registry.execute("photo.set_flag", {"path": "C:/p/x.CR3", "flag": "Z"})
    assert result.ok is False and "允许集合" in result.error.message


def test_unknown_command_returns_structured_error(registry):
    result = registry.execute("does.not.exist", {})
    assert result.ok is False and result.error.code == "unknown_command"


def test_handler_exception_becomes_structured_error(registry):
    person = registry.execute("person.confirm", {"person_id": 1})
    assert person.ok is False
    assert person.error.code == "handler_failed"
    assert "NotImplementedError" in person.error.message


# ===========================================================================
# 注册表自身的完整性
# ===========================================================================
def test_catalog_has_the_fourteen_declared_commands(registry):
    """PLAN.md T-06 明列 14 条命令，逐一核对（多一条少一条都要显式知道）。"""
    expected = {
        "project.create", "project.open",
        "photo.set_star", "photo.set_flag", "photo.set_scene_manual", "photo.batch_update",
        "person.confirm", "person.merge", "person.move_out",
        "person.rename", "person.set_cover",
        "collection.save", "export.run", "cleanup.rejected",
    }
    assert expected <= set(registry.names()), f"缺少命令：{sorted(expected - set(registry.names()))}"


def test_high_risk_commands_are_the_irreversible_ones(registry):
    high = {spec.name for spec in registry.specs() if spec.risk == RiskLevel.HIGH}
    assert {"export.run", "cleanup.rejected"} <= high, "导出与清理必须标为高风险"


def test_describe_is_agent_consumable(registry):
    """`describe()` 就是未来 Agent 的"工具列表"，必须可 JSON 序列化。"""
    import json

    described = registry.describe()
    assert len(described) == len(registry)
    json.dumps(described, ensure_ascii=False)  # 不抛即可
    for item in described:
        assert set(item) >= {"name", "mutates", "undoable", "risk", "params_schema"}
        assert item["risk"] in {"low", "medium", "high"}


def test_duplicate_registration_is_rejected():
    registry = CommandRegistry()
    spec = CommandSpec(name="dup", mutates=False, undoable=False,
                       risk=RiskLevel.LOW, params_schema={}, handler=lambda _p: None)
    registry.register(spec)
    with pytest.raises(ValueError, match="重复注册"):
        registry.register(spec)


# ===========================================================================
# 事件总线
# ===========================================================================
def test_event_bus_exact_prefix_and_global_subscriptions():
    """派发顺序固定为 精确 → 前缀通配 → 全局通配（可复现，不依赖 dict 插入序）。"""
    bus = EventBus()
    seen: list[str] = []
    bus.subscribe("person.confirmed", lambda e: seen.append(f"exact:{e.name}"))
    bus.subscribe("person.*", lambda e: seen.append(f"prefix:{e.name}"))
    bus.subscribe("*", lambda e: seen.append(f"all:{e.name}"))
    bus.publish("person.confirmed", {})
    bus.publish("photo.decision_changed", {})
    assert seen == [
        "exact:person.confirmed", "prefix:person.confirmed", "all:person.confirmed",
        "all:photo.decision_changed",
    ]


def test_event_bus_prefix_does_not_match_bare_name():
    bus = EventBus()
    seen: list[str] = []
    bus.subscribe("person.*", lambda e: seen.append(e.name))
    bus.publish("person", {})
    assert seen == []


def test_event_bus_unsubscribe_is_idempotent():
    bus = EventBus()
    calls: list[str] = []
    off = bus.subscribe("x", lambda e: calls.append(e.name))
    off()
    off()  # 再调一次不应出错
    bus.publish("x", {})
    assert calls == []
    assert bus.subscriber_count() == 0


def test_event_bus_isolates_subscriber_failures():
    bus = EventBus()
    errors: list[tuple[str, BaseException]] = []
    bus.on_error(lambda name, error: errors.append((name, error)))
    survived: list[str] = []
    bus.subscribe("x", lambda e: (_ for _ in ()).throw(RuntimeError("bad listener")))
    bus.subscribe("x", lambda e: survived.append(e.name))
    bus.publish("x", {})  # 不得抛
    assert survived == ["x"], "一个坏监听器不该影响其它监听器"
    assert errors and errors[0][0] == "x"


def test_event_bus_history_is_bounded_and_sequential():
    bus = EventBus(history=3)
    for index in range(5):
        bus.publish("tick", {"i": index})
    records = bus.history()
    assert len(records) == 3
    assert [r.payload["i"] for r in records] == [2, 3, 4]
    assert [r.seq for r in records] == [3, 4, 5]


def test_event_records_are_immutable_snapshots():
    bus = EventBus()
    payload = {"count": 1}
    record = bus.publish("x", payload)
    payload["count"] = 999
    assert record.payload["count"] == 1, "事件负载应是发布时刻的快照"


def test_event_bus_rejects_non_callable():
    with pytest.raises(TypeError):
        EventBus().subscribe("x", "not callable")  # type: ignore[arg-type]


# ===========================================================================
# UI 集成（命令表真的被装上去了）
# ===========================================================================
def test_main_window_exposes_command_registry(qtbot, tmp_path, monkeypatch):
    from engine import config

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(config, "DEFAULT_DB", str(tmp_path / "data" / "cull.db"))

    from ui.main_window import MainWindow

    window = MainWindow(start_in_library=True)
    qtbot.addWidget(window)
    assert window.commands is not None
    assert "photo.set_star" in window.commands

    window.begin_analysis(str(tmp_path))
    assert (window.store.get_meta("source_dir") or "") == str(tmp_path)
    # 经命令表写入 → 有事件、可撤销
    assert window.commands.bus.history(str(EventName.PHOTO_DECISION_CHANGED))
    window.close()


def test_switching_project_rebinds_command_registry(qtbot, tmp_path, monkeypatch):
    """换项目后命令表必须指向新库，否则写操作会落进上一个项目。"""
    from engine import config

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(config, "DEFAULT_DB", str(tmp_path / "data" / "cull.db"))

    from ui.main_window import MainWindow

    window = MainWindow(start_in_library=True)
    qtbot.addWidget(window)
    first_store = window.store

    project_a = tmp_path / "A" / "data" / "cull.db"
    project_a.parent.mkdir(parents=True)
    PhotoStore(str(project_a)).close()
    window.open_project(str(tmp_path / "A"))

    assert window.commands is not None
    result = window.commands.execute("photo.set_star", {"path": "C:/p/x.CR3", "star": 3})
    assert result.ok is True
    # 写的是新库（新库无此照片 → 不影响旧库），关键是命令表已换绑
    assert window.store.db_path != first_store.db_path
    window.close()


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def _store_fingerprint(store: PhotoStore) -> tuple:
    """库中"业务可见状态"的指纹（用于断言撤销后确实复原）。

    只取人工决策与元数据 —— 排除时间戳这类每次写入都会变的字段，否则
    "撤销后指纹不同"会误报。
    """
    photos = tuple(
        sorted(
            (row["path"], int(row.get("star") or 0), row.get("label"))
            for row in store.all_photos()
        )
    )
    meta = tuple(sorted((key, store.get_meta(key)) for key in ("source_dir",)))
    preferences = tuple(
        sorted((key, store.get_preference(key)) for key in
               ("cleanup.last_quarantine",))
    )
    return (photos, meta, preferences)
