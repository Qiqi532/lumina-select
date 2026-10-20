# -*- coding: utf-8 -*-
"""
models_guard.py —— 模型加载守卫（缓存自愈 / 离线优先 / 降级链 / 可解释错误）

为什么需要这个模块（全部来自本项目实测，不是理论风险）：

  1) **HF 缓存会自腐**：huggingface_hub 会把"服务端不存在某文件"这一事实写成
     `.no_exist/<rev>/<filename>` 标记。transformers 升级后需要的文件列表会变
     （本项目实测：旧版写入 processor_config.json 不存在，而 transformers 5.x
     恰恰需要它），于是离线加载 CLIP 必定抛 OSError，GUI 只显示"分析出错"。
     用户视角：昨天还能用，今天打开就崩，且完全无从自救。

  2) **网络不可控**：代理 502 / 断网会让 from_pretrained 抛原始异常。

  3) **降级链缺失**：美学头、画质模型、闭眼分类器任一缺失，本应自动降级；
     但旧实现里失败只写一行 warning，调用方拿到 None 后静默退化，
     用户以为"AI 分析过了"，实际是少算了一半信号。

本模块提供：
    repair_hf_cache()  清理过期 .no_exist 标记（幂等、安全）
    apply_env()        统一注入 HF_*/TORCH_* 缓存环境变量 + 镜像端点
    try_load()         按降级链依次尝试加载，全部失败时抛出可解释的 ModelLoadError

独立调试：python -m engine.models_guard
"""
from __future__ import annotations

import os
import shutil
import time
import hashlib
import json
import sys
from pathlib import Path

from . import config
from .log import get_logger

_log = get_logger("models_guard")


class ModelLoadError(RuntimeError):
    """模型加载失败（含已尝试的候选与原因），供 UI 展示可操作提示。"""

    def __init__(self, what: str, attempts: list[tuple[str, str]]):
        self.what = what
        self.attempts = attempts
        detail = "；".join(f"{name}: {err}" for name, err in attempts)
        super().__init__(f"{what} 加载失败（已尝试 {len(attempts)} 个候选）→ {detail}")

    def user_hint(self) -> str:
        """给用户看的可操作建议。"""
        lines = [f"{self.what} 未能加载，已尝试："]
        for name, err in self.attempts:
            lines.append(f"  · {name} —— {err}")
        if getattr(sys, "frozen", False):
            lines.append("请重新获取完整的离线便携目录；随包模型缺失或校验失败，当前不会联网补齐。")
            return "\n".join(lines)
        lines.append("")
        lines.append("可尝试：")
        lines.append("  1) 联网后重启程序，让模型自动下载到项目内 .hf_cache / .torch_cache；")
        lines.append(f"  2) 若国内网络不通，在 engine/config.py 中设置 "
                     f"HF_ENDPOINT = \"https://hf-mirror.com\"；")
        lines.append("  3) 离线使用时确认 .hf_cache / .torch_cache 目录随项目一起拷贝。")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 缓存环境变量
# ---------------------------------------------------------------------------
def apply_env() -> dict[str, str]:
    """把模型缓存统一重定向到项目内（不落 C 盘），并返回生效的变量。

    用 setdefault 而非无条件覆盖：
      * 打包态（PyInstaller）下 dist_runtime_hook.py 已在应用启动前把缓存指向
        exe 目录 —— 本函数不得覆盖回 _internal（只读，且可能装在 Program Files）；
      * 用户可用系统环境变量覆盖（例如把模型缓存放到大容量盘）。
    幂等：重复调用结果一致。返回本次生效的变量 dict，便于调用方展示。
    """
    root = config.PROJECT_ROOT
    env = {
        "HF_HOME": os.path.join(root, ".hf_cache"),
        "HF_HUB_CACHE": os.path.join(root, ".hf_cache", "hub"),
        "TORCH_HOME": os.path.join(root, ".torch_cache"),
        "TRANSFORMERS_CACHE": os.path.join(root, ".hf_cache", "hub"),
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "HF_HUB_DISABLE_PROGRESS_BARS": "1",
    }
    if config.HF_ENDPOINT:
        env["HF_ENDPOINT"] = config.HF_ENDPOINT
    for k, v in env.items():
        os.environ.setdefault(k, v)
    return {k: os.environ[k] for k in env}


def hf_hub_cache_dir() -> str:
    """HF hub 缓存实际生效目录（环境变量优先，回退项目内 .hf_cache/hub）。

    与 apply_env() 保持同一口径：打包态环境变量已由运行时钩子指向 exe 目录，
    缓存自愈 / 本地快照检查必须看这里，而不是硬编码的源码目录。
    """
    return os.environ.get("HF_HUB_CACHE") or os.path.join(
        config.PROJECT_ROOT, ".hf_cache", "hub")


# ---------------------------------------------------------------------------
# HF 缓存自愈
# ---------------------------------------------------------------------------
def repair_hf_cache(verbose: bool = False) -> dict:
    """清理 HuggingFace 缓存中过期的 `.no_exist` 标记。

    背景：`.no_exist/<rev>/<file>` 表示"该文件在服务端不存在"。这个结论只对
    【写入时的 transformers/huggingface_hub 版本】成立。版本升级后新增的必需
    文件会被这条陈旧标记一票否决 —— 于是明明文件在本地，离线加载却必失败。

    本函数删除这些标记，让加载器重新去快照目录里找真实文件。删除是安全的：
    最坏情况是重新联网确认一次该文件是否存在。

    同时清理**不完整快照**（只有 config.json 没有权重）：这类快照会让
    from_pretrained 选中它然后失败，而其实旁边另有一个完整快照。

    返回统计 dict。
    """
    stats = {"no_exist_removed": 0, "incomplete_snapshots": 0, "scanned": 0}
    if getattr(sys, "frozen", False):
        return stats
    hub = hf_hub_cache_dir()
    if not os.path.isdir(hub):
        return stats

    import re

    # 权重文件特征：有这些之一才算"完整"
    weight_pat = re.compile(
        r"\.(bin|safetensors|pt|pth|onnx|h5|msgpack|npz)$", re.IGNORECASE)

    for repo in os.listdir(hub):
        repo_dir = os.path.join(hub, repo)
        # 注意：判断前缀要用【目录名】而非完整路径（完整路径永远不以 models-- 开头）
        if not repo.startswith("models--") or not os.path.isdir(repo_dir):
            continue
        stats["scanned"] += 1

        # --- 1) 清 .no_exist ---
        no_exist = os.path.join(repo_dir, ".no_exist")
        if os.path.isdir(no_exist):
            for rev in os.listdir(no_exist):
                rev_dir = os.path.join(no_exist, rev)
                if not os.path.isdir(rev_dir):
                    continue
                for fn in os.listdir(rev_dir):
                    try:
                        os.remove(os.path.join(rev_dir, fn))
                        stats["no_exist_removed"] += 1
                    except OSError:
                        pass
            shutil.rmtree(no_exist, ignore_errors=True)
            if verbose:
                _log.info("已清理过期缓存标记：%s", repo)

        # --- 2) 删除不完整快照（无权重文件），保留完整的 ---
        snaps = os.path.join(repo_dir, "snapshots")
        if not os.path.isdir(snaps):
            continue
        revs = [r for r in os.listdir(snaps)
                if os.path.isdir(os.path.join(snaps, r))]
        if len(revs) < 2:
            continue
        complete = []
        for rev in revs:
            d = os.path.join(snaps, rev)
            try:
                has_w = any(weight_pat.search(f) for f in os.listdir(d))
            except OSError:
                has_w = False
            if has_w:
                complete.append(rev)
                continue
            # 无权重文件的快照：留着只会让加载器选中它然后失败，直接删
            shutil.rmtree(d, ignore_errors=True)
            stats["incomplete_snapshots"] += 1
            if verbose:
                _log.info("已删除不完整快照：%s/%s", repo, rev[:12])
        if not complete:
            _log.warning("缓存清理后 %s 无完整快照剩余，请联网重新下载模型", repo)

    if stats["no_exist_removed"] or stats["incomplete_snapshots"]:
        _log.info("HF 缓存自愈：清理 %d 个过期标记 / %d 个不完整快照",
                  stats["no_exist_removed"], stats["incomplete_snapshots"])
    return stats


# ---------------------------------------------------------------------------
# 降级链加载
# ---------------------------------------------------------------------------
def try_load(what: str, candidates: list[tuple[str, object]], timeout: float | None = None):
    """按 candidate 列表依次尝试加载，返回 (生效名称, 实例)。

    candidates: [(名称, 可调用工厂), ...]，工厂不接受参数、返回模型实例。
    全部失败时抛出 ModelLoadError（含每个候选的失败原因）。
    """
    timeout = timeout if timeout is not None else config.MODEL_LOAD_TIMEOUT
    attempts: list[tuple[str, str]] = []
    for name, factory in candidates:
        t0 = time.time()
        try:
            obj = factory()
            _log.info("%s 已就绪：%s（%.1fs）", what, name, time.time() - t0)
            return name, obj
        except Exception as e:  # noqa: BLE001 —— 降级链的语义就是吞掉单次失败
            err = f"{type(e).__name__}: {str(e).splitlines()[0][:160]}"
            _log.warning("%s 候选 %s 加载失败（%.1fs）：%s", what, name, time.time() - t0, err)
            attempts.append((name, err))
    raise ModelLoadError(what, attempts)


# ---------------------------------------------------------------------------
# 离线优先：判断本地是否已有模型快照
# ---------------------------------------------------------------------------
def has_local_snapshot(repo_id: str) -> str | None:
    """若 HF 缓存中已有该 repo 的完整快照，返回其目录；否则 None。

    "完整" = 目录下存在 config.json 且存在至少一个权重文件。
    """
    import re

    hub = hf_hub_cache_dir()
    repo_dir = os.path.join(hub, "models--" + repo_id.replace("/", "--"))
    snaps = os.path.join(repo_dir, "snapshots")
    if not os.path.isdir(snaps):
        return None
    weight_pat = re.compile(r"\.(bin|safetensors|pt|pth|onnx|h5)$", re.IGNORECASE)
    for rev in sorted(os.listdir(snaps)):
        d = os.path.join(snaps, rev)
        if not os.path.isdir(d):
            continue
        try:
            files = os.listdir(d)
        except OSError:
            continue
        if "config.json" in files and any(weight_pat.search(f) for f in files):
            return d
    return None


def local_model_source(repo_id: str) -> str | None:
    """本地已有**完整**快照时返回其目录，否则 None —— 调用方应**直接用它去加载**。

    为什么不只是"加 `local_files_only=True`、让 HF 自己解析缓存目录"：

      1) **时序脆弱**：huggingface_hub 在 **import 时**就把 `HF_HUB_CACHE` 算成模块常量。
         若它在 `apply_env()` 之前被 import（测试、第三方库或将来有人在模块顶层
         `import transformers` 都会触发），之后再设置环境变量**不再生效** —— 表现是
         "项目内 `.hf_cache` 里明明有模型，程序却去 `~/.cache/huggingface` 找并失败"。
         本项目在 S2-2 的测试里实测复现过这一条。
      2) **性能**：网络可达时 `from_pretrained` 仍会做 HEAD/etag 往返，抖动重试能把一次
         CLIP 加载从 ~15s 拖到 **499s**（实测）。
      3) **稳定性**：模型 ID 未锁修订，联网会让权重在用户无感知的情况下漂移，而
         `_ai_model_signature()` 含模型名 → 漂移触发全库重算，正是 P0-1 的雷区。

    显式给出目录可一次解决以上三条，并完全摆脱缓存环境变量的时序依赖。
    """
    if getattr(sys, "frozen", False):
        return bundled_model_source(repo_id)
    return has_local_snapshot(repo_id)


_validated_bundles: set[str] = set()


def bundled_model_source(repo_id: str) -> str:
    """Frozen builds use only the locked, hashed files inside _internal."""
    root = Path(config._BUNDLED_ROOT)
    lock_path = root / "models.lock.json"
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        model = next(item for item in lock["models"] if item["repo"] == repo_id)
        directory = root / "model_snapshots" / model["directory"]
        if repo_id not in _validated_bundles:
            for filename, record in model["files"].items():
                path = directory / filename
                if not path.is_file() or path.stat().st_size != record["size"]:
                    raise ValueError(f"missing or wrong size: {filename}")
                sha = hashlib.sha256()
                with path.open("rb") as stream:
                    for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                        sha.update(block)
                if sha.hexdigest() != record["sha256"]:
                    raise ValueError(f"SHA-256 mismatch: {filename}")
            _validated_bundles.add(repo_id)
        return str(directory)
    except (OSError, ValueError, KeyError, StopIteration, TypeError) as error:
        raise ModelLoadError(repo_id, [("随包权重", str(error))]) from error


def offline_local_files_only() -> bool:
    """是否应强制本地加载（config.OFFLINE_FIRST 且确实处于离线/受限网络）。"""
    if getattr(sys, "frozen", False):
        return True
    if not config.OFFLINE_FIRST:
        return False
    # 探测【实际配置的端点】而非写死 huggingface.co：国内网络典型场景是
    # hf-mirror.com 可达而 huggingface.co 被墙 —— 探官网会把在线误判为离线，
    # 导致首次运行时模型永远下载不下来。
    from urllib.parse import urlparse

    endpoint = os.environ.get("HF_ENDPOINT") or config.HF_ENDPOINT or "https://huggingface.co"
    host = urlparse(endpoint).hostname or "huggingface.co"
    try:
        import socket

        socket.setdefaulttimeout(1.5)
        socket.create_connection((host, 443), timeout=1.5).close()
        return False
    except Exception:
        return True


# ---------------------------------------------------------------------------
# 命令行调试入口
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    apply_env()
    print("缓存环境：")
    for k, v in apply_env().items():
        print(f"  {k} = {v}")
    print("\n缓存自愈：", repair_hf_cache(verbose=True))
    print("\n本地快照检查：")
    for repo in ("openai/clip-vit-base-patch32", "dima806/closed_eyes_image_detection"):
        p = has_local_snapshot(repo)
        print(f"  {repo:44s} -> {p or '无（需联网下载）'}")
    print("\n网络状态：", "离线（将强制本地加载）" if offline_local_files_only() else "在线")
