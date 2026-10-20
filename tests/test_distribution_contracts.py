from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = "光影选片助手_dist.spec"


def _text(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 【v0.6 S2-2 / S2-3】双版本（标准版 + 轻量版）收敛为单版本后，本文件只保留
# "一套打包入口、一套依赖锁"的契约。历史遗留的 *_lightweight.* 断言全部删除，
# 并新增一条"轻量产物不得回流"的负向守卫。
# ---------------------------------------------------------------------------


def test_only_one_packaging_entrypoint_remains():
    """轻量打包四件套必须已删除，仓库只剩一套打包入口。"""
    for absent in (
        "光影选片助手_dist_lightweight.spec",
        "build_dist_lightweight.bat",
        "installer_lightweight.iss",
        "dist_runtime_hook_light.py",
    ):
        assert not (ROOT / absent).exists(), f"{absent} 应随轻量版一并删除"
    assert (ROOT / SPEC).is_file()
    assert (ROOT / "build_dist.bat").is_file()
    assert (ROOT / "installer.iss").is_file()
    assert (ROOT / "dist_runtime_hook.py").is_file()


def test_installer_uses_standard_dist_and_is_installable():
    installer = _text("installer.iss")
    assert '#define MySourceDir "dist\\光影选片助手"' in installer
    assert "AppId=LuminaSelect.Standard" in installer
    # 单版本：不得再残留第二版的 AppId / 互斥体
    assert "LuminaSelect.Lightweight" not in installer
    assert "SetupMutexAppId" not in installer
    assert "DirExistsWarning=no" in installer
    assert "DirsExistsWarning" not in installer
    assert "ArchitecturesAllowed=x64compatible" in installer
    assert "ArchitecturesInstallIn64BitMode=x64compatible" in installer


def test_standard_spec_does_not_collect_entire_packages():
    spec = _text(SPEC)
    assert "collect_submodules" not in spec
    assert "modules/face_landmark/**" in spec
    assert "modules/face_detection/**" in spec
    assert "excludes=" in spec


def test_standard_spec_collects_only_required_model_families():
    spec = _text(SPEC)
    assert "transformers.models.clip.modeling_clip" in spec
    assert "transformers.models.vit.modeling_vit" in spec
    assert "'pyiqa'" in spec.split("excludes=[", 1)[1]
    assert "pyiqa" not in spec.split("hiddenimports =", 1)[1].split("a = Analysis", 1)[0]


def test_runtime_hook_initializes_torch_before_ui_mediapipe():
    """标准版冻结产物必须在 UI 加载 MediaPipe 之前初始化 Torch 原生 DLL。"""
    hook = _text("dist_runtime_hook.py")
    assert "import torch" in hook


def test_standard_spec_keeps_torch_model_dependencies_and_native_torchvision():
    spec = _text(SPEC)
    excluded = spec.split("excludes=[", 1)[1].split("noarchive=", 1)[0]
    assert "'sympy'" not in excluded
    for filename in (
        "_C_stable.pyd", "image_stable.pyd", "jpeg8.dll", "libpng16.dll",
        "libsharpyuv.dll", "libwebp.dll", "zlib.dll",
    ):
        assert filename in spec


def test_pytest_config_is_cp936_readable():
    (ROOT / "pytest.ini").read_text(encoding="cp936")


def _locked_packages(name: str) -> dict[str, str]:
    packages = {}
    for raw_line in _text(name).splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", "--")):
            continue
        assert "==" in line, f"unlocked requirement in {name}: {line}"
        package, version = line.split("==", 1)
        assert version and not any(operator in version for operator in "<>~")
        packages[package.casefold().replace("_", "-")] = version
    return packages


def test_requirements_are_exact_and_commercial_safe():
    """只剩一套依赖锁：必须含深度学习栈，且绝不含 pyiqa（许可证不合规）。"""
    packages = _locked_packages("requirements.txt")
    assert packages["mediapipe"] == "0.10.21"
    assert {"torch", "torchvision", "transformers"} <= packages.keys()
    assert "pyiqa" not in packages


def test_requirement_inputs_stay_single_edition():
    """轻量输入（requirements-lightweight.txt / requirements/lightweight.in）已删除。"""
    assert not (ROOT / "requirements-lightweight.txt").exists()
    assert not (ROOT / "requirements" / "lightweight.in").exists()
    standard = _text("requirements/standard.in").casefold()
    development = _text("requirements/dev.in").casefold()
    assert "torch" in standard and "transformers" in standard
    assert "pyiqa" not in standard + development
    assert "pytest-qt" in development and "pip-tools" in development


def test_spec_bundles_runtime_and_notices():
    spec = _text(SPEC)
    for required in (
        "ui", "services", "lxml", "exiftool.exe", "exiftool_files",
        "styles.qss", "THIRD_PARTY_NOTICES.md", "NOTICE.md",
    ):
        assert required in spec, required
    assert "'exiftool/exiftool_files'" in spec
    assert "'pyiqa'" in spec.split("excludes=[", 1)[1]


def test_build_script_gates_packaging_in_order():
    script = _text("build_dist.bat").casefold()
    positions = [script.index(fragment) for fragment in (
        "verify_exiftool.py", "-m pytest", "check_release_licenses.py",
        "test_distribution_contracts.py", "-m pyinstaller",
    )]
    assert positions == sorted(positions)
    assert "if errorlevel 1 exit /b 1" in script
    assert "pause" not in script


def test_installer_is_development_candidate_with_notices():
    installer = _text("installer.iss")
    assert "OutputBaseFilename=LuminaSelect-v0.5-standard-dev-candidate" in installer
    assert "THIRD_PARTY_NOTICES.md" in installer
    assert "NOTICE.md" in installer
    assert 'Source: "{#MySourceDir}\\_internal\\THIRD_PARTY_NOTICES.md"' in installer
    assert "{uninstallexe}" in installer
    assert '#define MyAppVersion "0.5.0"' in installer
