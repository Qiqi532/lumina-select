"""Audit locked runtime dependencies and generate third-party notices."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
import re
import sys
import tomllib
from typing import Mapping


_NORMALIZE_RE = re.compile(r"[-_.]+")
_LOCKED_RE = re.compile(r"^([A-Za-z0-9_.-]+)==([^;\s\\]+)")


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
) -> list[AuditIssue]:
    locked = locked_packages(requirements_text)
    denied = {normalize_name(name) for name in policy.get("deny_packages", [])}
    manual = {
        normalize_name(name) for name in policy.get("manual_review_packages", [])
    }
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
    return issues


def render_notices(
    requirements_text: str, installed: Mapping[str, PackageMetadata]
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
    )
    args.notices.write_text(
        render_notices(requirements_text, installed), encoding="utf-8", newline="\n"
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
