"""Five-zone keyboard-first review workspace wired exclusively to services."""
from __future__ import annotations

from PySide6.QtCore import (
    QItemSelection,
    QItemSelectionModel,
    QTimer,
    Qt,
    Signal,
)
from PySide6.QtGui import QImage
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from engine.store import PhotoStore
from services.collection_service import (
    CollectionService,
    count_for_condition,
    describe_condition,
)
from services.review_service import ReviewFilter, ReviewService
from ui.review.batch_bar import BatchBar
from ui.review.canvas import CanvasWidget
from ui.review.facets import FacetModel
from ui.review.filmstrip import FilmstripModel, FilmstripView
from ui.review.filters import FilterSidebar
from ui.review.inspector import InspectorWidget
from ui.review.shortcuts import install_review_shortcuts
from ui.review.smart_collections import CollectionPanel


class ReviewPage(QWidget):
    # 【T-11】复核页自身完成不了的两个动作交给 Shell：
    # navigate_requested —— 跨页跳转（如 Ctrl+E 去导出页）；
    # notice_requested   —— 状态栏提示（含"该功能尚未启用"的原因说明）。
    navigate_requested = Signal(str)
    notice_requested = Signal(str)

    def __init__(
        self,
        store: PhotoStore,
        *,
        image_loader=None,
        thumbnail_loader=None,
        commands=None,
        bindings=None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        # 【T-10】命令表：批量与清理都必须走它，UI 不直接写库（N1）。
        self.commands = commands
        # 【T-11】快捷键绑定（``{命令: 按键}``）。None = 沿用 v0.6 的既有键位。
        self.bindings = bindings
        self.review = ReviewService(store)
        # 【T-08】分面模型与 ReviewService 共享同一个 store，但职责不同：
        # ReviewService 管"审片队列/导航/撤销"，FacetModel 管"按 EXIF/评级收窄"。
        # 两者都只读 DB，写由 services/commands.py 收口（N1）。
        self.facets = FacetModel(store)
        # 【T-09】集合服务：读集合、算实时计数。写仍走命令表（N1）。
        self.collections = CollectionService(store)
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
        # 【T-09】集合面板挂在筛选条下方，共用左侧栏（都是"收窄范围"的动作）。
        self.collections_panel = CollectionPanel()
        self.collections_panel.save_requested.connect(self._save_collection)
        self.collections_panel.applied.connect(self._apply_collection)
        self.collections_panel.delete_requested.connect(self._delete_collection)
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(6)
        left_layout.addWidget(self.sidebar, 1)
        left_layout.addWidget(self.collections_panel)
        self.splitter.addWidget(left_panel)

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
        # 【T-10】交给 Qt 自己的 ExtendedSelection：Ctrl/Shift 连选与高亮是
        # 免费且行为标准的，自己实现一套只会和用户的系统习惯对不上。
        self.filmstrip.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.filmstrip.selectionModel().currentChanged.connect(self._filmstrip_selected)
        self.filmstrip.selectionModel().selectionChanged.connect(self._filmstrip_selection_changed)
        middle_layout.addWidget(self.filmstrip)

        self.batch_bar = BatchBar()
        self.batch_bar.star_requested.connect(self._batch_star)
        self.batch_bar.flag_requested.connect(self._batch_flag)
        self.batch_bar.select_all_requested.connect(self._batch_select_all)
        self.batch_bar.clear_requested.connect(self._batch_clear)
        self.batch_bar.cleanup_requested.connect(self._cleanup_rejected)
        middle_layout.addWidget(self.batch_bar)

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
        self.inspector.scene_requested.connect(self._set_scene_manual)
        self.splitter.addWidget(self.inspector)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.splitterMoved.connect(lambda _position, _index: self._save_timer.start())
        root.addWidget(self.splitter, 1)
        self.actions = install_review_shortcuts(self, self._command, bindings=self.bindings)
        self._restore_splitter_sizes()
        self.refresh()

    def install_shortcuts(self, bindings) -> None:
        """【T-11】换一套键位：先拆掉旧的，再装新的。

        不重建页面 —— 那会丢掉滚动位置、选中与撤销栈上下文；QAction 本来
        就是可以随时增删的。
        """
        for action in self.actions.values():
            self.removeAction(action)
            action.deleteLater()
        self.bindings = dict(bindings) if bindings else None
        self.actions = install_review_shortcuts(self, self._command, bindings=self.bindings)

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
        # 【T-10】队列重建后把选中态画回胶片带，并刷新批量条计数。
        self._sync_filmstrip_selection()
        self._update_batch_bar()
        self.sidebar.set_counts(self.review.filter_counts())
        # 【T-08】分面快照 + 作用域 + 筛后总数。计数由 FacetModel 算好，
        # 筛选条只负责画（见 ui/review/filters.py 的模块 docstring）。
        self.sidebar.set_facets(self.facets.facets_snapshot())
        self.sidebar.set_scope_label(self.facets.scope_label())
        self.sidebar.set_summary(self.facets.count(), has_filters=self.facets.has_filters)
        self._refresh_collections()
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

    def _set_scene_manual(self, scene: str | None) -> None:
        if self.review.current is None:
            return
        self.review.set_scene_manual(scene)
        self.notice_requested.emit("人工场景已保存；下次分析将按新场景重算推荐，可按 Ctrl+Z 撤销")
        self.refresh()

    def _filmstrip_selected(self, index, _previous) -> None:
        if self._updating or not index.isValid():
            return
        asset_id = index.data(FilmstripModel.AssetIdRole)
        if self.review.focus(asset_id) is not None:
            self.refresh()

    def _filmstrip_selection_changed(self, _selected, _deselected) -> None:
        """Qt 选择模型 → ReviewService（选中集合的唯一真相仍在 service 里）。"""
        if self._updating:
            return
        model = self.filmstrip.model()
        asset_ids = [
            model.data(index, FilmstripModel.AssetIdRole)
            for index in self.filmstrip.selectionModel().selectedIndexes()
        ]
        self.review.set_selection([asset_id for asset_id in asset_ids if asset_id])
        self._update_batch_bar()

    def _sync_filmstrip_selection(self) -> None:
        """ReviewService → Qt 选择模型（队列重建后把选中态画回去）。"""
        model = self.filmstrip.model()
        selection_model = self.filmstrip.selectionModel()
        if model is None or selection_model is None:
            return
        merged = QItemSelection()
        for row, item in enumerate(self.review.queue):
            if item.asset_id in self.review.selection:
                index = model.index(row, 0)
                merged.select(index, index)
        self._updating = True
        try:
            selection_model.select(
                merged, QItemSelectionModel.SelectionFlag.ClearAndSelect
            )
        finally:
            self._updating = False

    # ------------------------------------------------------------------
    # 【T-09】智能集合
    # ------------------------------------------------------------------
    def _collection_rows(self) -> list[dict]:
        """集合列表 + 实时计数 + 路径（供点选后切作用域）。"""
        if self.collections is None:
            return []
        rows = []
        for row in self.collections.list():
            row["paths"] = self.collections.paths(int(row["id"]))
            rows.append(row)
        return rows

    def _refresh_collections(self) -> None:
        self.collections_panel.refresh(self._collection_rows())

    def _save_collection(self) -> None:
        """把**当前筛选条件**存成集合（不是当前照片清单）。"""
        if self.commands is None or self.collections is None:
            self.notice_requested.emit("命令表不可用，无法保存集合")
            return
        condition = self.facets.condition_tree()
        preview = count_for_condition(
            self.review.store, condition, scope=self.facets.scope
        )
        name, accepted = QInputDialog.getText(
            self,
            "保存为集合",
            f"当前条件将命中 {preview} 张照片。\n"
            "集合保存的是**条件**（之后分析的新照片会自动纳入），不是照片清单。",
            text="",
        )
        if not accepted or not name.strip():
            return
        result = self.commands.execute(
            "collection.save",
            {
                "name": name.strip(),
                "condition": condition,
                "description": describe_condition(condition) if condition else "",
            },
        )
        if not result.ok:
            self.notice_requested.emit(
                f"保存集合失败：{result.error.message if result.error else result}"
            )
            return
        value = result.value if isinstance(result.value, dict) else {}
        self.notice_requested.emit(
            f"已保存集合「{value.get('name')}」· {value.get('count')} 张"
        )
        self._refresh_collections()

    def _apply_collection(self, scope: dict) -> None:
        self.facets.set_scope(scope)
        self.refresh()

    def _delete_collection(self, collection_id: int) -> None:
        if self.commands is None:
            self.notice_requested.emit("命令表不可用，无法删除集合")
            return
        answer = QMessageBox.question(
            self,
            "删除集合",
            "删除这个集合？\n照片本身不会受影响（集合只是一组条件）。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        result = self.commands.execute("collection.delete", {"id": collection_id})
        if not result.ok:
            self.notice_requested.emit(
                f"删除失败：{result.error.message if result.error else result}"
            )
            return
        # 若当前正停在这个集合里，删掉后要把作用域退回全部照片，
        # 否则用户会卡在一个已不存在的集合里，看到 0 张却不明白为什么。
        current_scope = self.facets.scope or {}
        if current_scope.get("kind") == "collection" and current_scope.get("id") == collection_id:
            self.facets.set_scope(None)
        self._refresh_collections()
        self.refresh()

    def _update_batch_bar(self) -> None:
        self.batch_bar.set_selection_count(len(self.review.selection))
        self.batch_bar.set_total_count(len(self.review.queue))
        self.batch_bar.set_rejected_count(len(self.review.rejected_items()))

    def _batch_star(self, star: int) -> None:
        self.review.apply_batch(star=int(star))
        self.reload()

    def _batch_flag(self, flag: str) -> None:
        self.review.apply_batch(flag=str(flag))
        self.reload()

    def _batch_select_all(self) -> None:
        self.review.select_all()
        self._sync_filmstrip_selection()
        self._update_batch_bar()

    def _batch_clear(self) -> None:
        self.review.clear_selection()
        self._sync_filmstrip_selection()
        self._update_batch_bar()

    def _cleanup_rejected(self) -> None:
        """清理已排除项：移进项目内隔离目录（N3，绝不真删）。"""
        paths = self.review.rejected_paths()
        if not paths:
            return
        project_dir = self.review.store.get_meta("source_dir") or ""
        if not project_dir:
            QMessageBox.warning(
                self,
                "无法清理",
                "当前项目没有记录来源目录，无法确定隔离目录的位置。",
            )
            return
        if self.commands is None:
            QMessageBox.warning(self, "无法清理", "命令表未装配，清理命令不可用。")
            return
        answer = QMessageBox.question(
            self,
            "清理已排除项",
            f"将把 {len(paths)} 张人工判废的照片移进隔离目录：\n"
            f"{project_dir}\\.trash_<今天日期>\n\n"
            "这是**搬移**不是删除 —— 可以随时搬回原处。\n"
            "AI 判废但你看都没看过的照片不会被搬走。\n\n确定继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        result = self.commands.execute("cleanup.rejected", {"paths": paths, "project_dir": project_dir})
        if not result.ok:
            QMessageBox.warning(self, "清理未执行", str(result.error))
            return
        value = result.value if isinstance(result.value, dict) else {}
        QMessageBox.information(
            self,
            "清理完成",
            f"已隔离 {value.get('quarantined', 0)} 张"
            + (f"，{value.get('missing', 0)} 张原文件已不在" if value.get("missing") else "")
            + (f"，{value.get('failed', 0)} 张失败" if value.get("failed") else "")
            + f"\n隔离目录：{value.get('root', '')}",
        )
        self.reload()

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
        # ---- 【T-11】新增键位 ----
        elif command == "confirm-best":
            self._next_group()
        elif command == "zoom-preview":
            self.canvas.set_zoom(1.0 if self.canvas.zoom_percent() > 100 else 2.0)
        elif command == "export":
            self.navigate_requested.emit("export")
            return
        elif command == "save-collection":
            self._save_collection()
            return
        else:
            self._command_pending(command)
            return
        self.refresh()

    def _command_pending(self, command: str) -> None:
        """尚未启用的键位 —— **必须说清原因，绝不静默 no-op**。

        静默什么都不做比报错更坏：用户会以为是自己按错了，反复重试。
        """
        reasons = {
            "search": "搜索（Ctrl+F）将在 T-12 规模优化后随虚拟滚动一起上线",
            "command-palette": "命令面板（Ctrl+K）是未来 Agent 入口，本期只做架构接缝（T-06），功能未开放",
            "goto-face-group": "跳到人脸组（Ctrl+Shift+N）依赖人物线，见 T-15",
            "assign-person": "归属人物（T）依赖人物线，见 T-15",
        }
        self.notice_requested.emit(reasons.get(command, f"命令 {command} 尚未接线"))

    def shutdown(self) -> None:
        if self._stopped:
            return
        if self._save_timer.isActive():
            self._save_timer.stop()
        self.save_splitter_sizes()
        self._stopped = True
        self.filmstrip_model.close()
        self.canvas.close()
