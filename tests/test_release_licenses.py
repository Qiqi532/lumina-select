from __future__ import annotations

from pathlib import Path

from scripts.check_release_licenses import (
    PackageMetadata,
    audit_lgpl_bundle,
    audit_requirements,
    render_notices,
)


POLICY = {
    "deny_packages": ["pyiqa"],
    "deny_license_terms": [
        "polyform noncommercial",
        "cc-by-nc",
        "non-commercial",
        "research only",
    ],
    "manual_review_packages": ["PySide6-Essentials", "shiboken6"],
    "lgpl_packages": ["PySide6-Essentials", "shiboken6"],
}


def _package(name: str, license_text: str = "MIT") -> PackageMetadata:
    return PackageMetadata(
        name=name,
        version="1.0.0",
        license_text=license_text,
        home_page="https://example.invalid",
    )


def test_denied_package_in_lock_file_fails():
    issues = audit_requirements(
        "numpy==1.26.4\npyiqa==0.1.16\n",
        POLICY,
        {"numpy": _package("numpy"), "pyiqa": _package("pyiqa")},
    )

    assert {issue.code for issue in issues} >= {"denied_package"}


def test_denied_license_terms_fail():
    for term in POLICY["deny_license_terms"]:
        issues = audit_requirements(
            "sample==1.0.0\n",
            POLICY,
            {"sample": _package("sample", f"License: {term}")},
        )
        assert {issue.code for issue in issues} >= {"denied_license"}


def test_development_allows_pyside_manual_review():
    issues = audit_requirements(
        "PySide6-Essentials==6.8.1\n",
        POLICY,
        {"pyside6-essentials": _package("PySide6-Essentials", "LGPL")},
    )

    assert issues == []


def test_release_requires_real_pyside_approval():
    issues = audit_requirements(
        "PySide6-Essentials==6.8.1\n",
        POLICY,
        {"pyside6-essentials": _package("PySide6-Essentials", "LGPL")},
        release=True,
        approvals={},
    )

    assert {issue.code for issue in issues} >= {"missing_manual_approval"}


def test_release_accepts_complete_auditable_approval():
    approvals = {
        "pyside6-essentials": {
            "basis": "LGPLv3 obligations satisfied: full license text shipped, onedir kept",
            "reviewer": "Legal reviewer",
            "reviewed_on": "2026-10-14",
        }
    }

    issues = audit_requirements(
        "PySide6-Essentials==6.8.1\n",
        POLICY,
        {"pyside6-essentials": _package("PySide6-Essentials", "LGPL")},
        release=True,
        approvals=approvals,
    )

    assert issues == []


def test_lgpl_package_without_declared_license_is_flagged():
    issues = audit_requirements(
        "PySide6-Essentials==6.8.1\n",
        POLICY,
        {"pyside6-essentials": _package("PySide6-Essentials", "MIT")},
    )

    assert {issue.code for issue in issues} >= {"lgpl_declaration_missing"}


def _make_onedir_bundle(root: Path, *, texts: tuple[str, ...] = ()) -> Path:
    """构造一个最小 onedir 发行目录（含 `_internal` 标记），可选地放入许可文件。"""
    bundle = root / "光影选片助手"
    (bundle / "_internal").mkdir(parents=True)
    for name in texts:
        (bundle / name).write_text(f"{name} text", encoding="utf-8")
    return bundle


def _make_internal_only_bundle(root: Path) -> Path:
    """模拟真实 PyInstaller 产物：datas 落在 `_internal/` 而非根目录。"""
    bundle = root / "光影选片助手"
    internal = bundle / "_internal"
    internal.mkdir(parents=True)
    for name in ("LGPL-3.0.txt", "GPL-3.0.txt", "RELINKING.md"):
        (internal / name).write_text("text", encoding="utf-8")
    return bundle


def test_lgpl_bundle_reports_missing_license_texts(tmp_path: Path):
    bundle = _make_onedir_bundle(tmp_path)

    issues = audit_lgpl_bundle(bundle)

    assert {issue.code for issue in issues} == {"missing_lgpl_text"}
    assert {issue.package for issue in issues} == {
        "LGPL-3.0.txt",
        "GPL-3.0.txt",
        "RELINKING.md",
    }


def test_lgpl_bundle_passes_when_texts_land_in_internal(tmp_path: Path):
    """真实 PyInstaller onedir 布局：datas 在 `_internal/`，同样满足合规。"""
    bundle = _make_internal_only_bundle(tmp_path)

    assert audit_lgpl_bundle(bundle) == []


def test_lgpl_bundle_flags_non_directory_bundle(tmp_path: Path):
    """onefile 单文件产物（不是目录）必须被判为不满足可替换性。"""
    onefile = tmp_path / "光影选片助手.exe"
    onefile.write_bytes(b"MZ")

    issues = audit_lgpl_bundle(onefile)

    assert {issue.code for issue in issues} == {"not_onedir_release"}


def test_lgpl_bundle_flags_missing_onedir_marker(tmp_path: Path):
    """目录存在但不是 onedir 结构（无 `_internal`）时同样报警。"""
    bundle = tmp_path / "光影选片助手"
    bundle.mkdir()
    for name in ("LGPL-3.0.txt", "GPL-3.0.txt", "RELINKING.md"):
        (bundle / name).write_text("text", encoding="utf-8")

    issues = audit_lgpl_bundle(bundle)

    assert {issue.code for issue in issues} == {"not_onedir_release"}


def test_lgpl_bundle_passes_when_texts_and_relinking_present(tmp_path: Path):
    bundle = _make_onedir_bundle(
        tmp_path, texts=("LGPL-3.0.txt", "GPL-3.0.txt", "RELINKING.md")
    )

    assert audit_lgpl_bundle(bundle) == []


def test_release_mode_requires_actual_lgpl_fulltext_in_bundle(tmp_path: Path):
    """发行模式：即使人工批准齐备，发行目录缺 LGPL/GPL 全文仍必须报错。"""
    approvals = {
        "pyside6-essentials": {
            "basis": "LGPLv3 obligations satisfied",
            "reviewer": "Legal reviewer",
            "reviewed_on": "2026-10-14",
        }
    }
    bundle = _make_onedir_bundle(tmp_path)

    issues = audit_requirements(
        "PySide6-Essentials==6.8.1\n",
        POLICY,
        {"pyside6-essentials": _package("PySide6-Essentials", "LGPL")},
        release=True,
        approvals=approvals,
        bundle_dir=bundle,
    )

    assert {issue.code for issue in issues} == {"missing_lgpl_text"}
    assert {issue.package for issue in issues} == {
        "LGPL-3.0.txt",
        "GPL-3.0.txt",
        "RELINKING.md",
    }


def test_release_mode_passes_when_approval_and_fulltext_present(tmp_path: Path):
    bundle = _make_onedir_bundle(
        tmp_path, texts=("LGPL-3.0.txt", "GPL-3.0.txt", "RELINKING.md")
    )
    approvals = {
        "pyside6-essentials": {
            "basis": "LGPLv3 obligations satisfied",
            "reviewer": "Legal reviewer",
            "reviewed_on": "2026-10-14",
        }
    }

    issues = audit_requirements(
        "PySide6-Essentials==6.8.1\n",
        POLICY,
        {"pyside6-essentials": _package("PySide6-Essentials", "LGPL")},
        release=True,
        approvals=approvals,
        bundle_dir=bundle,
    )

    assert issues == []


def test_notices_do_not_embed_full_license_bodies():
    long_license = "Apache-2.0\n" + ("full license body " * 100)

    notices = render_notices(
        "sample==1.0.0\n", {"sample": _package("sample", long_license)}
    )

    package_row = next(line for line in notices.splitlines() if line.startswith("| sample"))
    assert "full license body" not in package_row
    assert len(package_row) < 300


def test_notices_include_locked_exiftool_runtime():
    notices = render_notices(
        "sample==1.0.0\n",
        {"sample": _package("sample")},
        exiftool_lock={
            "version": "13.59",
            "archive_sha256": "a" * 64,
            "license_choice": "Artistic License 1.0 or GNU GPL version 1 or later",
            "official_project_url": "https://exiftool.org/",
        },
    )
    assert "ExifTool" in notices
    assert "13.59" in notices
    assert "a" * 64 in notices
