"""Audit locked runtime dependencies and generate third-party notices."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from importlib import metadata
import json
from pathlib import Path
import re
import sys
import tomllib
from typing import Mapping


_NORMALIZE_RE = re.compile(r"[-_.]+")
_LOCKED_RE = re.compile(r"^([A-Za-z0-9_.-]+)==([^;\s\\]+)")
_LGPL_RE = re.compile(r"lgpl", re.IGNORECASE)

# LGPLv3 义务：随发行包附带 LGPL-3.0 全文；且因 LGPLv3 以引用方式并入 GPLv3
# （LGPL-3.0 §3(b)/§4(b) 要求 "accompany the object code with a copy of the GNU GPL"），
# 必须同时附带 GPL-3.0 全文。文件名 → 期望在发行目录中的位置。
_LGPL_BUNDLED_FILES = ("LGPL-3.0.txt", "GPL-3.0.txt")
# LGPLv3 §4(d)(0)「安装信息」：需提供让用户替换 Qt 库的说明。
_LGPL_RELINKING_FILE = "RELINKING.md"
# 发行形态：onedir（目录）而非 onefile（单文件），以保证 Qt 库可被就地替换。
# 判定方式：产物必须是目录，且目录内存在 PyInstaller onedir 的 `_internal` 子目录。
_ONEDIR_MARKER = "_internal"


def normalize_name(name: str) -> str:
    return _NORMALIZE_RE.sub("-", name).casefold()


@dataclass(frozen=True, slots=True)
class PackageMetadata:
    name: str
    version: str
    license_text: str
    home_page: str


@dataclass(frozen=True, slots=True)
class AuditIssue:
    code: str
    package: str
    message: str


def lgpl_packages(
    policy: Mapping[str, list[str]],
) -> set[str]:
    """策略中声明的 LGPL 组件（发行时必须满足 LGPL 义务）。

    与 ``manual_review_packages``（人工复核）不同：这里聚焦"必须随包附全文 +
    保持可替换性"这一类 copyleft 义务。策略通过 ``lgpl_packages`` 键显式声明，
    避免把人工复核包误判为 LGPL。
    """
    return {normalize_name(name) for name in policy.get("lgpl_packages", [])}


def audit_lgpl_bundle(
    bundle_dir: Path | None,
) -> list[AuditIssue]:
    """校验发行目录满足 LGPLv3 合规的硬性要求。

    ``bundle_dir`` 为 None 时跳过（开发期不构建发行包的情形）。检查项：
    1. 附带 LGPL-3.0 全文与 GPL-3.0 全文（LGPL-3.0 §3(b)/§4(b)）；
    2. 附带 §4(d)(0) 要求的「安装信息」``RELINKING.md``（说明如何替换 Qt 库）；
    3. 产物是 **onedir 目录**（含 ``_internal`` 子目录），而非 onefile 单文件
       —— 只有 onedir 才能让用户就地替换 Qt 库。
    """
    if bundle_dir is None:
        return []
    issues: list[AuditIssue] = []
    if not bundle_dir.is_dir():
        issues.append(
            AuditIssue(
                "not_onedir_release",
                str(bundle_dir),
                f"LGPLv3 可替换性要求 onedir 目录形态，但 {bundle_dir} 不是目录"
                "（禁止 onefile 单文件发行）",
            )
        )
        return issues
    if not (bundle_dir / _ONEDIR_MARKER).is_dir():
        issues.append(
            AuditIssue(
                "not_onedir_release",
                str(bundle_dir),
                f"未在 {bundle_dir} 找到 onedir 标记子目录 `{_ONEDIR_MARKER}`，"
                "无法确认 Qt 库以独立文件存在（可替换性存疑）",
            )
        )
    for filename in (*_LGPL_BUNDLED_FILES, _LGPL_RELINKING_FILE):
        # PyInstaller onedir 把 datas 放进 `_internal/`；Inno Setup 再把它提升到 `{app}`。
        # 因此两个位置都算通过：构建产物里在 `_internal/`，安装后在根目录。
        if not (
            (bundle_dir / filename).is_file()
            or (bundle_dir / _ONEDIR_MARKER / filename).is_file()
        ):
            issues.append(
                AuditIssue(
                    "missing_lgpl_text",
                    filename,
                    f"LGPLv3 合规要求发行目录附带 {filename}"
                    f"（未在 {bundle_dir} 或 {bundle_dir / _ONEDIR_MARKER} 找到）",
                )
            )
    return issues


def locked_packages(requirements_text: str) -> dict[str, str]:
    packages: dict[str, str] = {}
    for raw_line in requirements_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", "--")):
            continue
        match = _LOCKED_RE.match(line)
        if match is None:
            raise ValueError(f"requirement is not exactly locked: {line}")
        packages[normalize_name(match.group(1))] = match.group(2)
    return packages


def installed_metadata() -> dict[str, PackageMetadata]:
    packages: dict[str, PackageMetadata] = {}
    for distribution in metadata.distributions():
        name = distribution.metadata.get("Name") or "unknown"
        license_text = (
            distribution.metadata.get("License-Expression")
            or distribution.metadata.get("License")
            or "Not declared"
        )
        home_page = (
            distribution.metadata.get("Home-page")
            or distribution.metadata.get("Project-URL")
            or "Not declared"
        )
        packages[normalize_name(name)] = PackageMetadata(
            name=name,
            version=distribution.version,
            license_text=license_text,
            home_page=home_page,
        )
    return packages


def audit_requirements(
    requirements_text: str,
    policy: Mapping[str, list[str]],
    installed: Mapping[str, PackageMetadata],
    *,
    release: bool = False,
    approvals: Mapping[str, Mapping[str, str]] | None = None,
    bundle_dir: Path | None = None,
) -> list[AuditIssue]:
    locked = locked_packages(requirements_text)
    denied = {normalize_name(name) for name in policy.get("deny_packages", [])}
    manual = {
        normalize_name(name) for name in policy.get("manual_review_packages", [])
    }
    lgpl = lgpl_packages(policy)
    terms = [term.casefold() for term in policy.get("deny_license_terms", [])]
    normalized_approvals = {
        normalize_name(name): value for name, value in (approvals or {}).items()
    }
    issues: list[AuditIssue] = []

    for package in sorted(locked):
        if package in denied:
            issues.append(
                AuditIssue("denied_package", package, "package is denied by policy")
            )
        package_metadata = installed.get(package)
        if package_metadata is not None:
            license_text = package_metadata.license_text.casefold()
            if any(term in license_text for term in terms):
                issues.append(
                    AuditIssue(
                        "denied_license",
                        package,
                        "installed license metadata contains a denied term",
                    )
                )
        # LGPL 组件：仅在确有安装元数据时校验声明，避免对"未安装的元包"误报。
        # 发行期不在此处单列 obligation —— 义务是否已履行，由 audit_lgpl_bundle
        # 对**发行目录实体**做校验（更真实，且不产生"永远无法清零"的噪声 issue）。
        if package in lgpl and package_metadata is not None:
            declared = package_metadata.license_text
            if not _LGPL_RE.search(declared):
                issues.append(
                    AuditIssue(
                        "lgpl_declaration_missing",
                        package,
                        "策略声明为 LGPL 组件，但安装元数据的许可证未体现 LGPL",
                    )
                )
        if release and package in manual:
            approval = normalized_approvals.get(package, {})
            if not all(approval.get(field, "").strip() for field in (
                "basis",
                "reviewer",
                "reviewed_on",
            )):
                issues.append(
                    AuditIssue(
                        "missing_manual_approval",
                        package,
                        "release mode requires an auditable manual approval",
                    )
                )
    issues.extend(audit_lgpl_bundle(bundle_dir if release else None))
    return issues


def render_notices(
    requirements_text: str, installed: Mapping[str, PackageMetadata],
    *, exiftool_lock: Mapping[str, object] | None = None,
) -> str:
    locked = locked_packages(requirements_text)
    lines = [
        "# Third-Party Notices",
        "",
        "Generated from the locked runtime requirements and installed metadata.",
        "",
        "| Package | Version | License | Project |",
        "| --- | --- | --- | --- |",
    ]
    for package in sorted(locked):
        item = installed.get(package)
        if item is None:
            lines.append(
                f"| {package} | {locked[package]} | Not installed | Not available |"
            )
            continue
        license_text = _notice_field(item.license_text)
        home_page = _notice_field(item.home_page)
        lines.append(
            f"| {item.name} | {item.version} | {license_text} | {home_page} |"
        )
    if exiftool_lock is not None:
        lines.append(
            f"| ExifTool | {exiftool_lock['version']} | "
            f"{_notice_field(str(exiftool_lock['license_choice']))} | "
            f"{exiftool_lock['official_project_url']} |"
        )
        lines.extend(("", "ExifTool Windows archive SHA-256: "
                      f"`{exiftool_lock['archive_sha256']}`."))
    lines.append("")
    return "\n".join(lines)


def _notice_field(value: str, limit: int = 160) -> str:
    first_line = next((line.strip() for line in value.splitlines() if line.strip()), "Not declared")
    compact = " ".join(first_line.split()).replace("|", "\\|")
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"


def _load_toml(path: Path) -> dict:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--requirements", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--release", action="store_true")
    parser.add_argument(
        "--approvals", type=Path, default=Path("release/license-approvals.toml")
    )
    parser.add_argument("--notices", type=Path, default=Path("THIRD_PARTY_NOTICES.md"))
    parser.add_argument("--exiftool-lock", type=Path, default=Path("release/exiftool.lock.json"))
    parser.add_argument(
        "--bundle-dir",
        type=Path,
        default=None,
        help="发行目录（release 模式下校验其中是否附带 LGPL/GPL 全文）",
    )
    args = parser.parse_args(argv)

    requirements_text = args.requirements.read_text(encoding="utf-8")
    policy = _load_toml(args.policy)
    installed = installed_metadata()
    approvals = _load_toml(args.approvals) if args.approvals.is_file() else {}
    issues = audit_requirements(
        requirements_text,
        policy,
        installed,
        release=args.release,
        approvals=approvals,
        bundle_dir=args.bundle_dir,
    )
    exiftool_lock = json.loads(args.exiftool_lock.read_text(encoding="utf-8"))
    args.notices.write_text(
        render_notices(requirements_text, installed, exiftool_lock=exiftool_lock),
        encoding="utf-8", newline="\n"
    )
    for issue in issues:
        print(f"{issue.code}: {issue.package}: {issue.message}", file=sys.stderr)
    if issues:
        return 1
    mode = "release" if args.release else "development"
    print(f"License {mode} check passed for {len(locked_packages(requirements_text))} packages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
