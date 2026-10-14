"""Five-zone keyboard-first review workspace wired exclusively to services."""
from __future__ import annotations

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from engine.store import PhotoStore
from services.review_service import ReviewFilter, ReviewService
from ui.review.canvas import CanvasWidget
from ui.review.facets import FacetModel
from ui.review.filmstrip import FilmstripModel, FilmstripView
from ui.review.filters import FilterSidebar
from ui.review.inspector import InspectorWidget
from ui.review.shortcuts import install_review_shortcuts


class ReviewPage(QWidget):
    def __init__(
        self,
        store: PhotoStore,
        *,
        image_loader=None,
        thumbnail_loader=None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.review = ReviewService(store)
        # 【T-08】分面模型与 ReviewService 共享同一个 store，但职责不同：
        # ReviewService 管"审片队列/导航/撤销"，FacetModel 管"按 EXIF/评级收窄"。
        # 两者都只读 DB，写由 services/commands.py 收口（N1）。
        self.facets = FacetModel(store)
        self._updating = False
        self._stopped = False
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(250)
        self._save_timer.timeout.connect(self.save_splitter_sizes)

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.sidebar = FilterSidebar()
        # 【T-08】分面条比旧 6 按钮宽 —— 相机/镜头型号本来就长。上限 280 是
        # 为了别把取景区挤没；用户拖动 splitter 的宽度会被记住（见 save_splitter_sizes）。
        self.sidebar.setMinimumWidth(180)
        self.sidebar.setMaximumWidth(280)
        self.sidebar.filter_selected.connect(self._filter_selected)
        self.sidebar.facet_toggled.connect(self._facet_toggled)
        self.sidebar.facet_cleared.connect(self._facet_cleared)
        self.splitter.addWidget(self.sidebar)

        middle = QWidget()
        middle_layout = QVBoxLayout(middle)
        middle_layout.setContentsMargins(4, 0, 4, 0)
        middle_layout.setSpacing(8)
        self.canvas = CanvasWidget(
            review_service=self.review,
            **({"image_loader": image_loader} if image_loader is not None else {}),
        )
        self.canvas.image_ready.connect(self._preview_ready)
        middle_layout.addWidget(self.canvas, 1)
        self.filmstrip_model = FilmstripModel(
            self.review.queue,
            **({"thumbnail_loader": thumbnail_loader} if thumbnail_loader is not None else {}),
        )
        self.filmstrip = FilmstripView(self.filmstrip_model)
        self.filmstrip.setFixedHeight(126)
        self.filmstrip.selectionModel().currentChanged.connect(self._filmstrip_selected)
        middle_layout.addWidget(self.filmstrip)
        footer = QHBoxLayout()
        self.progress_label = QLabel("0 / 0")
        # 【v0.6 S3-4 / P1-8】提示要把「星级与 P/X 的联动规则」说清楚，否则用户
        # 会以为星级与保留/排除互不影响，从而留下「0 星却已被保留」这类矛盾态。
        self.hint_label = QLabel("P 保留 · X 排除 · 0–5 星（0 星取消保留，给星撤回排除）· A–D 候选 · ←/→ 导航 · Ctrl+Z 撤销")
        self.hint_label.setToolTip(
            "P = 保留（记 5 星）；X = 排除（记 0 星）。\n"
            "把星级改成 0 会取消「保留」；给 1 星以上会撤回「排除」。\n"
            "只有被保留或达到最低星级的照片才会进入交付。"
        )
        self.confirm_button = QPushButton("确认并进入下一组")
        self.confirm_button.setObjectName("Primary")
        self.confirm_button.clicked.connect(self._next_group)
        footer.addWidget(self.progress_label)
        footer.addWidget(self.hint_label, 1)
        footer.addWidget(self.confirm_button)
        middle_layout.addLayout(footer)
        self.splitter.addWidget(middle)

        self.inspector = InspectorWidget(store)
        self.inspector.face_selected.connect(self.canvas.focus_rect)
        self.splitter.addWidget(self.inspector)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.splitterMoved.connect(lambda _position, _index: self._save_timer.start())
        root.addWidget(self.splitter, 1)
        self.actions = install_review_shortcuts(self, self._command)
        self._restore_splitter_sizes()
        self.refresh()

    def _restore_splitter_sizes(self) -> None:
        left = int(self.review.store.get_preference("review.left_width") or 180)
        right = int(self.review.store.get_preference("review.right_width") or 290)
        self.splitter.setSizes([left, 700, right])

    def save_splitter_sizes(self) -> None:
        if self._stopped:
            return
        sizes = self.splitter.sizes()
        if len(sizes) == 3 and sizes[0] and sizes[2]:
            self.review.store.set_preference("review.left_width", str(sizes[0]))
            self.review.store.set_preference("review.right_width", str(sizes[2]))

    def reload(self) -> None:
        self.review.reload()
        self.facets.rebuild()
        self.refresh()

    def refresh(self) -> None:
        if self._stopped:
            return
        current = self.review.current
        self._updating = True
        try:
            self.filmstrip_model.replace_items(self.review.queue)
            if current is not None:
                self.filmstrip.setCurrentIndex(self.filmstrip_model.index(self.review.cursor))
        finally:
            self._updating = False
        self.sidebar.set_counts(self.review.filter_counts())
        # 【T-08】分面快照 + 作用域 + 筛后总数。计数由 FacetModel 算好，
        # 筛选条只负责画（见 ui/review/filters.py 的模块 docstring）。
        self.sidebar.set_facets(self.facets.facets_snapshot())
        self.sidebar.set_scope_label(self.facets.scope_label())
        self.sidebar.set_summary(self.facets.count(), has_filters=self.facets.has_filters)
        if current is None:
            self.canvas.set_items([], self.review.view_mode)
            self.inspector.set_item({}, [])
            self.progress_label.setText("0 / 0")
            return
        if current.group_id is not None and current.group_id in self.review.groups:
            group = self.review.groups[current.group_id]
            group_items = [
                self.review.items[asset_id] for asset_id in group.item_ids
            ]
            self.progress_label.setText(
                f"组 {current.group_id} · 候选 {group.item_ids.index(current.asset_id) + 1}/{len(group.item_ids)}"
            )
        else:
            group_items = [current]
            self.progress_label.setText(f"照片 {self.review.cursor + 1}/{len(self.review.queue)}")
        self.canvas.set_items(
            group_items, self.review.view_mode, selected_id=current.asset_id
        )
        row, regions = self.review.inspector_data(current.asset_id)
        self.inspector.set_item(row, regions, focused=True)

    def _preview_ready(self, asset_id: str, image: QImage) -> None:
        current = self.review.current
        if current is not None and current.asset_id == asset_id:
            row, regions = self.review.inspector_data(asset_id)
            self.inspector.set_item(row, regions, focused=True, preview_image=image)

    def _filmstrip_selected(self, index, _previous) -> None:
        if self._updating or not index.isValid():
            return
        asset_id = index.data(FilmstripModel.AssetIdRole)
        if self.review.focus(asset_id) is not None:
            self.refresh()

    def _filter_selected(self, selected: ReviewFilter) -> None:
        """常用口径按钮：一键切到某个互斥口径，并清空分面。

        【为什么点口径要清分面】口径与分面若同时生效，用户看到的是
        "口径 ∩ 分面"，但按钮上只写着口径名 —— 那是会让人以为"照片丢了"
        的隐式组合。宁可把分面清掉，让状态始终是"所见即所得"。
        """
        self.facets.clear_all()
        self.review.set_filter(selected)
        self.refresh()

    def _facet_toggled(self, key: str, raw_value: object) -> None:
        """点某个分面值：在分面模型上翻转，并让审片队列跟上。

        【为什么审片队列也要跟着变】分面若只影响计数不影响队列，用户点了
        "5D4"却发现胶片条里还是全部照片 —— 筛选就是假的。这里把分面的
        条件树编译成 SQL、查出照片路径集合，再让 ReviewService 只保留这些
        照片（见 `_apply_facet_scope`）。
        """
        self.facets.toggle(key, raw_value)
        self._apply_facet_scope()
        self.refresh()

    def _facet_cleared(self) -> None:
        self.facets.clear_all()
        self._apply_facet_scope()
        self.refresh()

    def _apply_facet_scope(self) -> None:
        """把分面筛选结果下推给 ReviewService（只读查询，不写库）。"""
        if not self.facets.has_filters:
            self.review.set_photo_scope(None)
            return
        paths = {row["path"] for row in self.facets.query(order_by="path")}
        self.review.set_photo_scope(paths)

    def set_facet_scope(self, scope) -> None:
        """外部（集合/人物入口）设定作用域，例如 ``{"kind": "collection", ...}``。"""
        self.facets.set_scope(scope)
        self.refresh()

    def _next_group(self) -> None:
        current = self.review.current
        if current is None:
            return
        group_id = current.group_id
        # 【v0.6 S3-5 / P1-10】无分组的照片本身就是「一组」，不能再沿 group_id == None
        # 连续往后跳。旧实现会把 [独立A, 独立B, 独立C, 分组X] 从 A 直接跳到 X，
        # 让 B/C 完全被跳过。
        if group_id is None:
            self.review.next()
            self.refresh()
            return
        while self.review.next() is not None:
            next_item = self.review.current
            if next_item.asset_id == current.asset_id or next_item.group_id != group_id:
                break
            current = next_item
        self.refresh()

    def _command(self, command: str) -> None:
        if command == "pick":
            self.review.decide("P")
        elif command == "reject":
            self.review.decide("X")
        elif command.startswith("star-"):
            self.review.set_star(int(command[-1]))
        elif command.startswith("candidate-"):
            self.review.select_candidate(command[-1].upper())
        elif command == "previous":
            self.review.previous()
        elif command == "next":
            self.review.next()
        elif command == "toggle-view":
            self.review.toggle_view_mode()
        elif command == "fullscreen":
            window = self.window()
            window.showNormal() if window.isFullScreen() else window.showFullScreen()
        elif command == "toggle-inspector":
            self.inspector.setVisible(self.inspector.isHidden())
        elif command == "undo":
            self.review.undo()
        self.refresh()

    def shutdown(self) -> None:
        if self._stopped:
            return
        if self._save_timer.isActive():
            self._save_timer.stop()
        self.save_splitter_sizes()
        self._stopped = True
        self.filmstrip_model.close()
        self.canvas.close()
