# -*- coding: utf-8 -*-
"""models_guard 单测：本地模型加载源的判定与接线（v0.6 S2-2「补齐离线能力」）。

背景（都是实测，不是理论风险）：
  * **时序陷阱**：huggingface_hub 在 **import 时**就把 `HF_HUB_CACHE` 算成模块常量。
    若它在 `models_guard.apply_env()` 之前被 import，之后设置环境变量**不再生效** ——
    表现是"项目内 `.hf_cache` 里明明有模型，程序却去 `~/.cache/huggingface` 找并失败"。
    因此加载器改为**显式指向本地快照目录**，而不是依赖环境变量。
  * **性能**：网络可达时 `from_pretrained` 仍会做 HEAD/etag 往返，抖动重试能把一次
    CLIP 加载从 ~15s 拖到 499s。
  * **稳定性**：模型 ID 未锁修订，联网会让权重在用户无感知的情况下漂移，而
    `_ai_model_signature()` 含模型名 → 漂移触发全库重算 → P0-1 的触发条件。

> 注意：本文件**刻意不修改 `HF_HUB_CACHE` 环境变量**。测试里改它既测不到真实行为，
> 又会污染同进程内后续的真实模型加载（实测会让发布冒烟用例误判成"模型缺失"）。
> 改用 monkeypatch 重定向 `models_guard.hf_hub_cache_dir`。

本文件不加载真权重：接线检查通过打桩 transformers 的两个 `from_pretrained` 完成。
"""
from __future__ import annotations

from pathlib import Path

from engine import models_guard

CLIP_REPO = "openai/clip-vit-base-patch32"


def _point_cache_at(monkeypatch, hub: Path) -> None:
    """把「HF 缓存目录」这一查询重定向到临时目录（不碰环境变量）。"""
    monkeypatch.setattr(models_guard, "hf_hub_cache_dir", lambda: str(hub))


def _make_snapshot(hub: Path, repo_id: str, *, with_weight: bool) -> Path:
    snap = hub / ("models--" + repo_id.replace("/", "--")) / "snapshots" / "rev0"
    snap.mkdir(parents=True)
    (snap / "config.json").write_text("{}", encoding="utf-8")
    if with_weight:
        (snap / "pytorch_model.bin").write_bytes(b"\x00")
    return snap


def test_no_snapshot_means_we_must_go_online(tmp_path, monkeypatch):
    _point_cache_at(monkeypatch, tmp_path / "hub")
    assert models_guard.has_local_snapshot(CLIP_REPO) is None
    assert models_guard.local_model_source(CLIP_REPO) is None


def test_complete_snapshot_is_returned_as_load_source(tmp_path, monkeypatch):
    hub = tmp_path / "hub"
    _point_cache_at(monkeypatch, hub)
    snap = _make_snapshot(hub, CLIP_REPO, with_weight=True)
    assert models_guard.has_local_snapshot(CLIP_REPO) == str(snap)
    assert models_guard.local_model_source(CLIP_REPO) == str(snap)


def test_incomplete_snapshot_is_not_treated_as_cached(tmp_path, monkeypatch):
    """只有 config.json、没有权重 → 不算"已缓存"，必须允许联网下载。"""
    hub = tmp_path / "hub"
    _point_cache_at(monkeypatch, hub)
    _make_snapshot(hub, CLIP_REPO, with_weight=False)
    assert models_guard.has_local_snapshot(CLIP_REPO) is None
    assert models_guard.local_model_source(CLIP_REPO) is None


class _FakeModel:
    def to(self, _device):
        return self

    def eval(self):
        return self


def test_clip_loader_loads_from_local_snapshot_when_available(tmp_path, monkeypatch):
    """接线检查：本地有完整快照时，CLIP 必须**直接把该目录**交给 from_pretrained。

    这样才与"缓存环境变量在 import 时被固化"这一事实解耦。只测接线，不加载真权重。
    """
    import transformers

    from engine import aesthetics

    hub = tmp_path / "hub"
    _point_cache_at(monkeypatch, hub)
    snap = _make_snapshot(hub, CLIP_REPO, with_weight=True)

    seen: dict[str, tuple] = {}

    def fake_processor(*args, **kwargs):
        seen["processor"] = (args, kwargs)
        return object()

    def fake_model(*args, **kwargs):
        seen["model"] = (args, kwargs)
        return _FakeModel()

    monkeypatch.setattr(
        transformers.CLIPProcessor, "from_pretrained", staticmethod(fake_processor)
    )
    monkeypatch.setattr(
        transformers.CLIPModel, "from_pretrained", staticmethod(fake_model)
    )
    # 清掉可能已被其它测试加载的真实单例（monkeypatch 会在用例结束时还原）
    monkeypatch.setattr(aesthetics, "_model", None)
    monkeypatch.setattr(aesthetics, "_proc", None)

    aesthetics.get_clip()

    assert seen["processor"][0][0] == str(snap)
    assert seen["model"][0][0] == str(snap)
    assert seen["processor"][1].get("local_files_only") is True


def test_clip_loader_falls_back_to_repo_id_without_snapshot(tmp_path, monkeypatch):
    """本地没有快照时仍以模型 ID 为源（首次运行需要联网下载，不能被误判为本地缺失）。"""
    import transformers

    from engine import aesthetics

    _point_cache_at(monkeypatch, tmp_path / "hub")
    seen: dict[str, tuple] = {}

    def fake_processor(*args, **kwargs):
        seen["processor"] = (args, kwargs)
        return object()

    def fake_model(*args, **kwargs):
        seen["model"] = (args, kwargs)
        return _FakeModel()

    monkeypatch.setattr(
        transformers.CLIPProcessor, "from_pretrained", staticmethod(fake_processor)
    )
    monkeypatch.setattr(
        transformers.CLIPModel, "from_pretrained", staticmethod(fake_model)
    )
    monkeypatch.setattr(aesthetics, "_model", None)
    monkeypatch.setattr(aesthetics, "_proc", None)

    aesthetics.get_clip()

    assert seen["processor"][0][0] == aesthetics.MODEL_NAME
    assert "local_files_only" not in seen["processor"][1]
