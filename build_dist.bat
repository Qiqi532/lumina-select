@echo off
chcp 65001 >nul
rem ===========================================================================
rem 光影选片助手 —— 打包"自包含" onedir 版本（真正不再依赖 .venv 的 exe）
rem
rem 产物：dist\光影选片助手\ 文件夹（含 光影选片助手.exe + 全部依赖），
rem        双击 dist\光影选片助手\光影选片助手.exe 即可运行，无需项目源码/.venv。
rem
rem 纯 cmd.exe 语法：仅用 @echo off / chcp / cd /d / call / if / set，
rem       不引入任何 PowerShell cmdlet（Set-Location / Write-Host 等）。
rem
rem 可选：构建完成后打包成 zip —— 设置环境变量 ZIP=1 后运行本脚本：
rem        set ZIP=1 & build_dist.bat
rem ===========================================================================
setlocal
cd /d "%~dp0"
if "%~1"=="--release" (
    echo [错误] 正式发行须先完成人工 Lightroom 与 Qt 许可验收；本脚本只生成开发候选包。
    exit /b 2
)

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [错误] 未找到 .venv，请先创建虚拟环境并安装依赖（见 README）。
    exit /b 1
)

"%PY%" scripts\verify_exiftool.py
if errorlevel 1 exit /b 1
set "QT_QPA_PLATFORM=offscreen"
set "LUMINA_INFERENCE_BACKEND=heuristic"
"%PY%" -m pytest -q -o addopts=
if errorlevel 1 exit /b 1
"%PY%" scripts\check_release_licenses.py --requirements requirements.txt --policy release\license-policy.toml
if errorlevel 1 exit /b 1
"%PY%" -m pytest -q tests\test_distribution_contracts.py
if errorlevel 1 exit /b 1

echo.
echo ============================================================
echo   光影选片助手 —— 自包含 onedir 打包
echo   解释器: %PY%
echo   规格  : 光影选片助手_dist.spec
echo   说明  : 打包全部依赖（torch/transformers/PyQt6/mediapipe 等），
echo          产物可脱离 .venv 直接运行。
echo ============================================================
echo.

echo 打包中（首次约 3~8 分钟，取决于 torch/CUDA 体积）…
"%PY%" -m PyInstaller --noconfirm --clean 光影选片助手_dist.spec
if errorlevel 1 exit /b 1

if not exist "%~dp0dist\光影选片助手\光影选片助手.exe" (
    echo.
    echo [错误] 打包失败，请查看上方报错。
    exit /b 1
)

echo.
echo 完成：dist\光影选片助手\  （文件夹整体可独立运行）

rem ----- 可选：把 onedir 文件夹压缩为 zip（用 Windows 自带 tar.exe，非 PowerShell） -----
if "%ZIP%"=="1" (
    echo 正在打包 dist\光影选片助手.zip …
    if exist "%~dp0dist\光影选片助手.zip" del /q "%~dp0dist\光影选片助手.zip"
    tar -a -cf "%~dp0dist\光影选片助手.zip" -C "%~dp0dist" 光影选片助手
    if exist "%~dp0dist\光影选片助手.zip" (
        echo 完成：dist\光影选片助手.zip
    ) else (
        echo [警告] zip 打包失败，可手动压缩 dist\光影选片助手\ 文件夹。
    )
) else (
    echo 提示：运行  set ZIP=1 ^& build_dist.bat  可额外生成 zip 压缩包。
)

echo.
echo 下一步（制作安装包，可选）：用 Inno Setup 编译 installer.iss
