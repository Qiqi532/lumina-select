from __future__ import annotations

from scripts.check_release_licenses import (
    PackageMetadata,
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
    "manual_review_packages": ["PyQt6", "PyQt6-Qt6", "PyQt6-sip"],
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


def test_development_allows_pyqt_manual_review():
    issues = audit_requirements(
        "PyQt6==6.8.1\n",
        POLICY,
        {"pyqt6": _package("PyQt6", "GPL-3.0-only")},
    )

    assert issues == []


def test_release_requires_real_pyqt_approval():
    issues = audit_requirements(
        "PyQt6==6.8.1\n",
        POLICY,
        {"pyqt6": _package("PyQt6", "GPL-3.0-only")},
        release=True,
        approvals={},
    )

    assert {issue.code for issue in issues} >= {"missing_manual_approval"}


def test_release_accepts_complete_auditable_approval():
    approvals = {
        "pyqt6": {
            "basis": "Commercial agreement LS-2026-001",
            "reviewer": "Legal reviewer",
            "reviewed_on": "2026-09-12",
        }
    }

    issues = audit_requirements(
        "PyQt6==6.8.1\n",
        POLICY,
        {"pyqt6": _package("PyQt6", "Commercial")},
        release=True,
        approvals=approvals,
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
