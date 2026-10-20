"""Stage exactly the locked local model files for an offline onedir build."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil


ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "release" / "models.lock.json"


def digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            sha.update(block)
    return sha.hexdigest()


def verify_file(path: Path, record: dict) -> None:
    if not path.is_file() or path.stat().st_size != record["size"]:
        raise ValueError(f"missing or wrong size: {path}")
    if digest(path) != record["sha256"]:
        raise ValueError(f"SHA-256 mismatch: {path}")


def stage(source_hub: Path, destination: Path) -> None:
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    for model in lock["models"]:
        source = (source_hub / ("models--" + model["repo"].replace("/", "--")) /
                  "snapshots" / model["revision"])
        target = destination / model["directory"]
        for filename, record in model["files"].items():
            verify_file(source / filename, record)
        target.mkdir(parents=True, exist_ok=True)
        for filename, record in model["files"].items():
            staged = target / filename
            if not staged.is_file():
                shutil.copy2(source / filename, staged)
            verify_file(staged, record)
        extras = {p.name for p in target.iterdir()} - set(model["files"])
        if extras:
            raise ValueError(f"unexpected staged files in {target}: {sorted(extras)}")
        print(f"verified {model['repo']} at {model['revision']}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-hub", type=Path, default=ROOT / ".hf_cache" / "hub")
    parser.add_argument("--destination", type=Path, default=ROOT / "model_snapshots")
    args = parser.parse_args()
    stage(args.source_hub, args.destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
