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
    assert "pyiqa" not in spec.casefold()
    assert "modules/face_landmark/**" in spec


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
