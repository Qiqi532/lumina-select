"""Run an onedir build outside the source tree and audit bundled dependencies."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def audit_bundle(bundle: Path, backend: str) -> list[str]:
    internal = bundle / "_internal"
    names = {path.name.casefold() for path in internal.iterdir()} if internal.is_dir() else set()
    if backend == "heuristic":
        return sorted(names & {"torch", "torchvision", "transformers", "pyiqa"})
    if backend == "torch":
        failures = ["pyiqa"] if "pyiqa" in names else []
        failures.extend(f"missing:{name}" for name in ("torch", "transformers") if name not in names)
        return failures
    raise ValueError(f"unsupported backend: {backend}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--backend", choices=("heuristic", "torch"), required=True)
    parser.add_argument("--timeout", type=int, default=240)
    args = parser.parse_args(argv)
    executable = args.exe.resolve()
    if not executable.is_file():
        print(f"missing executable: {executable}", file=sys.stderr)
        return 1
    forbidden = audit_bundle(executable.parent, args.backend)
    if forbidden:
        print(f"bundle audit failed: {', '.join(forbidden)}", file=sys.stderr)
        return 1
    with tempfile.TemporaryDirectory(prefix="lumina-detached-dist-") as directory:
        report_path = Path(directory) / "report.json"
        environment = os.environ.copy()
        environment.update({
            "QT_QPA_PLATFORM": "offscreen",
            "LUMINA_INFERENCE_BACKEND": args.backend,
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
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
        return 0 if process.returncode == 0 and report.get("backend") == args.backend else 1


if __name__ == "__main__":
    raise SystemExit(main())
