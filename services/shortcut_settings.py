"""快捷键绑定模型与持久化（T-11）—— 无 Qt 依赖。

设计要点
--------
1. **命令是主键，按键是值**。用户改的是"P 这个动作绑到哪个键"，不是
   "这个键干什么"。以命令为主键才能保证"新增命令 → 自动拿到默认键"，
   以及"改键后重启仍在"。
2. **冲突必须显式报错**。同一个键绑两个命令是最容易让人抓狂的 bug：
   按下去有时出这个有时出那个（取决于注册顺序）。这里在保存前拦掉，
   并指出是跟哪条命令撞了。
3. **reserved 键不可占用**。`P`/`X`/`0`–`5` 这类裸键是选片的生命线，
   允许把"全屏"绑到 P 会让核心动作失效 —— 不是不允许，而是要看清楚代价，
   所以由调用方决定是否放行（UI 会给二次确认）。
"""
from __future__ import annotations

import json
from collections.abc import Mapping

from engine.user_settings import UserSettings

SETTINGS_KEY = "shortcuts.bindings"

# ---------------------------------------------------------------------------
# 命令表：命令 -> (默认按键, 中文标签, 说明)
# ---------------------------------------------------------------------------
# 约定：现有 COMMAND_KEYS 全部保留（PLAN T-11 明确要求），只在后面追加新键。
COMMAND_TABLE: tuple[tuple[str, str, str, str], ...] = (
    # ---- 既有（v0.6 起就在用，不动） ----
    ("pick", "P", "保留", "标记为保留并记 5 星"),
    ("reject", "X", "排除", "标记为排除并记 0 星"),
    ("star-0", "0", "0 星", "取消保留"),
    ("star-1", "1", "1 星", ""),
    ("star-2", "2", "2 星", ""),
    ("star-3", "3", "3 星", ""),
    ("star-4", "4", "4 星", ""),
    ("star-5", "5", "5 星", ""),
    ("candidate-a", "A", "候选 A", "跳到本组第 1 张候选"),
    ("candidate-b", "B", "候选 B", "跳到本组第 2 张候选"),
    ("candidate-c", "C", "候选 C", "跳到本组第 3 张候选"),
    ("candidate-d", "D", "候选 D", "跳到本组第 4 张候选"),
    ("previous", "Left", "上一张", ""),
    ("next", "Right", "下一张", ""),
    ("toggle-view", "Tab", "切换视图", "单张 / 比较"),
    ("fullscreen", "F", "全屏", ""),
    ("toggle-inspector", "I", "技术面板", "显示或隐藏"),
    ("undo", "Ctrl+Z", "撤销", ""),
    # ---- T-11 新增 ----
    ("search", "Ctrl+F", "搜索", "按文件名/路径过滤"),
    ("command-palette", "Ctrl+K", "命令面板", "未来 Agent 入口"),
    ("confirm-best", "Ctrl+Return", "确认最佳帧", "确认当前选择并进入下一组"),
    ("export", "Ctrl+E", "导出", "打开导出页"),
    ("save-collection", "Ctrl+B", "存为集合", "把当前筛选存成集合"),
    ("zoom-preview", "Space", "放大查看", "放大当前照片"),
    ("goto-face-group", "Ctrl+Shift+N", "跳到人脸组", "定位到该人脸所在分组"),
    ("assign-person", "T", "归属人物", "批量归属到某个人物"),
)

DEFAULT_BINDINGS: dict[str, str] = {
    command: key for command, key, _label, _note in COMMAND_TABLE
}
COMMAND_LABELS: dict[str, str] = {
    command: label for command, _key, label, _note in COMMAND_TABLE
}
COMMAND_NOTES: dict[str, str] = {
    command: note for command, _key, _label, note in COMMAND_TABLE
}

# 选片的核心裸键：改它们要谨慎（UI 给二次确认，但不禁止）。
RESERVED_KEYS = frozenset({"P", "X", "0", "1", "2", "3", "4", "5"})


class ShortcutConflict(ValueError):
    """按键冲突。``message`` 里会写清跟哪条命令撞了。"""


def normalize_key(key: str) -> str:
    """把按键序列规范成稳定形式，避免 ``ctrl+z`` 与 ``Ctrl+Z`` 被当成两个键。"""
    if not key:
        return ""
    parts = [part.strip() for part in str(key).split("+") if part.strip()]
    if not parts:
        return ""
    normalized: list[str] = []
    for part in parts:
        low = part.lower()
        if low in {"ctrl", "control"}:
            normalized.append("Ctrl")
        elif low in {"shift"}:
            normalized.append("Shift")
        elif low in {"alt", "meta"}:
            normalized.append("Alt")
        elif len(part) == 1:
            normalized.append(part.upper())
        else:
            normalized.append(part.capitalize())
    # 修饰键顺序固定：Ctrl → Alt → Shift → 主键
    order = {"Ctrl": 0, "Alt": 1, "Shift": 2}
    modifiers = sorted(
        (p for p in normalized[:-1]), key=lambda p: order.get(p, 3)
    )
    return "+".join([*modifiers, normalized[-1]])


def load_bindings(settings: UserSettings) -> dict[str, str]:
    """读绑定；缺失/损坏一律回落到默认值，并在结果里补上新增命令的默认键。"""
    raw = settings.get(SETTINGS_KEY)
    saved: Mapping[str, str] = {}
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                saved = {str(k): str(v) for k, v in parsed.items()}
        except (ValueError, TypeError):
            saved = {}
    merged = dict(DEFAULT_BINDINGS)
    for command, key in saved.items():
        # 只接受表里存在的命令 —— 否则删掉的命令会变成幽灵绑定。
        if command in DEFAULT_BINDINGS:
            normalized = normalize_key(key)
            if normalized:
                merged[command] = normalized
    return merged


def save_bindings(settings: UserSettings, bindings: Mapping[str, str]) -> dict[str, str]:
    """校验并保存。冲突抛 :class:`ShortcutConflict`，不写库。"""
    cleaned = {str(k): normalize_key(v) for k, v in bindings.items() if normalize_key(v)}
    conflicts = _find_conflicts(cleaned)
    if conflicts:
        command, other = conflicts[0]
        raise ShortcutConflict(
            f"{COMMAND_LABELS.get(command, command)} 与 "
            f"{COMMAND_LABELS.get(other, other)} 撞用了同一个按键 "
            f"「{cleaned[command]}」"
        )
    merged = dict(DEFAULT_BINDINGS)
    merged.update(cleaned)
    settings.set(SETTINGS_KEY, json.dumps(merged, ensure_ascii=False))
    return merged


def reset_bindings(settings: UserSettings) -> dict[str, str]:
    settings.delete(SETTINGS_KEY)
    return dict(DEFAULT_BINDINGS)


def _find_conflicts(bindings: Mapping[str, str]) -> list[tuple[str, str]]:
    """找出重复按键，返回 ``[(命令A, 命令B), ...]``。"""
    seen: dict[str, str] = {}
    conflicts: list[tuple[str, str]] = []
    for command in DEFAULT_BINDINGS:  # 固定顺序，报错可复现
        key = bindings.get(command)
        if not key:
            continue
        if key in seen:
            conflicts.append((command, seen[key]))
        else:
            seen[key] = command
    return conflicts


def occupied_by(bindings: Mapping[str, str], key: str) -> str | None:
    """查询某个键当前被哪条命令占用（无则 ``None``）。"""
    normalized = normalize_key(key)
    for command in DEFAULT_BINDINGS:
        if bindings.get(command) == normalized:
            return command
    return None


__all__ = [
    "COMMAND_LABELS",
    "COMMAND_NOTES",
    "COMMAND_TABLE",
    "DEFAULT_BINDINGS",
    "RESERVED_KEYS",
    "SETTINGS_KEY",
    "ShortcutConflict",
    "load_bindings",
    "normalize_key",
    "occupied_by",
    "reset_bindings",
    "save_bindings",
]
