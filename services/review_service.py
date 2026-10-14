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


class ReviewService:
    MAX_UNDO = 200

    def __init__(self, store: PhotoStore, *, star_filter: int = 4) -> None:
        self.store = store
        self.star_filter = star_filter
        self.active_filter = ReviewFilter.ALL
        self.filter_star: int | None = None
        # 【T-08】分面筛选下推的范围（照片路径集合）；None = 不限定。
        self._scope_paths: set[str] | None = None
        self.items: dict[str, ReviewItem] = {}
        self.groups: dict[int, ReviewGroup] = {}
        self.photo_rows: dict[str, dict] = {}
        self.face_rows: dict[str, list[dict]] = {}
        self.queue: list[ReviewItem] = []
        self.cursor = 0
        self._undo_stack: list[DecisionSnapshot] = []
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
                row.get("blur_score") is not None
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
        self._rebuild_queue(previous_id, previous_index)

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

    def undo(self) -> bool:
        if not self._undo_stack:
            return False
        snapshot = self._undo_stack.pop()
        self.store.restore_decisions(snapshot.before)
        self.reload()
        self.focus(snapshot.asset_id)
        return True
