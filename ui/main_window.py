"""Professional four-stage shell and project lifecycle.

【T-05 / M0-5 改造】从"一次向导"升级为"可持续的库"：

    * 新增 **项目入口页**（``LibraryPage``）作为 Shell 的首页 —— 列出已登记项目，
      支持新建 / 打开 / 从文件夹打开 / 从列表移除。
    * 原来的四阶段工作流（导入→分析→复核→导出）四个 Page **内部实现一律不动**，
      只是被 Shell 挂到内容区；打开项目时按需重建"依赖 store"的页面。
    * 项目切换的实质：把 ``self.store`` 指向该项目的 ``cull.db``，并重建
      ReviewPage / ExportPage（它们在构造时捕获 store）。ImportPage / AnalyzePage
      不持有 store，跨项目复用即可。
    * 关闭重开：App 启动即进入口页，选中项目后回到该项目库 —— 人工决策
      （star / label / decision_source）都在各自的 ``cull.db`` 里，天然零丢失。

红线遵守：
    * 本文件不含 SQL（N6）；库读写一律经 ``engine/store.py`` 与
      ``engine/catalog_registry.py``。
    * 不触碰 ``METRIC_FIELDS`` / ``_ai_model_signature()``（A6）。
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from engine import config
from engine.catalog_registry import CatalogRegistry, read_photo_count
from engine.log import get_logger
from engine.store import PhotoStore
from engine.user_settings import UserSettings
from services.command_catalog import build_registry
from services.shortcut_settings import (
    DEFAULT_BINDINGS,
    ShortcutConflict,
    load_bindings,
    save_bindings,
)
from services.events import EventBus
from services.task_center import TaskCenter
from .pages.analyze_page import AnalyzePage
from .pages.export_page import ExportPage
from .pages.import_page import ImportPage
from .pages.library_page import LibraryPage
from .pages.review_page import ReviewPage
from .settings import open_settings
from .task_panel import TaskPanel

_log = get_logger("ui.main")


STAGES = (
    ("import", "1 导入"),
    ("analyze", "2 分析"),
    ("review", "3 复核"),
    ("export", "4 导出到 LR"),
)

LIBRARY_PAGE = "library"

# 【T-07】分析任务的稳定名称 —— 取消时用它把 Task 与 AnalyzeWorker 对上。
_ANALYZE_TASK_NAME = "分析照片"


class MainWindow(QMainWindow):
    def __init__(
        self,
        *,
        project_db: str | Path | None = None,
        registry: CatalogRegistry | None = None,
        start_in_library: bool | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Lumina Select · 光影选片助手")
        self.resize(1280, 800)
        self.setMinimumSize(960, 600)

        # 全局项目注册表：跨项目索引，独立于任何项目库。
        self.registry = registry or CatalogRegistry()
        # 允许注入以便测试；默认沿用既有行为（config.DEFAULT_DB）。
        self._initial_project = str(project_db or config.DEFAULT_DB)
        # 未显式指定时：给了具体项目库就直接开工（兼容老用法），
        # 否则先进入口页（跨天工作流的正门）。
        self.start_in_library = (
            (project_db is None) if start_in_library is None else start_in_library
        )

        # PhotoStore 会顺手把库文件建出来（哪怕只是启动默认库），所以"库存在"
        # 并不代表"用户用过它" —— 是否值得登记只看照片数，见 register_current_project。
        self.store: PhotoStore = PhotoStore(self._initial_project)
        self.project_path: str = self.store.db_path
        # 【T-06】命令注册表：UI 的写操作全部经它执行（N1）。
        # 它是"未来 Agent 的唯一入口"在本期的形态 —— UI 与 Agent 走同一条路。
        self.commands = build_registry(self.store)
        # 【T-07】统一任务中心 + 进程内事件总线。
        # 事件总线是"正在发生什么"的唯一广播口：UI 与未来 Agent 都订阅它，
        # 而不是各自轮询 Task 对象 —— 保证两边看到同一份事实。
        self.events = EventBus()
        self.tasks = TaskCenter()
        # 【T-11】全局用户设置（跨项目、跨重启）。拿不到可写位置时不能让程序起不来
        # —— 退化成"用默认键位、不记住改动"，比直接崩要好。
        self.user_settings = self._open_user_settings()
        self.shortcut_bindings = (
            load_bindings(self.user_settings) if self.user_settings is not None
            else dict(DEFAULT_BINDINGS)
        )
        self.current_stage_name = "import"
        self._project_pages: dict[str, QWidget] = {}

        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(16, 16, 16, 8)
        root_layout.setSpacing(12)

        header = QFrame()
        header.setObjectName("WorkflowHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 12, 16, 12)
        self.back_button = QPushButton("← 我的项目")
        self.back_button.setObjectName("BackToLibrary")
        self.back_button.setToolTip("返回项目列表（当前项目的标星/标记会保留）")
        self.back_button.clicked.connect(lambda: self.show_library())
        header_layout.addWidget(self.back_button)
        brand = QLabel("LUMINA SELECT")
        brand.setObjectName("PageTitle")
        header_layout.addWidget(brand)
        header_layout.addStretch(1)
        self.stage_buttons: list[QPushButton] = []
        for name, label in STAGES:
            button = QPushButton(label)
            button.setObjectName("StageButton")
            button.clicked.connect(lambda _checked=False, stage=name: self.goto(stage))
            header_layout.addWidget(button)
            self.stage_buttons.append(button)
        root_layout.addWidget(header)

        state_row = QHBoxLayout()
        self.stage_state_label = QLabel()
        self.stage_state_label.setObjectName("StageState")
        state_row.addWidget(self.stage_state_label)
        state_row.addStretch(1)
        # 【v0.6 S4-1 / G4】项目级兜底入口：旧库升级说明 + 已复核清单导出。
        # 两者都放在状态行而非弹窗自动弹出 —— 启动时不阻塞用户，
        # 需要时一点即得（旧库提示按钮只在确实检测到旧库时才出现）。
        self.upgrade_button = QPushButton("旧库升级说明")
        self.upgrade_button.setObjectName("UpgradeNotice")
        self.upgrade_button.clicked.connect(self.show_upgrade_notice)
        self.upgrade_button.setVisible(False)
        state_row.addWidget(self.upgrade_button)
        self.decisions_export_button = QPushButton("导出已复核清单")
        self.decisions_export_button.setObjectName("ExportDecisions")
        self.decisions_export_button.setToolTip(
            "把人工标星/保留排除/人工场景导出为 CSV 兜底清单"
            "（库文件损坏或被误删时仍能查到自己做过什么）"
        )
        self.decisions_export_button.clicked.connect(lambda: self.export_review_list())
        state_row.addWidget(self.decisions_export_button)
        # 【T-11】快捷键设置入口。设置是**跨项目**的（存在全局设置库），
        # 所以放在顶栏而不是某个页面里。
        self.settings_button = QPushButton("快捷键设置")
        self.settings_button.setObjectName("SettingsButton")
        self.settings_button.setToolTip("自定义快捷键（保存在本机，重启后仍生效）")
        self.settings_button.clicked.connect(self.open_settings_dialog)
        state_row.addWidget(self.settings_button)
        root_layout.addLayout(state_row)

        self.stack = QStackedWidget()
        # 入口页（不持有 store）。
        self.library_page = LibraryPage()
        # 不依赖 store 的两个页面：跨项目复用。
        self.import_page = ImportPage()
        self.analyze_page = AnalyzePage()
        self.stack.addWidget(self.library_page)
        self.stack.addWidget(self.import_page)
        self.stack.addWidget(self.analyze_page)
        # 依赖 store 的两个页面：随项目重建。
        self.review_page: ReviewPage | None = None
        self.export_page: ExportPage | None = None
        self._install_project_pages()
        root_layout.addWidget(self.stack, 1)
        # 【T-07】底部任务面板：所有长任务的统一进度/取消入口。
        self.task_panel = TaskPanel(self.tasks)
        self.task_panel.cancel_requested.connect(self.cancel_task)
        root_layout.addWidget(self.task_panel)
        self.setCentralWidget(root)
        self.statusBar().showMessage("项目已保存 · 照片仅在本机处理")

        # 【T-07】UI 线程泵：worker 线程只把事件入队，由这里的定时器在 UI 线程
        # 一次性发布到事件总线 —— 保证订阅者（含未来的 Agent 桥）总在 UI 线程
        # 被回调，可安全更新 Qt 控件。250ms 足够跟上人眼，且开销可忽略。
        self._task_pump = QTimer(self)
        self._task_pump.setInterval(250)
        self._task_pump.timeout.connect(self._pump_tasks)
        self._task_pump.start()

        self.library_page.open_project.connect(self.open_project)
        self.library_page.create_project.connect(self.create_project)
        self.library_page.forget_project.connect(self.forget_project)
        self.import_page.source_selected.connect(self.begin_analysis)
        self.analyze_page.succeeded.connect(self._analysis_succeeded)
        self.analyze_page.failed.connect(self.show_error)
        self.analyze_page.cancelled.connect(lambda: self.status("分析已取消"))
        # 【T-07】分析 worker 的进度/结果落到统一任务中心 —— 面板与事件总线
        # 因此不必知道"分析"这个具体业务，只认"任务"这一层抽象。
        self.analyze_page.progress.connect(self._analysis_progress)
        self.analyze_page.finished.connect(self._analysis_finished)

        self.register_current_project(touch=True, skip_unused_default=True)
        if self.start_in_library:
            self.show_library()
        else:
            self.goto("import")
        self.refresh_upgrade_notice()

    # ------------------------------------------------------------------
    # 页面装配
    # ------------------------------------------------------------------
    def _install_project_pages(self) -> None:
        """按当前 ``self.store`` 创建/替换依赖 store 的页面。"""
        old_review, old_export = self.review_page, self.export_page
        # 【T-10】命令表随 store 重建（见 _switch_project），这里必须传当下的那份，
        # 否则清理命令会写进上一个项目的库。
        self.review_page = ReviewPage(
            self.store, commands=self.commands, bindings=self.shortcut_bindings
        )
        # 【T-11】复核页自身无法完成的两个动作交给 Shell：跨页导航与状态栏提示。
        self.review_page.navigate_requested.connect(self.goto)
        self.review_page.notice_requested.connect(
            lambda message: self.statusBar().showMessage(message)
        )
        self.export_page = ExportPage(self.store)
        self.stack.addWidget(self.review_page)
        self.stack.addWidget(self.export_page)
        self._project_pages = {
            "import": self.import_page,
            "analyze": self.analyze_page,
            "review": self.review_page,
            "export": self.export_page,
        }
        # 旧页面必须真正退场（它们各自持有 store 与定时器/线程）。
        if old_review is not None:
            try:
                old_review.shutdown()
            except Exception as error:  # noqa: BLE001
                _log.warning("释放旧复核页失败（已忽略）：%s", error)
            self.stack.removeWidget(old_review)
            old_review.deleteLater()
        if old_export is not None:
            try:
                old_export.shutdown()
            except Exception as error:  # noqa: BLE001
                _log.warning("释放旧导出页失败（已忽略）：%s", error)
            self.stack.removeWidget(old_export)
            old_export.deleteLater()

    @property
    def pages(self) -> dict[str, QWidget]:
        """工作流四页（不含入口页）。"""
        return self._project_pages

    @property
    def current_page(self) -> QWidget:
        return self.stack.currentWidget()

    # ------------------------------------------------------------------
    # 项目生命周期
    # ------------------------------------------------------------------
    def register_current_project(self, *, touch: bool = True,
                                 skip_unused_default: bool = False) -> None:
        """把当前项目登记进全局注册表（照片数取只读查询结果）。

        ``skip_unused_default``：启动时用的是"启动默认库"而非用户明确打开的项目。
        此时只有它**确实装过照片**才值得登记，否则就别塞进入口页 ——
        不然用户会看到一张指向空库、名字还叫 ``cull`` 或仓库名的幽灵卡片。
        判据只有一个：**库里有照片**（``photo_count`` 为真）。

        注意这里**不能**用"库文件是否预先存在"当判据：``PhotoStore`` 构造时会
        顺手把库建出来，于是从第二次启动起文件永远存在 —— 一个从未用过的
        默认库会变成永久幽灵卡片。用户真正在意的是"有没有内容"，只看计数。
        """
        if skip_unused_default:
            count = read_photo_count(self.project_path)
            if not count:
                return
        try:
            count = read_photo_count(self.project_path)
            self.registry.register(self.project_path, photo_count=count, touch=touch)
        except Exception as error:  # noqa: BLE001 —— 登记失败不该拦住开工
            _log.warning("登记项目到全局库失败（已忽略）：%s", error)

    def library_entries(self):
        """给入口页用的项目列表（并顺带刷新各项目照片数）。"""
        entries = self.registry.entries()
        for entry in entries:
            count = read_photo_count(entry.path)
            if count is not None and count != entry.photo_count:
                try:
                    self.registry.update_photo_count(entry.path, count)
                    entry.photo_count = count
                except Exception as error:  # noqa: BLE001
                    _log.warning("刷新项目照片数失败（已忽略）：%s", error)
        return entries

    def show_library(self) -> None:
        """切到入口页，并刷新卡片。"""
        self.current_stage_name = LIBRARY_PAGE
        self.library_page.show_entries(self.library_entries())
        self.stack.setCurrentWidget(self.library_page)
        self._set_workflow_visible(False)
        self.stage_state_label.setText("我的项目 · 选择一个项目继续")

    def _set_workflow_visible(self, visible: bool) -> None:
        """入口页下隐藏阶段按钮与项目级兜底入口，避免误导。"""
        for button in self.stage_buttons:
            button.setVisible(visible)
        self.back_button.setVisible(visible)
        self.decisions_export_button.setVisible(visible)
        # 升级提示按钮的可见性由 refresh_upgrade_notice 决定（只有检测到旧库才显示），
        # 这里只在"回到入口页"时收起它，回到工作流时交还给 refresh_upgrade_notice。
        if not visible:
            self.upgrade_button.setVisible(False)
        else:
            self.refresh_upgrade_notice(announce=False)

    def open_project(self, project_path: str | Path) -> None:
        """打开一个项目：定位/新建其 cull.db，切换 store，进入导入页。

        ``project_path`` 可以是项目库文件，也可以是项目文件夹（入口页
        "从文件夹打开…"给的就是目录）——目录按约定收敛到 ``<dir>/data/cull.db``。
        """
        db_path = self._resolve_project_db(project_path)
        if db_path is None:
            return
        if Path(db_path).resolve() == Path(self.project_path).resolve():
            # 已经是当前项目：只刷新并回到导入页，不重建页面（避免无谓抖动）。
            self.registry.touch(db_path)
            self.goto("import")
            return
        self._switch_project(db_path)
        self.goto("import")
        count = read_photo_count(db_path)
        suffix = f" · {count} 张" if count else ""
        self.status(f"已打开项目：{Path(db_path).stem}{suffix}")

    def create_project(self, directory: str | Path) -> None:
        """在指定文件夹新建项目并打开（库文件落在 ``<dir>/data/cull.db``）。"""
        db_path = self._project_db_in(Path(directory))
        if db_path is None:
            return
        self.open_project(db_path)

    def forget_project(self, project_path: str | Path) -> None:
        """从入口页列表移除登记 —— **绝不删除库文件或照片**。"""
        path = str(project_path)
        removed = self.registry.forget(path)
        self.library_page.show_entries(self.library_entries())
        if removed:
            self.status(f"已从列表移除：{Path(path).stem}（项目文件未被删除）")

    def _switch_project(self, db_path: str | Path) -> None:
        """把 store 换到新项目并重建依赖 store 的页面。"""
        if self.export_page is not None and not self.export_page.shutdown():
            self.show_error("导出仍在完成当前文件，请稍后再切换项目")
            return
        try:
            new_store = PhotoStore(str(db_path))
        except Exception as error:  # noqa: BLE001
            _log.exception("打开项目失败：%s", db_path)
            self.show_error(f"打开项目失败：{error}")
            return
        old_store = self.store
        self.store = new_store
        self.project_path = new_store.db_path
        # 命令表绑定的是 store，换项目必须重建 —— 否则命令会写进上一个项目库。
        self.commands = build_registry(new_store)
        # 【T-07】换项目时清空任务中心：上一个项目的"已完成任务"对新项目是噪音，
        # 而它的 store 也即将关闭，任务的 payload 不再可解释。进行中的任务
        # 已在上面被 export 的 shutdown 拦住（不允许带着未完成任务切项目）。
        self.tasks.clear()
        self._analysis_task = None
        if hasattr(self, "task_panel"):
            self.task_panel.refresh()
        # 顺序要紧：先让旧页面退场（它们还持有旧 store），再关旧连接。
        # 反过来会在 shutdown 里对已关闭的库做操作，日志刷一堆无用的 WARNING。
        self._install_project_pages()
        try:
            old_store.close()
        except Exception as error:  # noqa: BLE001
            _log.warning("关闭旧项目库失败（已忽略）：%s", error)
        self.register_current_project(touch=True)
        self.refresh_upgrade_notice(announce=False)

    def _resolve_project_db(self, project_path: str | Path) -> str | None:
        """把用户给的路径（库文件或目录）解析成可用的项目库路径。"""
        path = Path(project_path)
        if path.is_dir():
            return self._project_db_in(path)
        if path.suffix.lower() == ".db":
            return str(path)
        # 既不是已存在的目录、也不是 .db 文件 —— 当作"待创建的项目目录"处理。
        # ⚠️ 这里必须是 path 本身，不能退到 path.parent：否则会把父目录里的
        #    其它 .db（例如同级的 catalogs.db）当成用户想打开的项目。
        return self._project_db_in(path)

    def _project_db_in(self, directory: Path) -> str | None:
        """在目录中定位项目库：优先 ``data/cull.db``，其次已有的 ``*.db``。"""
        if not directory.exists():
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError as error:
                self.show_error(f"无法创建项目目录：{error}")
                return None
        canonical = directory / "data" / "cull.db"
        if canonical.is_file():
            return str(canonical)
        root_db = directory / "cull.db"
        if root_db.is_file():
            return str(root_db)
        # 目录里已有其它 .db（用户自建项目）→ 用最新的那个。
        # 排除全局注册表文件名：万一用户把项目目录选到了 LuminaSelect 数据目录，
        # 也不该把 catalogs.db 误当成项目库。
        candidates = sorted(
            (p for p in directory.glob("*.db")
             if p.is_file() and p.name.lower() != "catalogs.db"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if candidates:
            return str(candidates[0])
        return str(canonical)

    # ------------------------------------------------------------------
    # 工作流导航（保持既有行为）
    # ------------------------------------------------------------------
    def goto(self, name: str) -> None:
        if name not in self._project_pages:
            raise ValueError(f"unknown workflow stage: {name}")
        self.current_stage_name = name
        self._set_workflow_visible(True)
        if name == "review":
            self.review_page.reload()
        elif name == "export":
            self.export_page.reload()
        self.stack.setCurrentWidget(self._project_pages[name])
        current_index = list(self._project_pages).index(name)
        for index, button in enumerate(self.stage_buttons):
            state = "current" if index == current_index else "done" if index < current_index else "future"
            button.setProperty("state", state)
            button.style().unpolish(button)
            button.style().polish(button)
        self.stage_state_label.setText(
            f"当前：{STAGES[current_index][1]} · {Path(self.project_path).stem} · 项目已保存"
        )

    def begin_analysis(self, source: str) -> None:
        # 【v0.6 S3-5 / P1-9】分析进行中再次触发（重复点选目录、双击）不得让
        # AnalyzePage.start() 的 RuntimeError 冒到 Qt 事件循环里 —— 旧实现下
        # 用户看不到任何提示，而 goto("analyze") 已经切走了页面。
        worker = self.analyze_page.worker
        if worker is not None and worker.isRunning():
            self.status("分析正在进行中，请等待完成或先取消后再更换目录")
            return
        source_path = Path(source)
        if not source_path.is_dir():
            self.show_error("请选择存在的照片目录")
            return
        # 经命令表写入（N1：UI 不直接写库），并留下事件供任务中心/未来 Agent 订阅。
        result = self.commands.execute("photo.set_source_dir", {"source_dir": str(source_path)})
        if not result.ok:
            self.show_error(f"无法记录来源目录：{result.error.message if result.error else result}")
            return
        self.goto("analyze")
        # 【T-07】登记一个可取消任务，然后启动 worker。任务 id 记在实例上，
        # 以便进度/终态回调能对上同一条任务。
        self._analysis_task = self.tasks.register(_ANALYZE_TASK_NAME, cancelable=True)
        self.tasks.start(self._analysis_task, "准备分析…")
        self._pump_tasks()
        self.analyze_page.start(str(source_path), self.store.db_path)

    # ------------------------------------------------------------------
    # 【T-07】统一任务中心的接线
    # ------------------------------------------------------------------
    def _analysis_progress(self, phase: str, done: int, total: int) -> None:
        task = getattr(self, "_analysis_task", None)
        if task is None:
            return
        # total 未知时先不设，避免进度条给出假的百分比；一旦知道就补上。
        if total and not task.total:
            task.total = int(total)
        self.tasks.progress(task, done, str(phase))
        self._pump_tasks()

    def _analysis_finished(self, state: str, info) -> None:
        task = getattr(self, "_analysis_task", None)
        if task is None:
            return
        if state == "succeeded":
            self.tasks.succeed(task, info if isinstance(info, dict) else None)
        elif state == "cancelled":
            self.tasks.cancel(task)
        else:
            self.tasks.fail(task, info or "分析失败")
        self._analysis_task = None
        self._pump_tasks()

    def cancel_task(self, task_id: str) -> None:
        """请求取消一个任务。

        这里做两件事，顺序要紧：
          1. 通过 TaskCenter 置位 ``cancel_requested``（协议层，worker 会在
             安全点看到它并优雅收尾）；
          2. 若该任务对应分析 worker，同时调用 worker.cancel() 触发 Qt 的
             中断请求（双保险：pipeline 的 cancel_check 两个来源都会看）。
        """
        task = self.tasks.get(task_id)
        if task is None:
            return
        if not self.tasks.request_cancel(task):
            return
        worker = getattr(self.analyze_page, "worker", None)
        if task.name == _ANALYZE_TASK_NAME and worker is not None and worker.isRunning():
            worker.cancel()
        self.status(f"正在安全取消：{task.name}…")
        self._pump_tasks()

    def _pump_tasks(self) -> None:
        """把任务中心待发布的事件转成事件总线事件，并刷新面板。

        **必须在 UI 线程调用** —— 这样所有事件订阅者都在 UI 线程被回调，
        Qt 控件可以安全更新（worker 线程不得直接碰控件）。
        """
        self.tasks.drain_events(self.events.publish)
        self.task_panel.refresh()

    def _analysis_succeeded(self, result: dict) -> None:
        self.goto("review")
        # 重算之后旧后端记录已被覆盖：顺手刷新"旧库"入口，别让提示一直挂着。
        self.refresh_upgrade_notice(announce=False)
        # 照片数变了，更新注册表里的登记（入口页下次打开即最新）。
        self.register_current_project(touch=True)
        degradations = result.get("degradations") or []
        upgrade = result.get("upgrade") or {}
        details: list[str] = []
        if upgrade.get("recomputed"):
            # 【v0.6 S4-1 / G4】重算必须说清楚：否则用户只会看到"分数换了一套"
            # 却不知道为什么，也无从确认自己的复核有没有被保住。
            legacy = upgrade.get("legacy_backends") or []
            origin = f"（原后端：{'、'.join(legacy)}）" if legacy else ""
            details.append(
                f"模型已更新，指标已重算{origin}；"
                f"人工复核 {upgrade.get('decisions_preserved', 0)} 条已保留"
            )
        if degradations:
            # 【v0.6 S2-2 / G3】降级必须对用户可见：否则用户会以为"AI 都算过了"，
            # 实际少了美学/场景这一半信号，却拿着一份看起来完全正常的甄选结果。
            priority = {"critical": 2, "warning": 1, "info": 0}
            first = max(degradations, key=lambda item: priority.get(item.get("severity", "warning"), 1))
            level = {"critical": "严重", "warning": "警告", "info": "提示"}.get(first.get("severity"), "警告")
            detail = f"{level} · {first.get('stage', '未知阶段')}：{first.get('effect', '')}"
            if len(degradations) > 1:
                detail += f"（另有 {len(degradations) - 1} 项降级）"
            details.append(f"降级 · {detail}")
        head = "分析完成（降级）" if degradations else "分析完成"
        message = f"{head}：{result.get('total', 0)} 张"
        message += " · " + " · ".join(details) if details else "，等待人工复核"
        self.status(message)

    # ------------------------------------------------------------------
    # 【v0.6 S4-1 / G4】旧库升级提示 + 已复核清单导出（兜底）
    # ------------------------------------------------------------------
    def upgrade_report(self) -> dict:
        """读取本项目的旧库升级信息（读失败不阻断启动）。"""
        try:
            return self.store.upgrade_report()
        except Exception as error:          # noqa: BLE001 —— 提示失败不该拦住用户
            _log.warning("读取旧库升级信息失败（已忽略）：%s", error)
            return {}

    def refresh_upgrade_notice(self, *, announce: bool = True) -> None:
        """检测到旧库才显示升级入口，避免给新用户一个纯噪音按钮。"""
        report = self.upgrade_report()
        needs_attention = bool(report.get("needs_attention"))
        # 入口页下不显示项目级按钮；只有进了工作流才谈升级提示。
        visible = needs_attention and self.current_stage_name != LIBRARY_PAGE
        self.upgrade_button.setVisible(visible)
        if needs_attention and announce:
            self.status("检测到旧项目：点「旧库升级说明」查看升级、备份与重算详情")

    def show_upgrade_notice(self) -> None:
        report = self.upgrade_report()
        if not report.get("needs_attention"):
            self.status("项目为当前版本，无需升级")
            return
        box = QMessageBox(self)
        box.setWindowTitle("旧项目升级说明")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(self.store.upgrade_summary())
        box.setInformativeText(
            "人工复核结果不会丢失（备份也留在原项目旁）；指标会在下次分析时重算。"
        )
        export = box.addButton("导出已复核清单", QMessageBox.ButtonRole.ActionRole)
        box.addButton("知道了", QMessageBox.ButtonRole.AcceptRole)
        box.exec()
        if box.clickedButton() is export:
            self.export_review_list()

    def export_review_list(self, target: str | None = None) -> None:
        """导出人工复核清单（CSV）。target 为空时弹保存对话框。"""
        if target is None:
            default = str(Path(self.store.db_path).with_name("已复核清单.csv"))
            chosen, _ = QFileDialog.getSaveFileName(
                self, "导出已复核清单", default, "CSV 文件 (*.csv)"
            )
            if not chosen:
                return
            target = chosen
        try:
            result = self.store.export_decisions(target)
        except Exception as error:          # noqa: BLE001
            _log.exception("导出已复核清单失败：%s", target)
            self.show_error(f"导出已复核清单失败：{error}")
            return
        self.status(f"已导出 {result['rows']} 条人工复核记录 → {result['path']}")

    # ------------------------------------------------------------------
    # 【T-11】快捷键设置
    # ------------------------------------------------------------------
    @staticmethod
    def _open_user_settings():
        """打开全局设置库；失败时返回 ``None``（退化为"用默认键位、不记住改动"）。

        不能让"设置文件写不了"变成"程序起不来" —— 那是最糟的失败形态。
        """
        try:
            return UserSettings()
        except Exception as error:  # noqa: BLE001
            _log.warning("全局设置库不可用，快捷键将不持久化：%s", error)
            return None

    def open_settings_dialog(self) -> None:
        if self.user_settings is None:
            self.show_error("设置库不可用，快捷键无法保存（本次仍可按默认键位使用）")
            return
        updated = open_settings(self.shortcut_bindings, self)
        if updated is None:
            return
        try:
            self.shortcut_bindings = save_bindings(self.user_settings, updated)
        except ShortcutConflict as conflict:
            self.show_error(str(conflict))
            return
        self._apply_shortcut_bindings()

    def _apply_shortcut_bindings(self) -> None:
        """把新键位装到复核页上（重建页面代价太大，这里只重装 QAction）。"""
        page = self.review_page
        if page is None:
            return
        page.install_shortcuts(self.shortcut_bindings)
        self.status("快捷键已更新")

    def status(self, message: str) -> None:
        self.statusBar().showMessage(message)

    def show_error(self, message: str) -> None:
        self.statusBar().showMessage(f"错误：{message}")

    def closeEvent(self, event) -> None:
        worker = self.analyze_page.worker
        if worker is not None and worker.isRunning():
            task = getattr(self, "_analysis_task", None)
            if task is not None:
                self.tasks.request_cancel(task)
            worker.cancel()
            if not worker.wait(5000):
                self.show_error("分析仍在安全取消中，请稍后再退出")
                event.ignore()
                return
        if self.export_page is not None and not self.export_page.shutdown():
            self.show_error("导出仍在完成当前文件，请稍后再退出")
            event.ignore()
            return
        if self.review_page is not None:
            self.review_page.shutdown()
        self.store.close()
        self.registry.close()
        super().closeEvent(event)
