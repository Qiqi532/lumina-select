@echo off
chcp 65001 >nul
rem ===========================================================================
rem 光影选片助手 —— 轻量版（无 torch）自包含 onedir 打包
rem
rem 产物：dist_light\光影选片助手\ 文件夹（含 光影选片助手.exe + 依赖），
rem        双击即可运行，完全离线、无需下载任何模型权重。
rem
rem 与 build_dist.bat（含 torch/CLIP/MUSIQ，约 1.5GB）的区别：
rem   排除 torch/transformers/pyiqa，画质/美学/场景改走纯 OpenCV 启发式
rem   （engine/inference.py 的 HeuristicBackend，由 dist_runtime_hook_light.py 强制启用）。
rem
rem 纯 cmd.exe 语法，不引入 PowerShell。
rem ===========================================================================
setlocal
cd /d "%~dp0"
if "%~1"=="--release" (
    echo [错误] 正式发行须先完成人工 Lightroom 与 Qt 许可验收；本脚本只生成开发候选包。
    exit /b 2
)

set "PY=%~dp0.venv-light\Scripts\python.exe"
if not exist "%PY%" (
    echo [错误] 未找到 .venv-light，请先执行：
    echo   python -m venv .venv-light
    echo   .venv-light\Scripts\python.exe -m pip install -r requirements-lightweight.txt
    exit /b 1
)

"%PY%" scripts\verify_exiftool.py
if errorlevel 1 exit /b 1
set "QT_QPA_PLATFORM=offscreen"
set "LUMINA_INFERENCE_BACKEND=heuristic"
"%PY%" -m pytest -q -o addopts=
if errorlevel 1 exit /b 1
"%PY%" scripts\check_release_licenses.py --requirements requirements-lightweight.txt --policy release\license-policy.toml
if errorlevel 1 exit /b 1
"%PY%" -m pytest -q tests\test_distribution_contracts.py
if errorlevel 1 exit /b 1

echo.
echo ============================================================
echo   光影选片助手 —— 轻量版（无 torch）自包含打包
echo   解释器: %PY%
echo   规格  : 光影选片助手_dist_lightweight.spec
echo   说明  : 仅打包 PyQt6/MediaPipe/opencv/numpy 等，不打包 torch，
echo          推理走 OpenCV 启发式，产物离线、秒级启动。
echo ============================================================
echo.

echo 打包中（首次约 1~3 分钟）…
"%PY%" -m PyInstaller --noconfirm --clean --distpath dist_light --workpath build_light 光影选片助手_dist_lightweight.spec
if errorlevel 1 exit /b 1

if not exist "%~dp0dist_light\光影选片助手\光影选片助手.exe" (
    echo.
    echo [错误] 打包失败，请查看上方报错。
    exit /b 1
)

echo.
echo 完成：dist_light\光影选片助手\  （文件夹整体可独立运行，无需联网）

rem ----- 可选：把 onedir 文件夹压缩为 zip -----
if "%ZIP%"=="1" (
    echo 正在打包 dist_light\光影选片助手.zip …
    if exist "%~dp0dist_light\光影选片助手.zip" del /q "%~dp0dist_light\光影选片助手.zip"
    tar -a -cf "%~dp0dist_light\光影选片助手.zip" -C "%~dp0dist_light" 光影选片助手
    if exist "%~dp0dist_light\光影选片助手.zip" (
        echo 完成：dist_light\光影选片助手.zip
    ) else (
        echo [警告] zip 打包失败，可手动压缩 dist_light\光影选片助手\ 文件夹。
    )
) else (
    echo 提示：运行  set ZIP=1 ^& build_dist_lightweight.bat  可额外生成 zip 压缩包。
)

echo.
echo 下一步（制作安装包，可选）：用 Inno Setup 编译 installer_lightweight.iss
