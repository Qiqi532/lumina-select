"""T-05 / M0-5 —— 项目入口页 + Shell 骨架 + 跨会话零丢失。

核心验收（PLAN.md T-05）：
    关闭 App 重开，从入口页回到该项目，``star`` / ``label`` / ``decision_source``
    **零丢失**；有回归用例。
"""
from __future__ import annotations

import sqlite3

from pathlib import Path

from PySide6.QtWidgets import QApplication

from engine.catalog_registry import CatalogRegistry
from ui.main_window import MainWindow
from ui.pages.library_page import LibraryPage, ProjectCard


def _registry(tmp_path) -> CatalogRegistry:
    return CatalogRegistry(tmp_path / "catalogs.db")


def _make_project(tmp_path, name: str = "婚礼", *, photos: int = 0, star: int = 0,
                  label: str = "", decision_source: str = "") -> str:
    """造一个真实的项目库（含本项目 schema 的关键列），返回库路径字符串。"""
    db = tmp_path / name / "data" / "cull.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db)
    connection.execute(
        "CREATE TABLE photos (path TEXT PRIMARY KEY, fname TEXT, star INT DEFAULT 0, "
        "label TEXT DEFAULT '', decision_source TEXT DEFAULT '', is_waste INT DEFAULT 0)"
    )
    for index in range(photos):
        connection.execute(
            "INSERT INTO photos (path, fname, star, label, decision_source) VALUES (?,?,?,?,?)",
            (f"C:/p/{name}/IMG_{index:04d}.CR3", f"IMG_{index:04d}.CR3", star, label, decision_source),
        )
    connection.commit()
    connection.close()
    return str(db)


def _window(qtbot, tmp_path, *, start_in_library: bool = True):
    window = MainWindow(
        registry=_registry(tmp_path),
        start_in_library=start_in_library,
    )
    qtbot.addWidget(window)
    return window


# ---------------------------------------------------------------------------
# 入口页渲染
# ---------------------------------------------------------------------------
def test_library_page_renders_cards_for_registered_projects(qtbot, tmp_path):
    window = _window(qtbot, tmp_path)
    project = _make_project(tmp_path, "婚礼", photos=3)
    window.registry.register(project, photo_count=3)

    window.show_library()

    assert isinstance(window.current_page, LibraryPage)
    assert len(window.library_page.cards) == 1
    assert window.library_page.cards[0].entry.name == "婚礼"


def test_library_page_shows_empty_hint_when_no_projects(qtbot, tmp_path):
    window = _window(qtbot, tmp_path)

    window.show_library()

    assert window.library_page.cards == []
    assert window.library_page.empty_hint.isVisibleTo(window.library_page) is True


def test_boot_does_not_register_empty_default_project(qtbot, tmp_path):
    """★ 幽灵卡片回归：启动时的空默认库不得进入口页。

    否则用户第一次打开 App（或上次留下的空壳默认库还在时），会看到一张
    指向空库、名字还是仓库目录名的卡片，莫名其妙。
    """
    window = _window(qtbot, tmp_path)

    window.show_library()

    assert window.library_page.cards == [], "空默认项目不应被登记"
    assert window.registry.count() == 0


def test_empty_default_project_is_skipped_on_second_boot(qtbot, tmp_path):
    """★ 第二次启动回归：库文件已存在但仍为空，照样不得登记。

    这是最容易漏的一种：首启时 ``PhotoStore`` 会把默认库建出来，所以从第二次
    启动起"库文件存在"恒为真。若拿"文件是否预先存在"当判据，空默认库就会
    永久变成一张幽灵卡片。判据必须只看照片数。
    """
    from engine import config

    default_db = Path(config.DEFAULT_DB)
    default_db.parent.mkdir(parents=True, exist_ok=True)
    # 模拟"上一次启动留下的空库"
    connection = sqlite3.connect(default_db)
    connection.execute("CREATE TABLE photos (path TEXT PRIMARY KEY)")
    connection.commit()
    connection.close()
    assert default_db.is_file()

    window = _window(qtbot, tmp_path)
    window.show_library()

    assert window.library_page.cards == [], "空的默认库第二次启动也不应被登记"
    assert window.registry.count() == 0


def test_existing_default_project_with_photos_is_registered(qtbot, tmp_path):
    """反过来：默认库若真有内容（被用过），仍应出现在入口页。"""
    from engine import config

    default_db = Path(config.DEFAULT_DB)
    default_db.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(default_db)
    connection.execute("CREATE TABLE photos (path TEXT PRIMARY KEY)")
    connection.execute("INSERT INTO photos VALUES ('C:/p/IMG_0001.CR3')")
    connection.commit()
    connection.close()

    window = _window(qtbot, tmp_path)
    window.show_library()

    assert len(window.library_page.cards) == 1


def test_library_hides_workflow_chrome_and_back_button_returns(qtbot, tmp_path):
    window = _window(qtbot, tmp_path)
    project = _make_project(tmp_path, "婚礼", photos=1)
    window.open_project(project)
    QApplication.processEvents()

    # 工作流态：阶段按钮可见、返回按钮可见
    assert all(button.isVisibleTo(window) for button in window.stage_buttons)
    assert window.back_button.isVisibleTo(window) is True

    window.back_button.click()
    QApplication.processEvents()

    # 入口页态：阶段按钮与返回按钮都应收起
    assert window.current_stage_name == "library"
    assert not any(button.isVisibleTo(window) for button in window.stage_buttons)
    assert window.back_button.isVisibleTo(window) is False


# ---------------------------------------------------------------------------
# 打开项目
# ---------------------------------------------------------------------------
def test_open_project_switches_store_and_enters_import(qtbot, tmp_path):
    window = _window(qtbot, tmp_path)
    project = _make_project(tmp_path, "婚礼", photos=2)

    window.open_project(project)
    QApplication.processEvents()

    assert window.project_path == project
    assert window.store.db_path == project
    assert window.current_stage_name == "import"


def test_open_project_by_folder_resolves_data_cull_db(qtbot, tmp_path):
    """入口页"从文件夹打开…"给的是目录 —— 必须收敛到 <dir>/data/cull.db。"""
    window = _window(qtbot, tmp_path)
    folder = tmp_path / "毕业季"

    window.open_project(str(folder))
    QApplication.processEvents()

    assert window.project_path == str(folder / "data" / "cull.db")


def test_switching_projects_rebinds_project_pages(qtbot, tmp_path):
    """复核/导出页在构造时捕获 store，切项目必须换成绑定新 store 的实例。"""
    window = _window(qtbot, tmp_path)
    first = _make_project(tmp_path, "A", photos=1)
    second = _make_project(tmp_path, "B", photos=1)

    window.open_project(first)
    QApplication.processEvents()
    first_review = window.review_page

    window.open_project(second)
    QApplication.processEvents()

    assert window.review_page is not first_review
    assert window.review_page.review.store.db_path == second
    assert window.export_page.store.db_path == second


def test_open_project_twice_is_idempotent(qtbot, tmp_path):
    """重复打开同一项目不应重建页面（避免无谓抖动）。"""
    window = _window(qtbot, tmp_path)
    project = _make_project(tmp_path, "A", photos=1)

    window.open_project(project)
    QApplication.processEvents()
    page = window.review_page

    window.open_project(project)
    QApplication.processEvents()

    assert window.review_page is page


# ---------------------------------------------------------------------------
# ★ 核心验收：关闭重开，人工决策零丢失
# ---------------------------------------------------------------------------
def test_star_label_decision_survive_close_and_reopen(qtbot, tmp_path):
    """T-05 主验收：关掉 App 再从入口页打开同一项目，人工决策必须零丢失。"""
    project = _make_project(tmp_path, "婚礼", photos=3)
    registry_path = tmp_path / "catalogs.db"

    # --- 第一次会话：写人工决策 ---
    first_registry = CatalogRegistry(registry_path)
    window = MainWindow(registry=first_registry, start_in_library=True)
    qtbot.addWidget(window)
    window.open_project(project)
    QApplication.processEvents()
    window.store.upsert_photos_batch([
        {"path": "C:/p/婚礼/IMG_0000.CR3", "star": 5, "label": "P",
         "decision_source": "manual"},
        {"path": "C:/p/婚礼/IMG_0001.CR3", "star": 2, "label": "X",
         "decision_source": "manual"},
    ])
    window.close()
    QApplication.processEvents()

    # --- 第二次会话：全新窗口，从入口页回到该项目 ---
    second_registry = CatalogRegistry(registry_path)
    reopened = MainWindow(registry=second_registry, start_in_library=True)
    qtbot.addWidget(reopened)
    # 入口页里应该有这个项目
    assert any(entry.path == project for entry in reopened.library_entries())
    reopened.open_project(project)
    QApplication.processEvents()

    rows = {row["path"]: row for row in reopened.store.all_photos()}
    assert rows["C:/p/婚礼/IMG_0000.CR3"]["star"] == 5
    assert rows["C:/p/婚礼/IMG_0000.CR3"]["label"] == "P"
    assert rows["C:/p/婚礼/IMG_0000.CR3"]["decision_source"] == "manual"
    assert rows["C:/p/婚礼/IMG_0001.CR3"]["star"] == 2
    assert rows["C:/p/婚礼/IMG_0001.CR3"]["label"] == "X"
    reopened.close()
    QApplication.processEvents()


def test_opening_project_updates_registry_recent_order(qtbot, tmp_path):
    window = _window(qtbot, tmp_path)
    a = _make_project(tmp_path, "A", photos=1)
    b = _make_project(tmp_path, "B", photos=1)
    window.registry.register(a)
    window.registry.register(b)

    window.open_project(a)
    QApplication.processEvents()

    assert window.registry.entries()[0].path == a, "刚打开的项目应排在最前"


# ---------------------------------------------------------------------------
# 新建 / 移除
# ---------------------------------------------------------------------------
def test_create_project_makes_canonical_db_and_registers(qtbot, tmp_path):
    window = _window(qtbot, tmp_path)
    folder = tmp_path / "新项目"

    window.create_project(str(folder))
    QApplication.processEvents()

    canonical = folder / "data" / "cull.db"
    assert canonical.is_file()
    assert window.project_path == str(canonical)
    assert window.registry.get(canonical) is not None


def test_forget_project_removes_card_without_deleting_library(qtbot, tmp_path):
    window = _window(qtbot, tmp_path)
    project = _make_project(tmp_path, "婚礼", photos=2)
    window.registry.register(project, photo_count=2)
    window.show_library()
    assert len(window.library_page.cards) == 1

    window.forget_project(project)
    QApplication.processEvents()

    assert len(window.library_page.cards) == 0
    assert (tmp_path / "婚礼" / "data" / "cull.db").is_file(), "移除登记不得删库"
    assert "未被删除" in window.statusBar().currentMessage()


def test_library_page_signals_are_wired_to_shell(qtbot, tmp_path):
    """入口页只发意图，不自己开库 —— 信号必须由 Shell 接住。"""
    window = _window(qtbot, tmp_path)
    project = _make_project(tmp_path, "婚礼", photos=1)

    window.library_page.open_project.emit(project)
    QApplication.processEvents()

    assert window.project_path == project


# ---------------------------------------------------------------------------
# 既有行为不回归
# ---------------------------------------------------------------------------
def test_workflow_goto_still_works_after_library_refactor(qtbot, tmp_path):
    window = _window(qtbot, tmp_path)
    project = _make_project(tmp_path, "婚礼", photos=1)
    window.open_project(project)
    QApplication.processEvents()

    for stage in ("analyze", "review", "export", "import"):
        window.goto(stage)
        assert window.current_stage_name == stage

    assert [button.text() for button in window.stage_buttons] == [
        "1 导入", "2 分析", "3 复核", "4 导出到 LR"
    ]


def test_legacy_project_db_argument_still_skips_library(qtbot, tmp_path):
    """显式给 project_db 时不进入口页 —— 兼容既有调用与测试。"""
    window = MainWindow(project_db=tmp_path / "legacy.db", registry=_registry(tmp_path))
    qtbot.addWidget(window)

    assert window.current_stage_name == "import"
    window.close()


def test_goto_unknown_stage_raises(qtbot, tmp_path):
    window = _window(qtbot, tmp_path)
    window.open_project(_make_project(tmp_path, "A", photos=1))
    QApplication.processEvents()

    try:
        window.goto("nope")
    except ValueError as error:
        assert "nope" in str(error)
    else:  # pragma: no cover
        raise AssertionError("未知阶段必须报错")


def test_close_closes_registry_and_store(qtbot, tmp_path):
    window = _window(qtbot, tmp_path)

    window.close()
    QApplication.processEvents()

    # 关闭后再用连接应抛异常（说明确实关掉了）
    try:
        window.registry.count()
    except sqlite3.ProgrammingError:
        pass
    else:  # pragma: no cover
        raise AssertionError("注册表连接应在关闭窗口时释放")


# ---------------------------------------------------------------------------
# 卡片组件
# ---------------------------------------------------------------------------
def test_project_card_marks_missing_library(qtbot, tmp_path):
    from PySide6.QtWidgets import QLabel

    from engine.catalog_registry import CatalogEntry

    entry = CatalogEntry(
        path=str(tmp_path / "gone" / "data" / "cull.db"),
        name="已丢失", photo_count=0, opened_at=0.0, created_at=0.0, last_seen_dir=None,
    )
    card = ProjectCard(entry)
    qtbot.addWidget(card)

    assert any(
        label.objectName() == "CardWarn" for label in card.findChildren(QLabel)
    ), "库文件不在原位置时卡片必须给出可见警示"


def test_project_card_shows_photo_count_and_recent_time(qtbot, tmp_path):
    import time

    from PySide6.QtWidgets import QLabel

    from engine.catalog_registry import CatalogEntry

    entry = CatalogEntry(
        path=str(tmp_path / "p" / "data" / "cull.db"),
        name="婚礼", photo_count=128, opened_at=time.time() - 120,
        created_at=0.0, last_seen_dir=None,
    )
    card = ProjectCard(entry)
    qtbot.addWidget(card)

    meta = [label for label in card.findChildren(QLabel) if label.objectName() == "CardMeta"]
    assert meta, "卡片必须有元信息行"
    assert "128 张照片" in meta[0].text()
