# -*- mode: python ; coding: utf-8 -*-
# =============================================================================
# 光影选片助手_dist_lightweight.spec —— 轻量版（无 torch）自包含打包规格（onedir）
#
# 与 光影选片助手_dist.spec（含 torch/CLIP/MUSIQ，约 1.5GB）不同，本 spec：
#   * 排除 torch / torchvision / transformers / huggingface_hub / safetensors / pyiqa
#     —— 这些深度学习依赖由 engine/inference.py 的 HeuristicBackend 替代，
#        画质/美学/场景改走纯 OpenCV 启发式（见 engine/inference.py）。
#   * 保留 PyQt6（界面）+ MediaPipe（人脸/闭眼，非 torch）+ opencv/numpy/pillow/imagehash。
#   * 通过 dist_runtime_hook_light.py 强制 INFERENCE_BACKEND=heuristic，完全离线、
#     无需下载任何模型权重，双击即跑。
#
# 产物：dist_light/光影选片助手/ 文件夹，整体可独立运行。
# 用法：build_dist_lightweight.bat   （执行  pyinstaller 光影选片助手_dist_lightweight.spec）
# =============================================================================

from PyInstaller.utils.hooks import collect_data_files

app_script = 'app_qt.py'

# 只打包 FaceMesh 会引用的图和 TFLite 模型；手势、姿态、分割等模型不属于本应用。
# Python 模块由实际 import 与 PyInstaller 官方 hook 解析。
datas = collect_data_files(
    'mediapipe',
    includes=['modules/face_landmark/**', 'modules/face_detection/**'],
)

import os as _os
# 界面样式表
if _os.path.isfile('styles.qss'):
    datas.append(('styles.qss', '.'))
for notice in ('NOTICE.md', 'THIRD_PARTY_NOTICES.md', 'LICENSE', 'release/exiftool-lumina.config'):
    if not _os.path.isfile(notice):
        raise FileNotFoundError(notice)
    datas.append((notice, '.'))
for runtime in ('exiftool.exe', 'exiftool_files'):
    source = _os.path.join('release', 'exiftool', runtime)
    if not _os.path.exists(source):
        raise FileNotFoundError(source)
    datas.append((source, 'exiftool' if runtime == 'exiftool.exe' else 'exiftool/exiftool_files'))

hiddenimports = [
    'PyQt6.QtCore', 'PyQt6.QtGui', 'PyQt6.QtWidgets',
    'mediapipe', 'mediapipe.python.solutions.face_mesh',
    'absl.logging',
    'PIL.Image',
    'cv2', 'imagehash',
    'ui.main_window', 'ui.pages.review_page', 'ui.pages.export_page',
    'services.review_service', 'services.export_service', 'lxml.etree',
    'services.distribution_smoke',
]

a = Analysis(
    [app_script],
    pathex=['.'],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=['dist_runtime_hook_light.py'],   # 强制 heuristic 后端 + 缓存重定向
    excludes=[
        # —— 深度学习框架（轻量版核心排除项）——
        'torch', 'torchvision', 'torchaudio',
        'transformers', 'huggingface_hub', 'safetensors',
        'pyiqa', 'timm',
        # 与标准版一致的瘦身排除项
        'sklearn', 'matplotlib', 'tensorboard', 'wandb',
        'tensorflow', 'keras', 'panel', 'bokeh', 'holoviews', 'datashader',
        'botocore', 'boto3', 's3transfer', 'jmespath',
        'bitsandbytes', 'pyarrow', 'numba', 'llvmlite',
        'pandas', 'h5py', 'jax', 'jaxlib', 'flax', 'dask', 'distributed',
        'IPython', 'jupyter', 'notebook', 'ipykernel', 'streamlit',
        'pytest', 'sphinx', 'docutils',
        'seaborn', 'plotly', 'sympy',
        'PyQt5', 'PySide2', 'PySide6', 'shiboken2', 'shiboken6',
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='光影选片助手',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='光影选片助手',
)
