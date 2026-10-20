"""Asset-level culling state, navigation, filtering and undo without Qt."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping

from engine import config
from engine.store import PhotoStore


class ReviewFilter(StrEnum):
    ALL = "all"
    AI_PICKS = "ai-picks"
    UNCERTAIN = "uncertain"
    REJECTED = "rejected"
    TECHNICAL_ISSUE = "technical-issue"
    STAR = "star"


class ViewMode(StrEnum):
    SINGLE = "single"
    COMPARE = "compare"


@dataclass(frozen=True, slots=True)
class ReviewItem:
    asset_id: str
    paths: tuple[str, ...]
    preview_path: str
    group_id: int | None
    candidate_rank: int | None
    star: int
    label: str | None
    is_best: bool
    is_uncertain: bool
    is_rejected: bool
    has_technical_issue: bool


@dataclass(frozen=True, slots=True)
class ReviewGroup:
    group_id: int
    item_ids: tuple[str, ...]
    is_uncertain: bool


@dataclass(frozen=True, slots=True)
class DecisionSnapshot:
    asset_id: str
    before: tuple[dict, ...]


@dataclass(frozen=True, slots=True)
class BatchSnapshot:
    """【T-10】一次批量操作的撤销点。

    与 ``DecisionSnapshot`` 的区别只是"影响多个 asset"——撤销时无法也不必
    聚焦到某一张，回到批量前的状态即可。
    """

    asset_ids: tuple[str, ...]
    before: tuple[dict, ...]


@dataclass(frozen=True, slots=True)
class SceneSnapshot:
    asset_id: str
    before: tuple[tuple[str, str | None], ...]


class ReviewService:
    MAX_UNDO = 200

    def __init__(self, store: PhotoStore, *, star_filter: int = 4) -> None:
        self.store = store
        self.star_filter = star_filter
        self.active_filter = ReviewFilter.ALL
        self.filter_star: int | None = None
        # 【T-11】工具状态记忆：口径与星级阈值跨重启保留。
        # 只认**合法值** —— 旧版本存过的值若已不在枚举里就当没存过，
        # 否则会把库升级后的第一次启动直接卡在一个不存在的口径上。
        saved_filter = store.get_preference("review.filter")
        if saved_filter in ReviewFilter._value2member_map_:
            self.active_filter = ReviewFilter(saved_filter)
        saved_threshold = store.get_preference("review.star_threshold")
        if saved_threshold is not None and saved_threshold.isdigit():
            self.star_filter = int(saved_threshold)
        # 【T-08】分面筛选下推的范围（照片路径集合）；None = 不限定。
        self._scope_paths: set[str] | None = None
        self.items: dict[str, ReviewItem] = {}
        self.groups: dict[int, ReviewGroup] = {}
        self.photo_rows: dict[str, dict] = {}
        self.face_rows: dict[str, list[dict]] = {}
        self.queue: list[ReviewItem] = []
        self.cursor = 0
        # 【T-10】多选是"选中了哪些 asset"（不是路径）—— 一个 asset 可能含
        # RAW+JPEG 两张，用户点一下缩略图就应该整组一起动。
        self.selection: list[str] = []
        self._anchor: str | None = None
        self._undo_stack: list[DecisionSnapshot | BatchSnapshot | SceneSnapshot] = []
        saved_mode = store.get_preference("review.view_mode")
        self._preferred_mode = ViewMode(saved_mode) if saved_mode in ViewMode._value2member_map_ else None
        self.reload()

    @property
    def current(self) -> ReviewItem | None:
        return self.queue[self.cursor] if self.queue else None

    @property
    def view_mode(self) -> ViewMode:
        if self._preferred_mode is not None:
            return self._preferred_mode
        item = self.current
        if item is not None and item.group_id in self.groups:
            group = self.groups[item.group_id]
            if group.is_uncertain or len(group.item_ids) > 1:
                return ViewMode.COMPARE
        return ViewMode.SINGLE

    def toggle_view_mode(self) -> ViewMode:
        next_mode = ViewMode.COMPARE if self.view_mode == ViewMode.SINGLE else ViewMode.SINGLE
        self._preferred_mode = next_mode
        self.store.set_preference("review.view_mode", next_mode.value)
        return next_mode

    @staticmethod
    def _item(asset_id: str, rows: list[dict]) -> ReviewItem:
        preview = next(
            (row["path"] for row in rows if row.get("asset_role") == "jpeg"),
            rows[0]["path"],
        )
        group = next((row.get("group_id") for row in rows if row.get("group_id") is not None), None)
        ranks = [int(row["candidate_rank"]) for row in rows if row.get("candidate_rank") is not None]
        return ReviewItem(
            asset_id=asset_id,
            paths=tuple(row["path"] for row in rows),
            preview_path=preview,
            group_id=group,
            candidate_rank=min(ranks) if ranks else None,
            star=max(int(row.get("star") or 0) for row in rows),
            label=next((row.get("label") for row in rows if row.get("label")), None),
            is_best=any(bool(row.get("is_best")) for row in rows),
            is_uncertain=any(bool(row.get("is_uncertain")) for row in rows),
            is_rejected=any(
                row.get("label") == "X" or bool(row.get("is_waste")) for row in rows
            ),
            has_technical_issue=any(
                bool(row.get("blur_reliable", True))
                and row.get("blur_score") is not None
                and float(row["blur_score"]) < config.BLUR_WASTE_THRESHOLD
                or row.get("over_ratio") is not None
                and float(row["over_ratio"]) > config.EXPO_WASTE_RATIO
                or row.get("under_ratio") is not None
                and float(row["under_ratio"]) > config.EXPO_WASTE_RATIO
                for row in rows
            ),
        )

    def reload(self) -> None:
        previous_id = self.current.asset_id if self.current else None
        previous_index = self.cursor
        snapshot = self.store.review_snapshot()
        self.photo_rows = {row["path"]: row for row in snapshot["photos"]}
        self.face_rows = {}
        for region in snapshot["face_regions"]:
            self.face_rows.setdefault(region["path"], []).append(region)
        grouped: dict[str, list[dict]] = {}
        for row in snapshot["photos"]:
            grouped.setdefault(row.get("asset_pair_id") or row["path"], []).append(row)
        self.items = {asset_id: self._item(asset_id, rows) for asset_id, rows in grouped.items()}

        groups: dict[int, list[str]] = {}
        for item in self.items.values():
            if item.group_id is not None:
                groups.setdefault(item.group_id, []).append(item.asset_id)
        group_rows = {row["id"]: row for row in snapshot["groups"]}
        self.groups = {
            group_id: ReviewGroup(
                group_id,
                tuple(
                    sorted(
                        ids,
                        key=lambda asset_id: (
                            self.items[asset_id].candidate_rank or 10_000,
                            asset_id,
                        ),
                    )
                ),
                bool(group_rows.get(group_id, {}).get("is_uncertain")),
            )
            for group_id, ids in groups.items()
        }
        self._rebuild_queue(previous_id, previous_index)

    def inspector_data(self, asset_id: str) -> tuple[dict, list[dict]]:
        item = self.items.get(asset_id)
        if item is None:
            return {}, []
        path = item.preview_path
        return self.photo_rows.get(path, {}), self.face_rows.get(path, [])

    def _matches(self, item: ReviewItem, name: ReviewFilter, star: int | None) -> bool:
        if name == ReviewFilter.ALL:
            return True
        if name == ReviewFilter.AI_PICKS:
            return item.is_best or item.label == "P"
        if name == ReviewFilter.UNCERTAIN:
            return item.is_uncertain
        if name == ReviewFilter.REJECTED:
            return item.is_rejected
        if name == ReviewFilter.TECHNICAL_ISSUE:
            return item.has_technical_issue
        if name == ReviewFilter.STAR:
            return item.star == star if star is not None else item.star >= self.star_filter
        return False

    def filter_counts(self) -> Mapping[ReviewFilter, int]:
        return {
            name: sum(self._matches(item, name, None) for item in self.items.values())
            for name in ReviewFilter
        }

    def _rebuild_queue(self, previous_id: str | None, previous_index: int) -> None:
        self.queue = [
            item
            for item in self.items.values()
            if self._in_scope(item)
            and self._matches(item, self.active_filter, self.filter_star)
        ]
        ids = [item.asset_id for item in self.queue]
        self.cursor = (
            ids.index(previous_id)
            if previous_id in ids
            else min(previous_index, max(0, len(ids) - 1))
        )

    def set_filter(self, name: ReviewFilter | str, *, star: int | None = None) -> None:
        previous_id = self.current.asset_id if self.current else None
        previous_index = self.cursor
        self.active_filter = ReviewFilter(name)
        self.filter_star = star
        # 【T-11】记住口径：下次打开回到上次那个视图（"每次重开重设"是隐形流失）。
        self.store.set_preference("review.filter", self.active_filter.value)
        self._rebuild_queue(previous_id, previous_index)

    def set_star_threshold(self, threshold: int) -> None:
        """设置并记住星级阈值（跨重启保留）。"""
        self.star_filter = int(threshold)
        self.store.set_preference("review.star_threshold", str(self.star_filter))

    def set_photo_scope(self, paths: set[str] | None) -> None:
        """限定审片范围到给定的照片路径集合（``None`` = 不限定）。

        【T-08】分面筛选是在 SQL 层按 EXIF/评级收窄的，但审片队列是按
        ``asset_id``（同组多张：RAW+JPEG 合并成一个 asset）组织的，两套键
        对不上。这里收路径集合、按"asset 里有任一路径命中"来判定，避免把
        一个 RAW+JPEG 组的半边筛掉导致预览错乱。

        这样做是**只读过滤**：不改 DB、不动决策，纯内存队列收窄（N3/红线）。
        """
        self._scope_paths = None if paths is None else set(paths)
        previous_id = self.current.asset_id if self.current else None
        previous_index = self.cursor
        self._rebuild_queue(previous_id, previous_index)

    @property
    def photo_scope(self) -> set[str] | None:
        return getattr(self, "_scope_paths", None)

    def _in_scope(self, item: ReviewItem) -> bool:
        scope = getattr(self, "_scope_paths", None)
        if scope is None:
            return True
        return any(path in scope for path in item.paths)

    def focus(self, asset_id: str) -> ReviewItem | None:
        for index, item in enumerate(self.queue):
            if item.asset_id == asset_id:
                self.cursor = index
                return item
        return None

    def next(self) -> ReviewItem | None:
        if self.queue:
            self.cursor = min(self.cursor + 1, len(self.queue) - 1)
        return self.current

    def previous(self) -> ReviewItem | None:
        if self.queue:
            self.cursor = max(self.cursor - 1, 0)
        return self.current

    def select_candidate(self, letter: str) -> ReviewItem | None:
        item = self.current
        if item is None or item.group_id not in self.groups:
            return None
        position = ord(letter.upper()) - ord("A")
        candidates = self.groups[item.group_id].item_ids
        if not 0 <= position < min(4, len(candidates)):
            return None
        return self.focus(candidates[position])

    # ------------------------------------------------------------------
    # 【T-10】多选与批量操作
    # ------------------------------------------------------------------
    def select_only(self, asset_id: str) -> None:
        """无修饰键点击：只选这一张，并把它设为区间锚点。"""
        self.selection = [asset_id] if asset_id in self.items else []
        self._anchor = asset_id

    def select_toggle(self, asset_id: str) -> None:
        """Ctrl/Cmd + 点击：翻转这一张的选中态（多选的基本操作）。"""
        if asset_id not in self.items:
            return
        if asset_id in self.selection:
            self.selection.remove(asset_id)
        else:
            self.selection.append(asset_id)
        self._anchor = asset_id

    def select_range(self, asset_id: str) -> None:
        """Shift + 点击：从锚点连选到这一张，**按当前队列顺序**。

        按队列顺序而不是按 items 的字典序 —— 用户看到的是胶片带的顺序，
        连选必须和他眼睛看到的区间一致。
        """
        if asset_id not in self.items:
            return
        order = [item.asset_id for item in self.queue]
        anchor = self._anchor if self._anchor in order else None
        if anchor is None or asset_id not in order:
            self.selection = [asset_id]
        else:
            start, end = order.index(anchor), order.index(asset_id)
            if start > end:
                start, end = end, start
            self.selection = order[start : end + 1]
        self._anchor = asset_id

    def select_all(self) -> None:
        """全选**当前队列**（不是全库）—— 用户看到多少就是多少。"""
        self.selection = [item.asset_id for item in self.queue]

    def set_selection(self, asset_ids) -> None:
        """由 UI（Qt 选择模型）整体同步选中集合。

        只保留**当前仍存在**的 asset —— 筛选变化后旧 id 可能已不在队列里，
        留着会让"已选 N 张"虚高，并让批量操作作用在看不见的照片上。
        """
        seen: list[str] = []
        for asset_id in asset_ids:
            if asset_id in self.items and asset_id not in seen:
                seen.append(asset_id)
        self.selection = seen
        self._anchor = seen[-1] if seen else None

    def clear_selection(self) -> None:
        self.selection = []
        self._anchor = None

    def selected_items(self) -> list[ReviewItem]:
        known = self.items
        return [known[asset_id] for asset_id in self.selection if asset_id in known]

    def selected_paths(self) -> list[str]:
        """选中 asset 展开成的照片路径（RAW+JPEG 会成对展开）。"""
        paths: list[str] = []
        for item in self.selected_items():
            paths.extend(item.paths)
        return paths

    def _is_manually_rejected(self, item: ReviewItem) -> bool:
        for path in item.paths:
            row = self.photo_rows.get(path) or {}
            if row.get("label") == "X":
                return True
        return False

    def rejected_items(self) -> list[ReviewItem]:
        """已排除项 —— **只算人工判废**（``label == "X"``）。

        为什么不含 AI 判废（``is_waste``）：那里面有用户**根本没看过的照片**。
        把它们一起搬进隔离目录，等于把没审过的片子当垃圾处理 —— 这正是选片工具
        最不该做的事。要清理 AI 废片，得先过一遍人工复核。
        """
        return [item for item in self.items.values() if self._is_manually_rejected(item)]

    def rejected_paths(self) -> list[str]:
        paths: list[str] = []
        for item in self.rejected_items():
            paths.extend(item.paths)
        return paths

    def apply_batch(self, *, star: int | None = None, flag: str | None = None) -> int:
        """对选中 asset 批量写决策：**单事务**，整体一个撤销点。

        返回实际写入的照片行数。500 张一次操作要么全成要么全回滚，撤销也是
        一次还原 —— 用户按一次 Ctrl+Z 就能回到批量前。
        """
        items = self.selected_items()
        if not items:
            return 0
        patches: list[tuple[str, int, str | None]] = []
        for item in items:
            for path in item.paths:
                row = self.photo_rows.get(path) or {}
                current_star = int(row.get("star") or 0)
                label = row.get("label")
                target_star = int(star) if star is not None else current_star
                if flag == "P":
                    target_star, label = 5, "P"
                elif flag == "X":
                    target_star, label = 0, "X"
                elif flag == "U":
                    label = None
                patches.append((path, target_star, label))
        if not patches:
            return 0
        before = self.store.apply_decisions_batch(patches, source="manual")
        self._undo_stack.append(
            BatchSnapshot(tuple(item.asset_id for item in items), tuple(before))
        )
        if len(self._undo_stack) > self.MAX_UNDO:
            self._undo_stack.pop(0)
        self.reload()
        return len(before)

    def _apply(self, *, star: int, label: str | None) -> None:
        item = self.current
        if item is None:
            return
        before = self.store.apply_decision(
            list(item.paths), star=star, label=label, source="manual"
        )
        self._undo_stack.append(DecisionSnapshot(item.asset_id, tuple(before)))
        if len(self._undo_stack) > self.MAX_UNDO:
            self._undo_stack.pop(0)
        self.reload()

    # ------------------------------------------------------------------
    # 产品规则（v0.6 S3-4 / P1-8）
    # ------------------------------------------------------------------
    # P / X 是**主决策**，星级是同一决策上的 1–5 级细分评价；两者必须保持一致，
    # 否则会出现自相矛盾的状态 —— 旧实现允许「0 星却仍挂着 P，并因此被导出」。
    #
    #   · P  = 选片 → star 记 5
    #   · X  = 弃选 → star 记 0
    #   · set_star(0)    → 清掉 P（0 星不再是"选片"，杜绝「0 星却被导出」）
    #   · set_star(n>=1) → 撤回 X（重新给星即不再弃选，杜绝「有星却不导出」）
    #
    # 导出侧口径见 services/export_service.ExportService._asset_problem：
    #   已复核 且 label != "X" 且 (label == "P" 或 star >= min_star)
    def decide(self, key: str) -> None:
        if key not in {"P", "X"}:
            raise ValueError(f"unsupported decision: {key}")
        self._apply(star=5 if key == "P" else 0, label=key)

    def set_star(self, star: int) -> None:
        if not 0 <= star <= 5:
            raise ValueError("star must be between 0 and 5")
        current = self.current
        if current is None:
            return
        label = current.label
        if star == 0:
            if label == "P":
                label = None
        elif label == "X":
            label = None
        self._apply(star=star, label=label)

    def set_scene_manual(self, scene: str | None) -> None:
        item = self.current
        if item is None:
            return
        before = self.store.set_scene_manual_batch(list(item.paths), scene)
        if not before:
            return
        self._undo_stack.append(SceneSnapshot(item.asset_id, tuple(before)))
        if len(self._undo_stack) > self.MAX_UNDO:
            self._undo_stack.pop(0)
        self.reload()

    def undo(self) -> bool:
        if not self._undo_stack:
            return False
        snapshot = self._undo_stack.pop()
        if isinstance(snapshot, SceneSnapshot):
            self.store.restore_scene_manual_batch(list(snapshot.before))
        else:
            self.store.restore_decisions(snapshot.before)
        self.reload()
        # 单张撤销回到那一张（用户要看见自己撤了什么）；批量撤销不聚焦 ——
        # 聚焦到其中任意一张都会让人误以为只有它变了。
        if isinstance(snapshot, (DecisionSnapshot, SceneSnapshot)):
            self.focus(snapshot.asset_id)
        elif snapshot.asset_ids:
            self.focus(snapshot.asset_ids[0])
        return True
