# -*- mode: python ; coding: utf-8 -*-
# =============================================================================
# 光影选片助手_dist.spec —— 真正"自包含"的 PyInstaller 打包规格（onedir 形态）
#
# 与旧的 光影选片助手.spec（仅打包 launcher.py，运行时还要调 .venv 里的
# python 启动 app_qt.py）不同，本 spec 直接以 app_qt.py 为入口，把全部第三方
# 依赖（torch / transformers / PySide6 / mediapipe / 等）一并打进 onedir 文件夹，
# 双击 dist/光影选片助手/光影选片助手.exe 即可运行，无需项目 .venv 与源码在场。
#
# CLIP 与闭眼 ViT 从经锁定清单校验的 model_snapshots 打入 _internal。
#
# 用法：build_dist.bat   （内部执行  pyinstaller 光影选片助手_dist.spec）
# =============================================================================

from PyInstaller.utils.hooks import collect_data_files

# 入口脚本：直接打包桌面主程序（不再经 launcher 子进程）。
# 注意：下方注释中的反斜杠仅为 Windows 路径示例说明。
app_script = 'app_qt.py'

# MediaPipe 仅保留本应用 FaceMesh 所需图与 TFLite 模型。
datas = collect_data_files(
    'mediapipe',
    includes=['modules/face_landmark/**', 'modules/face_detection/**'],
)

# 随包分发的小权重模型（LAION 美学线性头约 3KB，只读，打进 _internal/models）
import os as _os
from importlib.util import find_spec as _find_spec
if _os.path.isdir('models'):
    datas.append(('models', 'models'))
if not _os.path.isdir('model_snapshots'):
    raise FileNotFoundError('model_snapshots (run scripts/stage_models.py)')
datas.append(('model_snapshots', 'model_snapshots'))
datas.append(('release/models.lock.json', '.'))
# 界面样式表：app_qt.py 从 _HERE（冻结态=_internal）读取 styles.qss
if _os.path.isfile('styles.qss'):
    datas.append(('styles.qss', '.'))
for notice in ('NOTICE.md', 'THIRD_PARTY_NOTICES.md', 'LICENSE', 'release/exiftool-lumina.config'):
    if not _os.path.isfile(notice):
        raise FileNotFoundError(notice)
    datas.append((notice, '.'))
# LGPLv3 合规（T-02）：PySide6/shiboken6 是 LGPLv3 组件，发行包必须附带许可证全文。
# 因 LGPLv3 以引用方式并入 GPLv3（LGPL-3.0 §3(b)/§4(b) 要求随目标码附 GNU GPL 副本），
# 还需一并附带 GPL-3.0.txt。缺任一则构建即失败，避免漏带发行。
# RELINKING.md 是 LGPLv3 §4(d)(0) 要求的「安装信息」，说明如何替换 Qt 库。
for license_file in ('release/LGPL-3.0.txt', 'release/GPL-3.0.txt', 'release/RELINKING.md'):
    if not _os.path.isfile(license_file):
        raise FileNotFoundError(license_file)
    datas.append((license_file, '.'))
for runtime in ('exiftool.exe', 'exiftool_files'):
    source = _os.path.join('release', 'exiftool', runtime)
    if not _os.path.exists(source):
        raise FileNotFoundError(source)
    datas.append((source, 'exiftool' if runtime == 'exiftool.exe' else 'exiftool/exiftool_files'))

# TorchVision 0.29's renamed native extensions are missed by the current hook.
_torchvision = _find_spec('torchvision')
if _torchvision is None or _torchvision.origin is None:
    raise ModuleNotFoundError('torchvision is required for the standard edition')
_torchvision_dir = _os.path.dirname(_torchvision.origin)
binaries = []
for filename in ('_C_stable.pyd', 'image_stable.pyd', 'jpeg8.dll',
                 'libpng16.dll', 'libsharpyuv.dll', 'libwebp.dll', 'zlib.dll'):
    source = _os.path.join(_torchvision_dir, filename)
    if not _os.path.isfile(source):
        raise FileNotFoundError(source)
    binaries.append((source, 'torchvision'))

# 延迟加载与注册表驱动的模块需要显式列出；其依赖由 PyInstaller hook 解析。
hiddenimports = [
    'torch', 'torchvision', 'transformers', 'huggingface_hub', 'safetensors',
    'transformers.models.clip.configuration_clip',
    'transformers.models.clip.image_processing_clip',
    'transformers.models.clip.modeling_clip',
    'transformers.models.clip.processing_clip',
    'transformers.models.clip.tokenization_clip',
    'transformers.models.vit.configuration_vit',
    'transformers.models.vit.image_processing_vit',
    'transformers.models.vit.modeling_vit',
    'PySide6.QtCore', 'PySide6.QtGui', 'PySide6.QtWidgets', 'shiboken6',
    'mediapipe', 'mediapipe.python.solutions.face_mesh',
    'absl.logging', 'PIL.Image', 'cv2', 'imagehash',
    'ui.main_window', 'ui.pages.review_page', 'ui.pages.export_page',
    'services.review_service', 'services.export_service', 'lxml.etree',
    'services.distribution_smoke',
]

a = Analysis(
    [app_script],
    pathex=['.'],                      # 在项目根目录查找 engine 包与本地模块
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=['dist_runtime_hook.py'],  # 打包前重定向 HF/TORCH 缓存目录
    excludes=[
        # MediaPipe solutions/__init__.py 实际导入 drawing_utils -> matplotlib.pyplot。
        'pyiqa', 'timm', 'sklearn', 'tensorboard', 'wandb',
        'torchaudio',
        # 环境中同时存在 PyQt5（conda 自带，mediapipe 间接依赖）与 PySide6，
        # PyInstaller 禁止同时打包两个 Qt 绑定 —— 本应用只用 PySide6。
        # ⚠️ 绝不能排除 shiboken6：它是 PySide6 的**必需**依赖
        #    （PySide6/__init__.py 要 `from shiboken6 import Shiboken`），
        #    排除后冻结态会在启动时抛 ImportError。仅排除 PyQt/PySide2 的绑定。
        'PyQt5', 'PyQt6', 'PySide2', 'shiboken2',
        # conda base 里的重型可选依赖：transformers/mediapipe 的 try-import 链
        # 会被 PyInstaller 静态分析拖进包里（实测 tensorflow 1GB + 其余约 1GB），
        # 运行时验证过全部不需要 —— 排除后瘦身约一半
        'tensorflow', 'keras', 'panel', 'bokeh', 'holoviews', 'datashader',
        'botocore', 'boto3', 's3transfer', 'jmespath',
        'bitsandbytes', 'pyarrow', 'numba', 'llvmlite',
        'pandas', 'h5py', 'jax', 'jaxlib', 'flax', 'dask', 'distributed',
        'IPython', 'jupyter', 'notebook', 'ipykernel', 'streamlit',
        'pytest', 'sphinx', 'docutils',
        'seaborn', 'plotly',
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],                     # onedir：二进制与数据由下方 COLLECT 收集进 _internal，
    exclude_binaries=True,  # 不再重复嵌入 exe（否则 exe 体积翻倍至数 GB）
    name='光影选片助手',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                        # 大包下关闭 UPX，避免个别二进制损坏/极慢
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,                    # 桌面原生窗口，无控制台黑窗
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# onedir 形态：产出 dist/光影选片助手/ 文件夹，整体可独立运行
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='光影选片助手',
)
