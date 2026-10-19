"""Run an onedir build outside the source tree and audit bundled dependencies."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.inference import DEFAULT_BACKEND  # noqa: E402


def audit_bundle(bundle: Path) -> list[str]:
    """审计冻结产物：必须含 torch/transformers，且绝不能混入 pyiqa。

    【v0.6 S2-1】原实现接受 backend 参数、分标准/轻量两套规则。轻量版下线后
    只剩一套规则，参数一并去掉 —— 留着它就等于留着一个"可以审计轻量产物"的假接口。

    【T-02】追加 LGPLv3 合规审计：onedir 形态 + 发行根目录含 LGPL/GPL 全文与
    RELINKING.md。这些是 T-02 的验收项（发行产物含 LGPL 全文、形态为 onedir）。
    """
    internal = bundle / "_internal"
    names = {path.name.casefold() for path in internal.iterdir()} if internal.is_dir() else set()
    failures = ["pyiqa"] if "pyiqa" in names else []
    failures.extend(f"missing:{name}" for name in ("torch", "transformers") if name not in names)
    # LGPLv3 合规物料：PyInstaller onedir 构建产物把它放进 `_internal/`
    # （Inno Setup 安装后再提升到 {app} 根目录）。两处任一存在即算通过。
    if not internal.is_dir():
        failures.append("not_onedir:missing _internal")
    for name in ("LGPL-3.0.txt", "GPL-3.0.txt", "RELINKING.md"):
        if not (
            (bundle / name).is_file() or (internal / name).is_file()
        ):
            failures.append(f"missing_lgpl:{name}")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=240)
    args = parser.parse_args(argv)
    executable = args.exe.resolve()
    if not executable.is_file():
        print(f"missing executable: {executable}", file=sys.stderr)
        return 1
    forbidden = audit_bundle(executable.parent)
    if forbidden:
        print(f"bundle audit failed: {', '.join(forbidden)}", file=sys.stderr)
        return 1
    with tempfile.TemporaryDirectory(prefix="lumina-detached-dist-") as directory:
        report_path = Path(directory) / "report.json"
        environment = os.environ.copy()
        environment.update({
            "QT_QPA_PLATFORM": "offscreen",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_HOME": str(Path(directory) / "empty_hf_home"),
            "HF_HUB_CACHE": str(Path(directory) / "empty_hf_home" / "hub"),
            "TORCH_HOME": str(Path(directory) / "empty_torch_home"),
        })
        try:
            process = subprocess.run(
                [str(executable), "--smoke-dist", str(report_path)],
                cwd=directory,
                env=environment,
                timeout=args.timeout,
                capture_output=True,
                text=True,
                errors="replace",
            )
        except subprocess.TimeoutExpired:
            print("frozen executable smoke timed out", file=sys.stderr)
            return 1
        if not report_path.is_file():
            print(f"no smoke report (exit {process.returncode}): {process.stderr[-1000:]}", file=sys.stderr)
            return 1
        report = json.loads(report_path.read_text(encoding="utf-8"))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if process.returncode != 0 and process.stderr:
            print(process.stderr[-4000:], file=sys.stderr)
        bundled = report.get("model_sources") or {}
        internal = executable.parent / "_internal" / "model_snapshots"
        sources_ok = all(
            path and Path(path).is_relative_to(internal)
            for path in (bundled.get("clip"), bundled.get("eye"))
        )
        return 0 if (process.returncode == 0 and
                     report.get("backend") == DEFAULT_BACKEND and
                     report.get("clip_loaded") and report.get("eye_loaded") and
                     sources_ok and not report.get("degradations")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
