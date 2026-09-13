from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _text(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_light_build_uses_isolated_environment_and_output():
    script = _text("build_dist_lightweight.bat")
    assert ".venv-light\\Scripts\\python.exe" in script
    assert "--distpath dist_light" in script
    assert "--workpath build_light" in script
    assert "installer_lightweight.iss" in script


def test_light_installer_is_separate():
    standard = _text("installer.iss")
    light = _text("installer_lightweight.iss")
    assert '#define MySourceDir "dist\\光影选片助手"' in standard
    assert '#define MySourceDir "dist_light\\光影选片助手"' in light
    assert "AppId=LuminaSelect.Standard" in standard
    assert "AppId=LuminaSelect.Lightweight" in light
    assert "SetupMutexAppId" not in standard + light
    assert "DirExistsWarning=no" in standard
    assert "DirExistsWarning=no" in light
    assert "DirsExistsWarning" not in standard + light
    assert "ArchitecturesAllowed=x64compatible" in standard
    assert "ArchitecturesAllowed=x64compatible" in light
    assert "ArchitecturesInstallIn64BitMode=x64compatible" in standard
    assert "ArchitecturesInstallIn64BitMode=x64compatible" in light
    assert "光影选片助手轻量版" in light


def test_light_spec_does_not_collect_entire_packages():
    spec = _text("光影选片助手_dist_lightweight.spec")
    assert "collect_submodules" not in spec
    assert "modules/face_landmark/**" in spec
    assert "modules/face_detection/**" in spec
    assert "'torch'" in spec
    assert "excludes=" in spec


def test_standard_spec_collects_only_required_model_families():
    spec = _text("光影选片助手_dist.spec")
    assert "collect_submodules" not in spec
    assert "transformers.models.clip.modeling_clip" in spec
    assert "transformers.models.vit.modeling_vit" in spec
    assert "'pyiqa'" in spec.split("excludes=[", 1)[1]
    assert "pyiqa" not in spec.split("hiddenimports =", 1)[1].split("a = Analysis", 1)[0]
    assert "modules/face_landmark/**" in spec


def test_standard_runtime_initializes_torch_before_ui_mediapipe():
    hook = _text("dist_runtime_hook.py")
    assert "import torch" in hook
    assert "import torch" not in _text("dist_runtime_hook_light.py")


def test_standard_spec_keeps_torch_model_dependencies_and_native_torchvision():
    spec = _text("光影选片助手_dist.spec")
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


def test_light_requirements_are_exact_and_exclude_standard_runtime():
    packages = _locked_packages("requirements-lightweight.txt")
    assert packages["mediapipe"] == "0.10.21"
    assert "opencv-contrib-python" in packages
    assert {"torch", "torchvision", "transformers", "pyiqa"}.isdisjoint(packages)


def test_standard_requirements_are_exact_and_commercial_safe():
    packages = _locked_packages("requirements.txt")
    assert packages["mediapipe"] == "0.10.21"
    assert {"torch", "torchvision", "transformers"} <= packages.keys()
    assert "pyiqa" not in packages


def test_requirement_inputs_separate_runtime_editions():
    standard = _text("requirements/standard.in").casefold()
    lightweight = _text("requirements/lightweight.in").casefold()
    development = _text("requirements/dev.in").casefold()
    assert "torch" in standard and "transformers" in standard
    assert "torch" not in lightweight and "transformers" not in lightweight
    assert "pyiqa" not in standard + lightweight + development
    assert "pytest-qt" in development and "pip-tools" in development


def test_both_specs_bundle_v05_runtime_and_notices():
    for name in ("光影选片助手_dist.spec", "光影选片助手_dist_lightweight.spec"):
        spec = _text(name)
        for required in ("ui", "services", "lxml", "exiftool.exe", "exiftool_files", "styles.qss", "THIRD_PARTY_NOTICES.md", "NOTICE.md"):
            assert required in spec, (name, required)
        assert "'exiftool/exiftool_files'" in spec
    standard = _text("光影选片助手_dist.spec")
    light = _text("光影选片助手_dist_lightweight.spec")
    assert "'pyiqa'" in standard.split("excludes=[", 1)[1]
    for excluded in ("'torch'", "'torchvision'", "'transformers'", "'pyiqa'"):
        assert excluded in light


def test_build_scripts_gate_packaging_in_order():
    for name in ("build_dist.bat", "build_dist_lightweight.bat"):
        script = _text(name).casefold()
        positions = [script.index(fragment) for fragment in (
            "verify_exiftool.py", "-m pytest", "check_release_licenses.py",
            "test_distribution_contracts.py", "-m pyinstaller",
        )]
        assert positions == sorted(positions), name
        assert "if errorlevel 1 exit /b 1" in script
        assert "pause" not in script


def test_installers_are_distinct_development_candidates_with_notices():
    standard = _text("installer.iss")
    light = _text("installer_lightweight.iss")
    assert "OutputBaseFilename=LuminaSelect-v0.5-standard-dev-candidate" in standard
    assert "OutputBaseFilename=LuminaSelect-v0.5-lightweight-dev-candidate" in light
    for installer in (standard, light):
        assert "THIRD_PARTY_NOTICES.md" in installer
        assert "NOTICE.md" in installer
        assert 'Source: "{#MySourceDir}\\_internal\\THIRD_PARTY_NOTICES.md"' in installer
        assert "{uninstallexe}" in installer
        assert '#define MyAppVersion "0.5.0"' in installer
