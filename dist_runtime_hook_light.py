# -*- coding: utf-8 -*-
r"""
dist_runtime_hook_light.py —— 轻量版（无 torch）分发包的 PyInstaller 运行时钩子

由 光影选片助手_dist_lightweight.spec 的 runtime_hooks 加载，在 app_qt.py 被 import
*之前* 执行。作用：
  1. 强制推理后端为 "heuristic"（纯 OpenCV，无 torch / 无模型下载 / 完全离线），
     使打出的 exe 不依赖任何深度学习权重，约 150MB、秒级启动。
  2. 重定向崩溃日志到 exe 旁的 crash.log（打包态无控制台）。
  3. 缓存目录重定向（保持与标准版一致，便于同目录共存）。

本文件为 raw 字符串（r""），避免 Windows 路径反斜杠被解析成非法转义。
"""
import os
import sys


def _app_dir() -> str:
    r"""返回 exe 所在目录（onedir 形态下即 dist\光影选片助手 文件夹）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


_app = _app_dir()

# PyInstaller 的自动 PyQt6 运行钩子会先导入 QtCore；此后再加载 MediaPipe
# 的 _framework_bindings 会在 Windows 上以 DLL 初始化错误失败。自定义钩子
# 排在自动钩子前，先装载扩展，随后 faces.py 才导入 Python 侧 API。
if getattr(sys, "frozen", False) and sys.platform == "win32":
    import ctypes
    from pathlib import Path

    _mp_native_dir = Path(sys._MEIPASS) / "mediapipe" / "python"
    _mp_extensions = list(_mp_native_dir.glob("_framework_bindings*.pyd"))
    if len(_mp_extensions) == 1:
        _mp_dll_directory = os.add_dll_directory(str(_mp_native_dir))
        try:
            _mp_native = ctypes.WinDLL(str(_mp_extensions[0]))
        except OSError:
            _mp_dll_directory.close()
            # 保留无脸中性值退化路径；faces.py 会记录可诊断的初始化错误。

# 关键：强制轻量推理后端（在 engine 任何模块 import 之前设置，确保生效）
os.environ["LUMINA_INFERENCE_BACKEND"] = "heuristic"

# 缓存目录重定向（轻量版虽不下载 torch/CLIP 权重，仍保留一致布局）
os.environ.setdefault("HF_HOME", os.path.join(_app, ".hf_cache"))
os.environ.setdefault("HF_HUB_CACHE", os.path.join(_app, ".hf_cache", "hub"))
os.environ.setdefault("TORCH_HOME", os.path.join(_app, ".torch_cache"))
os.environ.setdefault("TRANSFORMERS_CACHE", os.path.join(_app, ".hf_cache", "hub"))
os.environ.setdefault("PYTHONPYCACHEPREFIX", os.path.join(_app, "pycache"))


# ---- 崩溃日志 ----
def _install_crash_logger():
    import time as _time
    import traceback as _tb

    def _hook(etype, val, tb):
        try:
            with open(os.path.join(_app, "crash.log"), "a", encoding="utf-8") as f:
                timestamp = _time.strftime("%Y-%m-%d %H:%M:%S")
                f.write(f"\n===== {timestamp} =====\n")
                _tb.print_exception(etype, val, tb, file=f)
        finally:
            sys.__excepthook__(etype, val, tb)

    sys.excepthook = _hook


_install_crash_logger()
