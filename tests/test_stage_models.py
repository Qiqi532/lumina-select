from __future__ import annotations

import hashlib
import json

import pytest

from scripts import stage_models


def test_stage_only_locked_files_and_reject_corruption(tmp_path, monkeypatch):
    source = tmp_path / "hub" / "models--sample--model" / "snapshots" / "revision"
    source.mkdir(parents=True)
    (source / "config.json").write_bytes(b"verified")
    (source / "unfinished.incomplete").write_bytes(b"partial")
    lock = tmp_path / "models.lock.json"
    lock.write_text(json.dumps({"models": [{
        "repo": "sample/model", "revision": "revision", "directory": "sample",
        "files": {"config.json": {"size": 8,
                  "sha256": hashlib.sha256(b"verified").hexdigest()}},
    }]}), encoding="utf-8")
    monkeypatch.setattr(stage_models, "LOCK", lock)
    target = tmp_path / "staged"
    stage_models.stage(tmp_path / "hub", target)
    assert [p.name for p in (target / "sample").iterdir()] == ["config.json"]
    (source / "config.json").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        stage_models.stage(tmp_path / "hub", target)
